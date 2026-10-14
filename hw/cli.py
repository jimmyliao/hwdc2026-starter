"""工作坊跨平台命令列介面。"""

import argparse
import importlib.util
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import dotenv_values

from hw import console
from hw.console import configure_console


@contextmanager
def module_args(name: str, args: list[str]):
    original = sys.argv
    sys.argv = [name, *args]
    try:
        yield
    finally:
        sys.argv = original


def doctor() -> int:
    results: list[tuple[str, bool, str]] = []
    results.append(("Python >= 3.11", sys.version_info >= (3, 11), "請安裝 Python 3.11 以上版本；uv 會自動下載：uv python install 3.12"))
    try:
        uv = subprocess.run(
            ["uv", "--version"], capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=5, check=False, env={**os.environ, "PYTHONUTF8": "1"},
        )
        uv_ok = uv.returncode == 0 and uv.stdout.strip().startswith("uv ")
        uv_version = uv.stdout.strip() if uv_ok else "找不到 uv"
    except (OSError, subprocess.TimeoutExpired):
        uv_ok, uv_version = False, "找不到 uv"
    results.append((f"uv 版本（{uv_version}）", uv_ok, "請安裝或更新 uv：https://docs.astral.sh/uv/"))
    original = (console.ORIGINAL_ENCODING or "unknown").lower().replace("-", "").replace("_", "")
    utf8_console = original in {"utf8", "utf8sig"}
    results.append((
        "UTF-8 輸出" + ("" if utf8_console else f"（主控台為 {console.ORIGINAL_ENCODING}，hw 已自動改用 UTF-8）"),
        True,
        "",
    ))
    missing = [name for name in ("dotenv", "rich", "pytest") if importlib.util.find_spec(name) is None]
    results.append((
        "相依套件已安裝" + (f"（缺少 {', '.join(missing)}）" if missing else ""),
        not missing, "請在專案目錄執行 make setup（或 uv sync），並用 uv run hw 啟動",
    ))
    try:
        with tempfile.TemporaryFile(dir=Path.cwd()):
            pass
        writable = True
    except OSError:
        writable = False
    results.append(("專案目錄可寫", writable, "請移至有寫入權限的專案目錄（不要放在唯讀或同步中的雲端資料夾）"))
    env_file = Path(".env")
    try:
        settings = dotenv_values(env_file) if env_file.exists() else {}
        env_ok = not env_file.exists() or env_file.is_file()
    except (OSError, UnicodeError):
        settings, env_ok = {}, False
    results.append((
        ".env 狀態" + ("（已存在）" if env_file.exists() else "（離線練習可不建立）"),
        env_ok, "請複製範本：macOS／Linux 用 cp .env.example .env，Windows 用 copy .env.example .env；並以 UTF-8 儲存",
    ))
    from gateway.token_store import read_token

    try:
        token_present = bool(read_token())
        token_ok = True
    except OSError:
        token_present, token_ok = False, False
    results.append((
        "課堂 token 狀態" + ("（已設定）" if token_present else "（離線練習不需 token）"),
        token_ok, "請檢查 ~/.hwdc26/token 的權限，或重新領取：make claim（或 uv run hw claim）",
    ))
    gateway = os.getenv("GATEWAY_URL") or settings.get("GATEWAY_URL")
    if gateway:
        try:
            with urlopen(gateway.rstrip("/") + "/health", timeout=5) as response:
                healthy = 200 <= response.status < 300
        except (URLError, TimeoutError, OSError, ValueError):
            healthy = False
        results.append((
            "選用 gateway /health", healthy,
            "請檢查 GATEWAY_URL 或網路；離線練習（make check／uv run hw check）不受影響",
        ))
    for name, passed, remedy in results:
        print(f"{'PASS' if passed else 'FAIL'} {name}；{'無需處理' if passed else remedy}")
    return 0 if all(passed for name, passed, _ in results if not name.startswith("選用 gateway")) else 1


def main(argv: list[str] | None = None) -> int:
    os.environ["PYTHONUTF8"] = "1"
    configure_console()
    parser = argparse.ArgumentParser(prog="hw", description="工作坊跨平台指令")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ask").add_argument("question")
    commands.add_parser("models")
    commands.add_parser("check").add_argument("level", choices=["L1", "L2"])
    for name in ("run", "report", "claim", "submit", "test", "doctor"):
        commands.add_parser(name)
    args, extra = parser.parse_known_args(argv)
    if extra and args.command not in {"claim", "submit", "report", "run", "test"}:
        parser.error(f"不支援的參數：{' '.join(extra)}")
    if args.command == "doctor":
        return doctor()
    if args.command == "test":
        import pytest

        os.environ["PROVIDER"] = "mock"
        return pytest.main(extra)
    if args.command in {"check", "run"}:
        os.environ["PROVIDER"] = "mock"
    if args.command in {"ask", "models"}:
        from agent.main import main as agent_main

        call_args = ["ask", args.question] if args.command == "ask" else ["models"]
        with module_args("agent.main", call_args):
            return agent_main()
    if args.command == "check":
        from checks import l1, l2

        return (l1 if args.level == "L1" else l2).main()
    if args.command == "run":
        from scenarios.runner import main as run_main

        function = run_main
    elif args.command == "report":
        from report.make_report import main as report_main

        function = report_main
    elif args.command == "claim":
        from submit.claim import main as claim_main

        function = claim_main
    else:
        from submit.submit import main as submit_main

        function = submit_main
        extra = ["--require-ready", *extra]
    with module_args(args.command, extra):
        function()
    return 0
