import hashlib
import json
import os
import random
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

from gateway.pricing import SHORT_ANSWER_INSTRUCTION

@dataclass
class Result:
    text: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    cached: bool = False
    fallback_used: bool = False
    thoughts_tokens: int = 0
    truncated: bool = False


class LLMError(Exception):
    pass


class ConfigError(LLMError):
    pass


class RateLimited(LLMError):
    def __init__(self, message: str = "請求遭到限速", retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class BudgetExceeded(LLMError):
    def __init__(self, tenant: str):
        self.tenant = tenant
        super().__init__(f"租戶 {tenant} 已超過預算上限")


class ProviderError(LLMError):
    pass


_rate_lock = Lock()
_requests: deque[float] = deque()
_usage_lock = Lock()
_mock_only: ContextVar[bool] = ContextVar("mock_only", default=False)


@contextmanager
def mock_only():
    scope = _mock_only.set(True)
    try:
        yield
    finally:
        _mock_only.reset(scope)


def list_aliases() -> dict[str, str]:
    aliases = {"fast": "gemini-3.1-flash-lite", "mock": "mock", "class": "fast"}
    if os.getenv("INSTRUCTOR") == "1":
        aliases["smart"] = "gemini-3.8-flash"
    return aliases


def prepared_messages(messages: list[dict]) -> list[dict]:
    instruction = {"role": "system", "content": SHORT_ANSWER_INSTRUCTION}
    return messages if messages and messages[0] == instruction else [instruction, *messages]


def _setting(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} 必須是非負整數") from exc
    if parsed < 0 or (name == "RPM_LIMIT" and parsed == 0):
        raise ConfigError(f"{name} 必須是{'正' if name == 'RPM_LIMIT' else '非負'}整數")
    return parsed


def _throttle(limit: int) -> None:
    with _rate_lock:
        now = time.monotonic()
        while _requests and _requests[0] <= now - 60:
            _requests.popleft()
        if len(_requests) >= limit:
            delay = _requests[0] + 60 - now
            time.sleep(max(0, delay))
            now = time.monotonic()
            while _requests and _requests[0] <= now - 60:
                _requests.popleft()
        _requests.append(now)


def _log_path() -> Path:
    return Path(os.getenv("USAGE_LOG", "./usage.jsonl"))


def _spent(tenant: str) -> float:
    path = _log_path()
    if not path.exists():
        return 0.0
    total = 0.0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            entry = json.loads(line)
            if entry["tenant"] == tenant:
                total += entry["cost_usd"]
    return total


def _check_budget(tenant: str) -> None:
    filename = os.getenv("BUDGETS_FILE")
    if not filename:
        return
    try:
        with Path(filename).open(encoding="utf-8") as stream:
            budgets = json.load(stream)
        if not isinstance(budgets, dict):
            raise ValueError("預算資料必須是 JSON 物件")
        limit = float(budgets[tenant]) if tenant in budgets else None
    except (OSError, ValueError, TypeError) as exc:
        raise ConfigError(f"BUDGETS_FILE 無法讀取或格式錯誤：{filename}") from exc
    if limit is not None and _spent(tenant) > limit:
        raise BudgetExceeded(tenant)


def _cache_path(alias: str, messages: list[dict]) -> Path:
    model = os.getenv("GATEWAY_MODEL", "fast") if alias == "class" else list_aliases().get(alias)
    payload = json.dumps([alias, model, messages], sort_keys=True, ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return Path(os.getenv("CACHE_DIR", "./.cache")) / f"{digest}.json"


def _record(
    tenant: str, alias: str, result: Result | None, error: Exception | None,
    provider: str, model: str, fallback_used: bool,
) -> None:
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tenant": tenant,
        "alias": alias,
        "provider": result.provider if result else provider,
        "model": result.model if result else model,
        "input_tokens": result.input_tokens if result else 0,
        "output_tokens": result.output_tokens if result else 0,
        "thoughts_tokens": result.thoughts_tokens if result else 0,
        "truncated": result.truncated if result else False,
        "cost_usd": result.cost_usd if result and not result.cached else 0.0,
        "cached": result.cached if result else False,
        "fallback_used": result.fallback_used if result else fallback_used,
        "status": "ok" if error is None else f"error:{type(error).__name__}",
    }
    path = _log_path()
    with _usage_lock:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")


def chat(
    messages: list[dict], *, alias: str | None = None, tenant: str = "default",
    skip_client_throttle: bool = False,
) -> Result:
    requested = alias if alias is not None else os.getenv("PROVIDER")
    provider = ""
    model = ""
    fallback_used = False
    result = None
    error = None
    try:
        if not requested:
            raise ConfigError("請指定 alias 或設定 PROVIDER（fast、mock、class）")
        aliases = list_aliases()
        if requested not in aliases:
            raise ConfigError(f"未知別名 {requested}；可用別名：{', '.join(aliases)}")
        if requested == "class" and os.getenv("GATEWAY_MODEL", "fast") not in {"fast", "mock"}:
            raise ConfigError("GATEWAY_MODEL 只能設定 fast 或 mock")
        if _mock_only.get() and requested != "mock":
            raise ConfigError("十題批次練習只允許 mock；真實 provider 請改用單題 make ask（或 uv run hw ask <問題>），以避免無謂請求與費用")
        fallback_names = [name.strip() for name in os.getenv("FALLBACK", "").split(",") if name.strip()]
        if _mock_only.get() and any(name != "mock" for name in fallback_names):
            raise ConfigError("十題批次練習只允許 mock；請移除真實 provider 的 FALLBACK，以避免無謂請求與費用")
        _check_budget(tenant)
        messages = prepared_messages(messages)
        cache_path = _cache_path(requested, messages)
        if cache_path.exists():
            try:
                with cache_path.open(encoding="utf-8") as stream:
                    result = replace(Result(**json.load(stream)), cached=True, cost_usd=0.0)
            except (OSError, ValueError, TypeError) as exc:
                raise ConfigError(f"快取無法讀取或格式錯誤：{cache_path}") from exc
            return result
        retries = _setting("MAX_RETRIES", 3)
        limit = _setting("RPM_LIMIT", 5)
        chain = [requested]
        chain.extend(fallback_names)
        chain = list(dict.fromkeys(chain))
        for name in chain:
            if name not in aliases:
                raise ConfigError(f"FALLBACK 含未知別名 {name}；可用別名：{', '.join(aliases)}")
        for index, name in enumerate(chain):
            fallback_used = index > 0
            model = aliases[name]
            provider = "remote" if name == "class" else "mock" if name == "mock" else "gemini"
            for attempt in range(retries + 1):
                try:
                    if not skip_client_throttle:
                        _throttle(limit)
                    if name == "mock":
                        from gateway.providers import mock
                        result = mock.chat(messages)
                    elif name == "class":
                        from gateway.providers import remote
                        result = remote.chat(messages, tenant=tenant)
                    else:
                        from gateway.providers import gemini
                        result = gemini.chat(messages, model)
                    if not result.text:
                        raise ProviderError(f"{provider} 回傳空內容")
                    result = replace(result, fallback_used=fallback_used)
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(asdict(result), ensure_ascii=False), encoding="utf-8")
                    error = None
                    return result
                except RateLimited as exc:
                    error = exc
                    if attempt < retries:
                        ceiling = max(0, exc.retry_after if exc.retry_after is not None else 1) * 2**attempt
                        time.sleep(random.uniform(0, ceiling))
                except (ConfigError, ProviderError) as exc:
                    error = exc
                    break
        assert error is not None
        raise error
    except Exception as exc:
        error = exc
        raise
    finally:
        _record(tenant, requested or "", result, error, provider, model, fallback_used)
