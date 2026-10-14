"""專案外的課堂 token 存放位置。"""

import os
from pathlib import Path


def token_path() -> Path:
    return Path.home() / ".hwdc26" / "token"


def read_token() -> str:
    token = os.getenv("GATEWAY_TOKEN", "").strip()
    if token:
        return token
    path = token_path()
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()
