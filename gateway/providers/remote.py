"""講師 gateway 的遠端 provider。"""

import errno
import hashlib
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from gateway.llm import BudgetExceeded, ConfigError, ProviderError, RateLimited, Result
from gateway.token_store import read_token


_local_guard = Lock()
_local_locks: dict[Path, tuple[Lock, int]] = {}


def _lock_directory() -> Path:
    explicit = os.getenv("REMOTE_LOCK_DIR")
    if explicit:
        return Path(explicit).resolve()
    # 預設放在使用者家目錄：同一使用者的所有終端機看到同一個位置；
    # TMPDIR／TEMP 會因 shell、工作階段而不同，不適合當跨行程的會合點。
    try:
        directory = Path.home() / ".hwdc26" / "locks"
        directory.mkdir(parents=True, exist_ok=True)
        return directory.resolve()
    except (OSError, RuntimeError):
        return Path(tempfile.gettempdir()).resolve()


def lock_path(url: str, token: str) -> Path:
    """鎖檔路徑只由 (url, token) 與 REMOTE_LOCK_DIR（預設 ~/.hwdc26/locks）決定。"""
    digest = hashlib.sha256((url + "\0" + token).encode()).hexdigest()
    return _lock_directory() / f"hwdc-class-{digest}.lock"


@contextmanager
def _one_request(url: str, token: str):
    try:
        timeout = float(os.getenv("REMOTE_LOCK_TIMEOUT_S", "20"))
    except ValueError as exc:
        raise ConfigError("REMOTE_LOCK_TIMEOUT_S 必須是非負秒數") from exc
    if not 0 <= timeout < float("inf"):
        raise ConfigError("REMOTE_LOCK_TIMEOUT_S 必須是非負秒數")
    path = lock_path(url, token)
    deadline = time.monotonic() + timeout
    with _local_guard:
        local, users = _local_locks.get(path, (Lock(), 0))
        _local_locks[path] = (local, users + 1)
    acquired = False
    try:
        acquired = local.acquire(timeout=max(0, deadline - time.monotonic()))
        if not acquired:
            raise ProviderError("上一個真實請求尚未完成；等候本機鎖逾時，請稍後再試")
        try:
            lock_file = path.open("a+b")
        except OSError as exc:
            raise ProviderError("無法存取本機請求鎖") from exc
        with lock_file as lock:
            os.set_inheritable(lock.fileno(), False)
            if os.name == "nt":
                import msvcrt
                if lock.seek(0, os.SEEK_END) == 0:
                    lock.write(b"\0")
                    lock.flush()
            else:
                import fcntl
            while True:
                try:
                    if os.name == "nt":
                        lock.seek(0)
                        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK, errno.EDEADLK):
                        raise ProviderError("無法取得本機請求鎖") from exc
                    if time.monotonic() >= deadline:
                        raise ProviderError("上一個真實請求尚未完成；等候本機鎖逾時，請稍後再試") from exc
                    time.sleep(min(0.1, max(0, deadline - time.monotonic())))
            try:
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    finally:
        if acquired:
            local.release()
        with _local_guard:
            _, users = _local_locks[path]
            if users == 1:
                del _local_locks[path]
            else:
                _local_locks[path] = (local, users - 1)


def _detail(exc: HTTPError) -> str:
    """取出 server 回傳的 detail，讓錯誤原因清楚可見（失敗要叫）。"""
    try:
        detail = json.load(exc)["detail"]
        text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
        return text[:200]
    except Exception:
        return "（無額外說明）"


def chat(messages: list[dict], tenant: str = "default") -> Result:
    model_alias = os.getenv("GATEWAY_MODEL", "fast")
    if model_alias not in {"fast", "mock"}:
        raise ConfigError("GATEWAY_MODEL 只能設定 fast 或 mock")
    url = os.getenv("GATEWAY_URL", "").strip()
    try:
        token = read_token()
    except OSError as exc:
        raise ConfigError("無法讀取課堂 token；請重新 make claim（或 uv run hw claim）") from exc
    if not url or not token:
        raise ConfigError("缺少 GATEWAY_URL 或 GATEWAY_TOKEN，請先 make claim（或 uv run hw claim）")
    request = Request(
        url.rstrip("/") + "/v1/chat/completions",
        data=json.dumps({"model": model_alias, "messages": messages}, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with _one_request(url, token):
            with urlopen(request, timeout=45) as response:
                payload = json.load(response)
    except HTTPError as exc:
        if exc.code == 401:
            raise ConfigError("token 過期或無效，請重新 make claim（或 uv run hw claim）") from exc
        # 429，或帶 Retry-After 的 503（伺服器排隊已滿的暫時性狀況）都視為可重試；沒有 Retry-After 的 503（暫停、設定錯誤）仍是明確錯誤。
        if exc.code == 429 or (exc.code == 503 and exc.headers.get("Retry-After")):
            value = exc.headers.get("Retry-After")
            try:
                retry_after = float(value) if value is not None else None
            except ValueError:
                retry_after = None
            raise RateLimited(f"講師 gateway 請求遭到限速：{_detail(exc)}", retry_after=retry_after) from exc
        if exc.code == 402:
            raise BudgetExceeded(tenant) from exc
        raise ProviderError(f"講師 gateway 回傳 HTTP {exc.code}：{_detail(exc)}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ProviderError("無法連線講師 gateway") from exc
    except (ValueError, UnicodeError) as exc:
        raise ProviderError("講師 gateway 回應不是有效 JSON") from exc
    try:
        text = payload["choices"][0]["message"]["content"]
        usage = payload["usage"]
        model = payload["model"]
        input_tokens = usage["prompt_tokens"]
        output_tokens = usage["completion_tokens"]
        cost = payload["x_cost_usd"]
        if not isinstance(text, str) or not text or not isinstance(model, str):
            raise ValueError("invalid completion")
        if type(input_tokens) is not int or type(output_tokens) is not int or input_tokens < 0 or output_tokens < 0:
            raise ValueError("invalid usage")
        if not isinstance(cost, (int, float)) or isinstance(cost, bool) or not 0 <= cost < float("inf"):
            raise ValueError("invalid cost")
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ProviderError("講師 gateway 回應格式錯誤") from exc
    truncated = payload.get("x_truncated", False)
    if not isinstance(truncated, bool):
        raise ProviderError("講師 gateway 回應格式錯誤：x_truncated")
    return Result(text, "remote", model, input_tokens, output_tokens, float(cost), truncated=truncated)
