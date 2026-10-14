"""統一命令列文字編碼及狀態符號。"""

import sys

# 設定前的主控台編碼（doctor 用來提醒學員；設定後一律是 utf-8）。
ORIGINAL_ENCODING: str | None = None


def configure_console() -> None:
    global ORIGINAL_ENCODING
    if ORIGINAL_ENCODING is None:
        ORIGINAL_ENCODING = getattr(sys.stdout, "encoding", None) or "unknown"
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


def symbol(success: bool) -> str:
    mark = "✓" if success else "✗"
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        mark.encode(encoding)
    except (UnicodeError, LookupError):
        return "[OK]" if success else "[X]"
    return mark
