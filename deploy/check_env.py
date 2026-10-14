"""啟動前檢查必要環境變數，不輸出完整值。"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
KEY_LIKE = re.compile(r"(?:AIza|sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{12,}\Z")
SENSITIVE = re.compile(r"(?:KEY|SECRET|TOKEN|PASSWORD|CREDENTIAL)", re.IGNORECASE)
MIN_LENGTH = {"CLASS_CODE": 10, "SERVER_SECRET": 32, "ADMIN_TOKEN": 32}


def required_from_config(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"無法讀取設定檔 {path}：{type(exc).__name__}") from exc
    names = data.get("required") if isinstance(data, dict) else data
    if not isinstance(names, list):
        raise ValueError("JSON 設定必須是變數名稱陣列，或含 required 陣列的物件")
    return names


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="啟動前檢查必要環境變數")
    parser.add_argument("names", nargs="*", help="必要環境變數名稱")
    parser.add_argument("--config", type=Path, help="JSON 設定檔（required 陣列）")
    args = parser.parse_args(argv)

    try:
        names = args.names + (required_from_config(args.config) if args.config else [])
        if not names or any(not isinstance(name, str) or not NAME.fullmatch(name) for name in names):
            raise ValueError("至少指定一個合法的環境變數名稱")
    except ValueError as exc:
        parser.error(str(exc))

    names = list(dict.fromkeys(names))
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        print(f"環境預檢失敗：缺少 {', '.join(missing)}", file=sys.stderr)
        return 1

    weak = [f"{name} 長度 {len(os.environ[name])}，至少需要 {minimum}"
            for name, minimum in MIN_LENGTH.items() if name in names and len(os.environ[name]) < minimum]
    if weak and os.environ.get("ALLOW_WEAK_SECRETS") != "1":
        print("環境預檢失敗：設定強度不足（" + "；".join(weak) + "）", file=sys.stderr)
        return 1

    for name in names:
        value = os.environ[name]
        if SENSITIVE.search(name) or KEY_LIKE.fullmatch(value) or name in MIN_LENGTH:
            print(f"{name}：已設定（長度 {len(value)}）")
        else:
            print(f"{name}：已設定")
    print("環境預檢通過")
    return 0


if __name__ == "__main__":
    sys.exit(main())
