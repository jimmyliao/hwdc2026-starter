"""向講師服務領取限時 token。"""

import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv, set_key
from gateway.token_store import read_token, token_path


def claim(env_path: Path | None = None) -> tuple[str, str]:
    url = os.getenv("CLAIM_URL", "").strip()
    if not url:
        gateway = os.getenv("GATEWAY_URL", "").strip()
        if not gateway:
            raise ValueError("請設定 CLAIM_URL 或 GATEWAY_URL")
        url = gateway.rstrip("/") + "/claim"
    code = os.getenv("CLASS_CODE", "").strip()
    nickname = os.getenv("NICKNAME", "").strip()
    if not code or not nickname:
        raise ValueError("請設定 CLASS_CODE 與 NICKNAME")
    headers = {"Content-Type": "application/json"}
    try:
        existing = read_token()
    except OSError:
        existing = ""
    if existing:
        headers["Authorization"] = f"Bearer {existing}"
    request = Request(
        url,
        data=json.dumps({"class_code": code, "nickname": nickname}, ensure_ascii=False).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"領取 token 失敗：HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("無法連線講師服務，請檢查 CLAIM_URL 或 GATEWAY_URL") from exc
    except (ValueError, UnicodeError) as exc:
        raise RuntimeError("講師服務回應不是有效 JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("token"), str) or not payload["token"] or not payload.get("expires_at"):
        raise RuntimeError("講師服務回應缺少 token 或 expires_at")
    token = payload["token"]
    path = token_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token + "\n")
    if os.name == "posix":
        path.chmod(0o600)
    if env_path is not None:
        if not env_path.exists():
            fd = os.open(env_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        if not set_key(str(env_path), "GATEWAY_TOKEN", token):
            raise RuntimeError("無法寫入 .env")
    return token, str(payload["expires_at"])


def main() -> None:
    parser = argparse.ArgumentParser(description="領取講師 gateway token")
    parser.add_argument("--env", action="store_true", help="改存入專案 .env（不建議）")
    args = parser.parse_args()
    load_dotenv(".env")
    try:
        env_file = Path(".env")
        write_env = args.env or os.getenv("CLAIM_WRITE_ENV") == "1" or env_file.is_file()
        token, expires = claim(env_file if write_env else None)
    except (ValueError, RuntimeError, OSError) as exc:
        raise SystemExit(str(exc)) from exc
    if Path(".env").is_file():
        for key in ("GATEWAY_URL", "CLASS_CODE", "NICKNAME"):
            if val := os.getenv(key, "").strip():
                set_key(".env", key, val)
    visible = min(24, max(0, len(token) - 5))
    print(f"已領取 token：{token[:visible]}…{token[-4:]}；到期日：{expires}")
    if write_env:
        print("token 已寫入 .env；請勿貼到 GitHub 或截圖")
    else:
        print("token 已存於專案外，請勿貼到 GitHub 或截圖")


if __name__ == "__main__":
    from hw.console import configure_console

    configure_console()
    main()
