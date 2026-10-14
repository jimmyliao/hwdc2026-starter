"""未安裝選配 server 依賴時，略過需要 fastapi 的測試（講師端與 CI 會安裝並執行）。"""

import importlib.util
from pathlib import Path


SERVER_TEST_FILES = ("test_server.py", "test_server_security.py")


def server_extra_installed() -> bool:
    return all(importlib.util.find_spec(name) is not None for name in ("fastapi", "httpx"))


collect_ignore = [] if server_extra_installed() else [
    str(Path(__file__).with_name(name)) for name in SERVER_TEST_FILES
]
