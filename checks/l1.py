"""六項互不依賴的 L1 離線檢查。"""

import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import textwrap

from hw.console import symbol


ROOT = Path(__file__).resolve().parents[1]
ALIASES = {"fast", "mock", "class"}
FAKE_GATEWAY = """
import os
import sys
import types

gateway = types.ModuleType("gateway")
llm = types.ModuleType("gateway.llm")
class LLMError(Exception):
    pass
llm.LLMError = LLMError
llm.list_aliases = lambda: {"fast": "fast-model", "mock": "mock-model", "class": "fast-model"}
def chat(messages, *, alias=None, tenant="default"):
    if MODE == "error":
        raise LLMError("設定錯誤：離線測試")
    if alias is not None and alias != os.environ["PROVIDER"]:
        raise AssertionError("未採用 PROVIDER")
    if tenant != "classroom":
        raise AssertionError("未傳遞 TENANT")
    return types.SimpleNamespace(
        text="離線回答", provider="mock", model="mock-model",
        input_tokens=2, output_tokens=3, cost_usd=0.000001,
        cached=False, fallback_used=False,
    )
llm.chat = chat
gateway.llm = llm
sys.modules["gateway"] = gateway
sys.modules["gateway.llm"] = llm
import runpy
sys.argv = ["agent.main", "ask", "離線問題"]
runpy.run_module("agent.main", run_name="__main__")
"""


def _environment() -> dict[str, str]:
    # 子程序只帶執行必要的環境，絕不讀取或傳遞使用者的 API key。
    return {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT),
        "PROVIDER": "mock",
        "TENANT": "classroom",
    }


def _run_agent(mode: str, provider: str = "mock") -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        script = f"MODE = {mode!r}\n" + FAKE_GATEWAY
        return subprocess.run(
            [sys.executable, "-c", script],
            cwd=directory,
            env={**_environment(), "PROVIDER": provider},
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=15,
            check=False,
        )


def _default_alias() -> bool:
    spec = importlib.util.spec_from_file_location("l1_agent_config", ROOT / "agent/config.py")
    if spec is None or spec.loader is None:
        return False
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, "DEFAULT_ALIAS", None) in ALIASES


def _example_provider() -> bool:
    content = (ROOT / ".env.example").read_text(encoding="utf-8")
    return bool(re.search(r"(?m)^PROVIDER\s*=", content)) and not re.search(r"(?m)^PROVDER\s*=", content)


def _project_dotenv() -> bool:
    with tempfile.TemporaryDirectory() as directory:
        project = Path(directory) / "project"
        (project / "agent").mkdir(parents=True)
        (project / "agent/config.py").write_text(
            (ROOT / "agent/config.py").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (project / ".env").write_text("PROVIDER=mock\n", encoding="utf-8")
        elsewhere = Path(directory) / "elsewhere"
        elsewhere.mkdir()
        script = textwrap.dedent(
            """
            import importlib.util
            import os
            import sys
            spec = importlib.util.spec_from_file_location("isolated_config", sys.argv[1])
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.load_environment()
            sys.exit(0 if os.environ.get("PROVIDER") == "mock" else 1)
            """
        )
        env = _environment()
        env.pop("PROVIDER")
        result = subprocess.run(
            [sys.executable, "-c", script, str(project / "agent/config.py")],
            cwd=elsewhere,
            env=env,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=15,
            check=False,
        )
        return result.returncode == 0


def _provider_selected() -> bool:
    results = [_run_agent("success", provider) for provider in ("fast", "mock", "class")]
    return all(result.returncode == 0 and "離線回答" in result.stdout for result in results)


def _error_visible() -> bool:
    result = _run_agent("error")
    return result.returncode != 0 and "設定錯誤" in result.stderr


def _secrets_safe() -> bool:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    return not re.search(r"AIzaSyFAKE", example, flags=re.IGNORECASE) and bool(
        re.search(r"(?m)^/?\.env\s*$", ignored)
    )


def main() -> int:
    checks = [
        ("模型別名可辨識", _default_alias, "預設別名必須是 hw models（make models）清單中的其中一個；相似但不在清單內的名稱不算。"),
        ("範例環境設定可用", _example_provider, "比較設定檔欄位與程式使用的環境變數。"),
        ("從其他目錄載入設定", _project_dotenv, "換個工作目錄再執行一次：.env 要用程式檔案的位置往上找（Path(__file__) 往上一層是專案根目錄），不能靠目前所在目錄。"),
        ("尊重 PROVIDER 選擇", _provider_selected, "用 PROVIDER=mock、PROVIDER=class 各執行一次，結果應隨之改變：把 PROVIDER 的值傳給 gateway，或不指定別名讓 gateway 自己讀取，兩種都可以。"),
        ("錯誤明確且非零結束", _error_visible, "讓 gateway 的失敗可由使用者和命令列察覺。"),
        ("範例與忽略規則安全", _secrets_safe, "檢查示範憑證及本機設定的版控規則。"),
    ]
    passed = 0
    for label, check, hint in checks:
        try:
            success = check()
        except (OSError, ImportError, subprocess.TimeoutExpired) as exc:
            print(f"{symbol(False)} {label}：檢查無法執行（{type(exc).__name__}）")
            continue
        if success:
            passed += 1
            print(f"{symbol(True)} {label}")
        else:
            print(f"{symbol(False)} {label}：{hint}")
    print(f"L1：{passed}/6")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    from hw.console import configure_console

    configure_console()
    sys.exit(main())
