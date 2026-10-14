"""講師共用的單執行個體 gateway。"""

import asyncio
import concurrent.futures
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import math
import os
import sys
import time
import unicodedata
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import BoundedSemaphore, Lock
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from gateway import llm
from gateway.pricing import MAX_OUTPUT_TOKENS, SHORT_ANSWER_INSTRUCTION, cost_usd, estimate_tokens
from server import tokens as token_format


MAX_BODY_BYTES = 65536
MAX_MESSAGES = 20
MAX_CHARACTERS = 4000  # F11：由 12000 降為 4000
CLAIM_RPM = 20  # 每個識別 IP；會場可能共用公網 IP，可用環境變數 CLAIM_RPM 覆蓋
SNAPSHOT_INTERVAL_S = 30
SNAPSHOT_FILE = Path("/tmp/hwdc2026-server-state.json")
UPSTREAM_TIMEOUT_S = 30  # F9：上游（Gemini）請求逾時，可用 UPSTREAM_TIMEOUT_S 覆蓋
MAX_BUCKETS = 5000  # 每種限速桶最多保留的鍵數
MIN_CLASS_CODE = 10
MIN_SECRET = 32
LOG = logging.getLogger("uvicorn.error")


class Claim(BaseModel):
    class_code: str
    nickname: str


class Revocation(BaseModel):
    nickname: str


class Message(BaseModel):
    role: str
    content: str


class Completion(BaseModel):
    model: str
    messages: list[Message] = Field(min_length=1, max_length=MAX_MESSAGES)


class Submission(BaseModel):
    level: str = Field(min_length=1, max_length=40)
    passed: bool
    tokens: int = Field(ge=0, le=10**9)
    cost: float = Field(ge=0, le=10**6)
    details: dict = Field(default_factory=dict)


def _number(name: str, default: str, *, integer: bool = False) -> float | int:
    raw = os.getenv(name, default)
    try:
        value = int(raw) if integer else float(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必須是正數") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"{name} 必須是正數")
    return value


def _secret(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(503, detail=f"缺少 {name} 設定")
    return value


def _same(left: str, right: str) -> bool:
    """以 bytes 比對，非 ASCII（含孤立 surrogate）不會拋出 TypeError。"""
    return hmac.compare_digest(left.encode("utf-8", "surrogatepass"), right.encode("utf-8", "surrogatepass"))


def _normalize(value: str) -> str:
    """NFKC + 空白收斂 + 去除控制／格式字元 + casefold，撤銷、綁定、排行榜都用這個值。"""
    text = unicodedata.normalize("NFKC", value)
    text = " ".join(text.split())
    text = "".join(char for char in text if not unicodedata.category(char).startswith("C"))
    text = " ".join(text.split()).casefold()
    return unicodedata.normalize("NFKC", text)


def _nickname(value: str) -> str:
    cleaned = _normalize(value)
    if not cleaned or len(cleaned) > 20:
        raise HTTPException(422, detail="暱稱須為 1 至 20 個字元")
    return cleaned


def _try_nickname(value: str) -> str | None:
    try:
        return _nickname(value)
    except HTTPException:
        return None


def _conservative_tokens(text: str) -> int:
    """保守估算輸入 token：CJK 1；ASCII 字母／空白 1/4；ASCII 數字與符號 1/2；其餘（emoji、符號、非拉丁字母）1。"""
    total = 0.0
    for char in text:
        if char < "\x80":
            total += 0.25 if char.isalpha() or char.isspace() else 0.5
        else:
            total += 1.0
    return math.ceil(total)


def _estimate_input(messages: list[dict]) -> int:
    return estimate_tokens(SHORT_ANSWER_INSTRUCTION) + sum(_conservative_tokens(m["content"]) for m in messages)


_INT_FIELDS = ("question_count", "quality_passed")


def _clean_int(value, maximum: int = 100000) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
        return None
    return value


def _clean_details(details: dict) -> dict:
    """F1：details 一律走白名單，其餘（含 class_code、token）全部丟棄。"""
    clean: dict = {}
    for key in ("l1", "l2"):
        value = details.get(key)
        if isinstance(value, dict):
            passed, total = _clean_int(value.get("passed"), 1000), _clean_int(value.get("total"), 1000)
            if passed is not None and total is not None:
                clean[key] = {"passed": passed, "total": total}
    for key in _INT_FIELDS:
        value = _clean_int(details.get(key), 1000)
        if value is not None:
            clean[key] = value
    return clean


def _public_row(row: dict) -> dict:
    return {key: row[key] for key in ("nickname", "level", "passed", "tokens", "cost", "updated_at")}


def _rate(bucket: deque[float], limit: int) -> None:
    now = time.monotonic()
    while bucket and bucket[0] <= now - 60:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(429, detail="請求過於頻繁", headers={"Retry-After": str(max(1, math.ceil(bucket[0] + 60 - now)))})
    bucket.append(now)


def _bucket(store: dict, key: str) -> deque:
    """限速桶有上限：滿了先清過期桶，仍滿則共用 overflow 桶（寧可誤限，不讓記憶體無限成長）。"""
    if key not in store and len(store) >= MAX_BUCKETS:
        _prune(store)
        if len(store) >= MAX_BUCKETS:
            key = "_overflow"
    return store[key]


def _prune(store: dict) -> None:
    now = time.monotonic()
    for key in list(store):
        bucket = store[key]
        while bucket and bucket[0] <= now - 60:
            bucket.popleft()
        if not bucket:
            del store[key]


class _Guard:
    """失敗次數鎖定：每個識別碼在 window 秒內失敗 limit 次即鎖 lock_s 秒。"""

    def __init__(self) -> None:
        self.failures: dict[str, deque[float]] = {}
        self.locked: dict[str, float] = {}

    def retry_after(self, key: str) -> int:
        until = self.locked.get(key, 0.0)
        now = time.monotonic()
        return max(1, math.ceil(until - now)) if until > now else 0

    def fail(self, key: str, limit: int, window: float, lock_s: float) -> None:
        now = time.monotonic()
        if key not in self.failures and len(self.failures) >= MAX_BUCKETS:
            self.prune(window)
            if len(self.failures) >= MAX_BUCKETS:
                key = "_overflow"
        bucket = self.failures.setdefault(key, deque())
        while bucket and bucket[0] <= now - window:
            bucket.popleft()
        bucket.append(now)
        if len(bucket) >= limit:
            self.locked[key] = now + lock_s
            bucket.clear()

    def prune(self, window: float = 60.0) -> None:
        now = time.monotonic()
        for key in list(self.failures):
            bucket = self.failures[key]
            while bucket and bucket[0] <= now - window:
                bucket.popleft()
            if not bucket:
                del self.failures[key]
        for key in [key for key, until in self.locked.items() if until <= now]:
            del self.locked[key]


def client_identity(request: Request) -> dict:
    """F2：依 TRUSTED_PROXY_HOPS 取 X-Forwarded-For 由右往左第 N 個位址；不足或畸形則退回 client.host。"""
    host = request.client.host if request.client else "unknown"
    try:
        hops = int(os.getenv("TRUSTED_PROXY_HOPS", "1"))
    except ValueError:
        hops = 1
    hops = max(0, hops)
    raw = ",".join(request.headers.getlist("x-forwarded-for"))
    chain = [part.strip() for part in raw.split(",")] if raw else []
    resolved, source = host, "client.host"
    if hops > 0 and len(chain) >= hops:
        candidate = chain[-hops]
        try:
            resolved, source = str(ipaddress.ip_address(candidate)), "x-forwarded-for"
        except ValueError:
            pass
    return {"client_host": host, "x_forwarded_for": raw, "chain": chain,
            "trusted_proxy_hops": hops, "resolved_ip": resolved, "source": source}


def _fatal(code: int) -> None:
    """設定不合格時直接結束行程（exit 1）；uvicorn 對 lifespan 失敗預設是 exit 3，這裡明確用 1。"""
    os._exit(code)


def config_problems() -> list[str]:
    """F3：設定強度檢查；回傳問題清單（空代表通過）。"""
    problems = []
    for name, minimum in (("CLASS_CODE", MIN_CLASS_CODE), ("SERVER_SECRET", MIN_SECRET), ("ADMIN_TOKEN", MIN_SECRET)):
        value = os.getenv(name, "")
        if len(value) < minimum:
            problems.append(f"{name} 長度 {len(value)}，至少需要 {minimum} 字元")
    return problems


def _budget_error(remaining: float, reserved: float) -> HTTPException:
    return HTTPException(402, detail={
        "code": "budget_exceeded",
        "message": f"剩餘預算 ${max(0, remaining):.8f} USD，不足以預留本次最多 ${reserved:.8f} USD",
        "remaining_usd": max(0, remaining),
        "required_usd": reserved,
    })


class BodyLimitMiddleware:
    """F6：串流計數請求本文；不論有無 Content-Length，累計超過上限立即以 413 中止。"""

    def __init__(self, app, limit: int = MAX_BODY_BYTES) -> None:
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > self.limit:
            await JSONResponse({"detail": "請求內容過大"}, status_code=413)(scope, receive, send)
            return
        seen = 0

        async def counted():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self.limit:
                    raise HTTPException(413, detail="請求內容過大")
            return message

        await self.app(scope, counted, send)


def create_app() -> FastAPI:
    lock = Lock()
    boot_id = uuid4().hex
    started = time.monotonic()
    claims: dict[str, deque[float]] = defaultdict(deque)
    global_claims: deque[float] = deque()
    claim_guard, admin_guard = _Guard(), _Guard()
    global_failures: deque[float] = deque()
    cooldown_until = 0.0
    requests: dict[str, deque[float]] = defaultdict(deque)
    tokens: dict[str, deque[tuple[float, int]]] = defaultdict(deque)
    costs: dict[str, float] = defaultdict(float)
    reserved_costs: dict[str, float] = defaultdict(float)
    reserved_tokens: dict[str, int] = defaultdict(int)
    active_tokens: set[str] = set()
    revoked: set[str] = set()
    ttl_deadlines: dict[str, float] = {}
    bound: dict[str, dict] = {}  # 暱稱 -> {"token_id", "expires"}（F4 綁定）
    ever_issued: set[str] = set()
    concurrency = _number("GLOBAL_CONCURRENCY", "16", integer=True)
    slots = BoundedSemaphore(concurrency)
    upstream = concurrent.futures.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="upstream")
    submissions: dict[str, dict] = {}
    total_cost = 0.0
    total_reserved = 0.0
    killed = False
    waiting = 0

    def public_rows() -> list[dict]:
        return sorted((_public_row(row) for row in submissions.values()),
                      key=lambda item: (-int(item["passed"]), item["cost"], item["nickname"]))

    def snapshot() -> None:
        with lock:
            data = {
                "boot_id": boot_id,
                "total_cost_usd": total_cost,
                "claim_count_by_ip": {ip: len(times) for ip, times in claims.items()},
                "token_costs_usd": dict(costs),
                "submissions": public_rows(),
                "killed": killed,
            }
        temporary = SNAPSHOT_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        temporary.replace(SNAPSHOT_FILE)

    def housekeeping() -> None:
        """定期清理限速桶；並限制 usage／cache 目錄大小（F10）。"""
        with lock:
            _prune(claims)
            _prune(requests)
            claim_guard.prune()
            admin_guard.prune()
        usage = Path(os.getenv("USAGE_LOG", "./usage.jsonl"))
        try:
            if usage.is_file() and usage.stat().st_size > _number("MAX_USAGE_LOG_BYTES", "5242880", integer=True):
                usage.replace(usage.with_suffix(usage.suffix + ".1"))
            cache = Path(os.getenv("CACHE_DIR", "./.cache"))
            if cache.is_dir():
                files = sorted(cache.glob("*.json"), key=lambda path: path.stat().st_mtime)
                excess = len(files) - int(_number("MAX_CACHE_FILES", "500", integer=True))
                for path in files[:max(0, excess)]:
                    path.unlink(missing_ok=True)
        except OSError:
            pass

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        problems = config_problems()
        if problems and os.getenv("ALLOW_WEAK_SECRETS") != "1":
            print("啟動中止：設定強度不足\n  - " + "\n  - ".join(problems)
                  + "\n請使用 Secret Manager 注入足夠長的隨機值（CLASS_CODE ≥ 10、SERVER_SECRET 與 ADMIN_TOKEN ≥ 32 字元）；"
                  "僅本機測試可設 ALLOW_WEAK_SECRETS=1。", file=sys.stderr)
            sys.stderr.flush()
            _fatal(1)
        if problems:
            print("警告：ALLOW_WEAK_SECRETS=1，略過設定強度檢查（僅限本機測試）", file=sys.stderr)
        # F10：server 預設把 usage／cache 放進 /tmp，並由 housekeeping 限制大小。
        os.environ.setdefault("USAGE_LOG", "/tmp/hwdc2026-usage.jsonl")
        os.environ.setdefault("CACHE_DIR", "/tmp/hwdc2026-cache")

        async def periodic_snapshot() -> None:
            while True:
                await asyncio.sleep(SNAPSHOT_INTERVAL_S)
                snapshot()
                housekeeping()

        task = asyncio.create_task(periodic_snapshot())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            upstream.shutdown(wait=False, cancel_futures=True)
            snapshot()

    # F16：關閉 /docs、/redoc、/openapi.json
    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(BodyLimitMiddleware)

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception):
        LOG.error("未處理例外：%s", type(exc).__name__)  # 只記類別，不記訊息、不對外洩漏堆疊
        return JSONResponse({"detail": "內部錯誤"}, status_code=500)

    def enabled() -> None:
        if os.getenv("KILL") == "1" or killed:
            raise HTTPException(503, detail="服務已暫停")

    def bearer(authorization: str | None) -> str:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(401, detail="缺少 Bearer token")
        return authorization[7:]

    def verified(authorization: str | None) -> tuple[str, str] | None:
        """驗證 Authorization 內的 token；無效回傳 None（不拋出）。"""
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[7:]
        try:
            nickname = token_format.verify(token, _secret("SERVER_SECRET"),
                                           now=datetime.fromtimestamp(time.time(), timezone.utc))
        except (ValueError, HTTPException):
            return None
        return _normalize(nickname), hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()

    def authenticate(authorization: str | None) -> tuple[str, str]:
        token = bearer(authorization)
        try:
            nickname = token_format.verify(token, _secret("SERVER_SECRET"),
                                           now=datetime.fromtimestamp(time.time(), timezone.utc))
        except ValueError as exc:
            raise HTTPException(401, detail=str(exc)) from None
        nickname = _normalize(nickname)
        token_id = hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()
        with lock:
            if nickname in revoked:
                raise HTTPException(401, detail="token 已被講師撤銷")
            if token_id in ttl_deadlines and time.time() > ttl_deadlines[token_id]:
                raise HTTPException(401, detail="token 過期或無效")
        return nickname, token_id

    def admin(authorization: str | None, request: Request) -> None:
        """F8：/admin/* 依識別 IP 限制失敗次數；鎖定期間連正確 token 也拒絕。"""
        ip = client_identity(request)["resolved_ip"]
        with lock:
            wait = admin_guard.retry_after(ip)
        if wait:
            raise HTTPException(429, detail="管理端驗證失敗次數過多，請稍後再試", headers={"Retry-After": str(wait)})
        try:
            candidate = bearer(authorization)
            ok = _same(candidate, _secret("ADMIN_TOKEN"))
        except HTTPException as exc:
            if exc.status_code != 401:
                raise
            ok = False
        if not ok:
            with lock:
                admin_guard.fail(ip, int(_number("ADMIN_FAIL_LIMIT", "10", integer=True)), 60,
                                 _number("ADMIN_LOCK_S", "60"))
            raise HTTPException(401, detail="管理員驗證失敗")

    # Cloud Run 的前端會攔截 /healthz（回 Google 自己的 404），所以另提供 /health 供外部健康檢查。
    @app.get("/health")
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "killed": os.getenv("KILL") == "1" or killed,
                "boot_id": boot_id, "uptime_s": round(time.monotonic() - started, 1)}

    @app.post("/claim")
    def claim(body: Claim, request: Request):
        nonlocal cooldown_until
        enabled()
        ip = client_identity(request)["resolved_ip"]
        hint = _try_nickname(body.nickname)
        presented = verified(request.headers.get("authorization"))
        # 已持有同暱稱有效 token 的重新領取（reclaim）不受鎖定與全域冷卻影響。
        trusted = bool(presented and hint and presented[0] == hint)
        now = time.monotonic()
        with lock:
            if not trusted:
                wait = claim_guard.retry_after(ip)
                if wait:
                    raise HTTPException(429, detail="課堂代碼錯誤次數過多，請稍後再試", headers={"Retry-After": str(wait)})
                if cooldown_until > now:
                    raise HTTPException(429, detail="領取暫時冷卻中，請稍後再試",
                                        headers={"Retry-After": str(max(1, math.ceil(cooldown_until - now)))})
            _rate(_bucket(claims, ip), int(_number("CLAIM_RPM", str(CLAIM_RPM), integer=True)))
            if not trusted:
                _rate(global_claims, int(_number("CLAIM_GLOBAL_RPM", "120", integer=True)))
        if not _same(body.class_code, _secret("CLASS_CODE")):
            if not trusted:
                with lock:
                    claim_guard.fail(ip, int(_number("CLAIM_FAIL_LIMIT", "10", integer=True)), 60,
                                     _number("CLAIM_LOCK_S", "60"))
                    global_failures.append(now)
                    while global_failures and global_failures[0] <= now - 60:
                        global_failures.popleft()
                    if len(global_failures) >= int(_number("CLAIM_GLOBAL_FAIL_LIMIT", "60", integer=True)):
                        cooldown_until = now + _number("CLAIM_GLOBAL_COOLDOWN_S", "60")
                        global_failures.clear()
            raise HTTPException(403, detail="班級代碼錯誤")
        nickname = _nickname(body.nickname)
        day = os.getenv("TOKEN_EXPIRES_ON", "20261016")
        try:
            expiry = token_format.expires_at(day)
        except ValueError as exc:
            raise HTTPException(503, detail=str(exc)) from exc
        wall = time.time()
        if datetime.fromtimestamp(wall, timezone.utc).astimezone(token_format.TAIPEI) > expiry:
            raise HTTPException(503, detail="簽發日期已過，請更新 TOKEN_EXPIRES_ON")
        token = token_format.issue(nickname, day, _secret("SERVER_SECRET"))
        token_id = hashlib.sha256(token.encode()).hexdigest()
        ttl = os.getenv("TOKEN_TTL_S")
        with lock:
            if nickname in revoked:
                raise HTTPException(403, detail="暱稱已被講師撤銷")
            entry = bound.get(nickname)
            if entry and entry["expires"] > wall and not trusted:
                raise HTTPException(409, detail="此暱稱已領取過 token；如需重領請在 Authorization 帶舊 token，"
                                                "遺失請聯絡講師解除綁定")
            if nickname not in ever_issued and len(ever_issued) >= _number("MAX_TOKENS_ISSUED", "80", integer=True):
                raise HTTPException(403, detail="全場簽發上限已達，請聯絡講師")
            if ttl is not None:
                deadline = wall + _number("TOKEN_TTL_S", ttl, integer=True)
                ttl_deadlines[token_id] = min(deadline, ttl_deadlines.get(token_id, deadline))
                deadline = ttl_deadlines[token_id]
                expiry = min(expiry, datetime.fromtimestamp(deadline, token_format.TAIPEI))
            bound[nickname] = {"token_id": token_id, "expires": expiry.timestamp()}
            ever_issued.add(nickname)
        return {"token": token, "expires_at": expiry.isoformat(), "expires_on": day, "reclaimed": trusted}

    @app.post("/v1/chat/completions")
    def completions(body: Completion, authorization: str | None = Header(default=None)):
        nonlocal total_cost, total_reserved, waiting
        enabled()
        nickname, token_id = authenticate(authorization)
        aliases = {alias.strip() for alias in os.getenv("ALLOWED_ALIASES", "fast,mock").split(",") if alias.strip()}
        aliases &= {"fast", "mock", "smart"} if os.getenv("INSTRUCTOR") == "1" else {"fast", "mock"}
        if body.model not in aliases or body.model not in llm.list_aliases():
            raise HTTPException(400, detail="模型別名不在白名單")
        fallback = {alias.strip() for alias in os.getenv("FALLBACK", "").split(",") if alias.strip()}
        if not fallback <= aliases:
            raise HTTPException(503, detail="FALLBACK 含未授權的模型別名")
        if any(message.role not in {"system", "user", "assistant"} for message in body.messages):
            raise HTTPException(422, detail="不支援的訊息角色")
        if sum(len(message.content) for message in body.messages) > MAX_CHARACTERS:
            raise HTTPException(413, detail="訊息字數超過上限")
        messages = [message.model_dump() for message in body.messages]
        input_tokens = _estimate_input(messages)
        if input_tokens > _number("MAX_INPUT_TOKENS", "1500", integer=True):
            raise HTTPException(413, detail="輸入 token 超過上限，請先截斷 context")
        estimated_tokens = input_tokens + MAX_OUTPUT_TOKENS
        models = llm.list_aliases()
        reserved = max(cost_usd(models[name], input_tokens, MAX_OUTPUT_TOKENS) for name in {body.model} | fallback)
        with lock:
            if token_id in active_tokens:
                raise HTTPException(429, detail="請等上一個請求完成", headers={"Retry-After": "1"})
            active_tokens.add(token_id)
        acquired = False
        budget_reserved = False
        handed_off = False
        counted = False
        try:
            with lock:
                if waiting >= _number("MAX_WAITING", "16", integer=True):
                    raise HTTPException(503, detail="等候中的請求過多，請稍後再試", headers={"Retry-After": "1"})
                waiting += 1
                counted = True
            try:
                acquired = slots.acquire(timeout=_number("GLOBAL_QUEUE_TIMEOUT_S", "20"))
            finally:
                with lock:
                    waiting -= 1
                    counted = False
            if not acquired:
                raise HTTPException(503, detail="全域佇列等候逾時，請稍後再試", headers={"Retry-After": "1"})
            enabled()
            with lock:
                now = time.monotonic()
                bucket = tokens[token_id]
                while bucket and bucket[0][0] <= now - 60:
                    bucket.popleft()
                used_tokens = sum(amount for _, amount in bucket)
                tpm = _number("TOKEN_TPM", "4000", integer=True)
                if used_tokens + reserved_tokens[token_id] + estimated_tokens > tpm:
                    retry = max(1, math.ceil(bucket[0][0] + 60 - now)) if bucket else 1
                    raise HTTPException(429, detail="token 每分鐘用量超過上限", headers={"Retry-After": str(retry)})
                remaining = _number("TOKEN_BUDGET_USD", "0.05") - costs[token_id] - reserved_costs[token_id]
                if reserved > remaining + 1e-12:
                    raise _budget_error(remaining, reserved)
                global_remaining = _number("GLOBAL_BUDGET_USD", "10") - total_cost - total_reserved
                if reserved > global_remaining + 1e-12:
                    raise _budget_error(global_remaining, reserved)
                _rate(requests[token_id], _number("TOKEN_RPM", "6", integer=True))
                reserved_costs[token_id] += reserved
                reserved_tokens[token_id] += estimated_tokens
                total_reserved += reserved
                budget_reserved = True
            future = upstream.submit(llm.chat, messages, alias=body.model, tenant=nickname, skip_client_throttle=True)
            finished, _ = concurrent.futures.wait([future], timeout=_number("UPSTREAM_TIMEOUT_S", str(UPSTREAM_TIMEOUT_S)))
            if not finished:
                # F9：上游逾時。上游執行緒無法強制中止，slot 交給完成回呼釋放（維持上游並行上限的真實性），
                # 若稍後成功仍把實際費用記入帳上。
                handed_off = True

                def late_finish(done: concurrent.futures.Future) -> None:
                    nonlocal total_cost
                    try:
                        late = done.result()
                        with lock:
                            costs[token_id] += late.cost_usd
                            total_cost += late.cost_usd
                    except BaseException:  # noqa: BLE001 - 逾時後的失敗不再有人接手
                        pass
                    finally:
                        slots.release()

                future.add_done_callback(late_finish)
                raise HTTPException(504, detail="上游模型逾時，請稍後再試", headers={"Retry-After": "2"})
            try:
                result = future.result()
            except llm.RateLimited as exc:
                raise HTTPException(429, detail=str(exc), headers={"Retry-After": str(max(0, exc.retry_after or 0))}) from exc
            except llm.BudgetExceeded as exc:
                raise HTTPException(402, detail={
                    "code": "budget_exceeded",
                    "message": f"{exc}；gateway 剩餘預算 ${max(0, min(remaining, global_remaining)):.8f} USD",
                    "remaining_usd": max(0, min(remaining, global_remaining)),
                    "required_usd": reserved,
                }) from exc
            except llm.ConfigError as exc:
                raise HTTPException(503, detail=str(exc)) from exc
            except llm.ProviderError as exc:
                raise HTTPException(502, detail=str(exc)) from exc
            with lock:
                costs[token_id] += result.cost_usd
                total_cost += result.cost_usd
                tokens[token_id].append((time.monotonic(), estimated_tokens))
        finally:
            with lock:
                if budget_reserved:
                    reserved_costs[token_id] -= reserved
                    reserved_tokens[token_id] -= estimated_tokens
                    total_reserved -= reserved
                if counted:
                    waiting -= 1
                active_tokens.remove(token_id)
            if acquired and not handed_off:
                slots.release()
        return {
            "id": f"chatcmpl-{uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": result.model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": result.text},
                         "finish_reason": "length" if result.truncated else "stop"}],
            "usage": {"prompt_tokens": result.input_tokens, "completion_tokens": result.output_tokens,
                      "total_tokens": result.input_tokens + result.output_tokens},
            "x_cost_usd": result.cost_usd,
            "x_truncated": result.truncated,
        }

    @app.get("/questions")
    def questions(authorization: str | None = Header(default=None)):
        enabled()
        authenticate(authorization)
        path = Path(os.getenv("QUESTIONS_FILE", "questions.sample.json"))
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise HTTPException(503, detail=f"題組無法讀取：{path}") from exc
        if not isinstance(data, list):
            raise HTTPException(503, detail="題組格式錯誤")
        return data

    @app.post("/submit")
    def submit(body: Submission, authorization: str | None = Header(default=None)):
        enabled()
        nickname, _ = authenticate(authorization)
        if not math.isfinite(body.cost):
            raise HTTPException(422, detail="成本必須是有限數字")
        with lock:
            submissions[nickname] = {
                "nickname": nickname, "level": body.level, "passed": body.passed, "tokens": body.tokens,
                "cost": body.cost, "details": _clean_details(body.details),
                "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        return {"ok": True}

    @app.get("/leaderboard")
    def leaderboard(format: str | None = Query(default=None)):
        with lock:
            rows = public_rows()
        if format == "json":
            return rows
        if format is not None:
            raise HTTPException(400, detail="不支援的格式")
        items = "".join(
            f"<tr><td class=\"rank\">{rank}</td><td>{html.escape(row['nickname'])}</td><td>{html.escape(row['level'])}</td>"
            f"<td>{'通過' if row['passed'] else '未通過'}</td><td>{row['tokens']}</td><td>{row['cost']:.6f}</td>"
            f"<td>{html.escape(row['updated_at'])}</td></tr>"
            for rank, row in enumerate(rows, 1)
        )
        style = (
            "<style>body{margin:0;padding:2rem 3rem;background:#12151c;color:#e8eaf0;"
            "font:28px/1.5 -apple-system,'PingFang TC','Noto Sans TC',sans-serif}"
            "h1{font-size:2.4rem;margin:0 0 1rem}table{border-collapse:collapse;width:100%}"
            "th,td{padding:.5rem 1rem;text-align:left;border-bottom:1px solid #2c3242}"
            "th{color:#8f98ad;font-weight:600;font-size:.8em}.rank{color:#f0c36a;font-weight:700;width:3rem}"
            "tr:nth-child(2) td{background:#1b2030}</style>"
        )
        return HTMLResponse(
            '<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="5"><title>排行榜</title>'
            + style + "</head><body>"
            "<h1>排行榜</h1><table><tr><th>#</th><th>暱稱</th><th>關卡</th><th>通過</th><th>tokens</th><th>成本</th>"
            f"<th>更新時間</th></tr>{items}</table></body></html>"
        )

    @app.get("/admin/stats")
    def stats(request: Request, authorization: str | None = Header(default=None)):
        admin(authorization, request)
        with lock:
            return {"total_cost_usd": total_cost, "claims": sum(map(len, claims.values())),
                    "submissions": len(submissions), "killed": killed or os.getenv("KILL") == "1",
                    "boot_id": boot_id, "uptime_s": round(time.monotonic() - started, 1),
                    "issued": len(ever_issued), "bound": len(bound), "waiting": waiting}

    @app.get("/admin/debug-client")
    def debug_client(request: Request, authorization: str | None = Header(default=None)):
        admin(authorization, request)
        return client_identity(request)

    @app.post("/admin/revoke")
    def revoke(body: Revocation, request: Request, authorization: str | None = Header(default=None)):
        admin(authorization, request)
        nickname = _nickname(body.nickname)
        with lock:
            revoked.add(nickname)
        return {"revoked": nickname}

    @app.post("/admin/unrevoke")
    def unrevoke(body: Revocation, request: Request, authorization: str | None = Header(default=None)):
        admin(authorization, request)
        nickname = _nickname(body.nickname)
        with lock:
            revoked.discard(nickname)
        return {"unrevoked": nickname}

    @app.post("/admin/release")
    def release(body: Revocation, request: Request, authorization: str | None = Header(default=None)):
        admin(authorization, request)
        nickname = _nickname(body.nickname)
        with lock:
            was_bound = bound.pop(nickname, None) is not None
        return {"released": nickname, "was_bound": was_bound}

    @app.get("/admin/revoked")
    def list_revoked(request: Request, authorization: str | None = Header(default=None)):
        admin(authorization, request)
        with lock:
            return {"revoked": sorted(revoked)}

    @app.post("/admin/kill")
    def kill(request: Request, authorization: str | None = Header(default=None)):
        nonlocal killed
        admin(authorization, request)
        with lock:
            killed = True
        return {"killed": True}

    @app.post("/admin/revive")
    def revive(request: Request, authorization: str | None = Header(default=None)):
        nonlocal killed
        admin(authorization, request)
        with lock:
            killed = False
        return {"killed": os.getenv("KILL") == "1"}

    app.state.snapshot = snapshot
    app.state.housekeeping = housekeeping
    return app


app = create_app()
