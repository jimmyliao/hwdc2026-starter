"""L2 三項互不依賴的離線行為檢查。"""

import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hw.console import symbol
from scenarios import tasks
from gateway.pricing import estimate_tokens, SHORT_ANSWER_INSTRUCTION


ROOT = Path(__file__).resolve().parent.parent


def check_input_limit() -> bool:
    seen = []

    def fake_ask(question, tenant=None):
        seen.append(question)
        return "ok"

    with patch.object(tasks, "ask", fake_ask):
        tasks.run_task("超長背景 " * 2400 + "請摘要")
    return len(seen) == 1 and estimate_tokens(seen[0]) + estimate_tokens(SHORT_ANSWER_INSTRUCTION) <= tasks.MAX_INPUT_TOKENS


def check_tenant_budget() -> bool:
    from gateway import llm

    with tempfile.TemporaryDirectory() as directory:
        usage = Path(directory) / "usage.jsonl"

        def fake_ask(question, tenant=None):
            return llm.chat([{"role": "user", "content": question}], alias="mock", tenant=tenant or "default")

        environment = {
            "PROVIDER": "mock",
            "FALLBACK": "",
            "MOCK_FAIL": "",
            "RPM_LIMIT": "1000",
            "BUDGETS_FILE": str(ROOT / "budgets.json"),
            "USAGE_LOG": str(usage),
            "CACHE_DIR": str(Path(directory) / "cache"),
        }
        with patch.dict(os.environ, environment), patch.object(tasks, "ask", fake_ask):
            tasks.run_task("第一題", tenant="team-a")
            tasks.run_task("第二題", tenant="team-b")
            exceeded = False
            try:
                tasks.run_task("第三題", tenant="team-b")
            except llm.BudgetExceeded:
                exceeded = True
        tenants = {json.loads(line)["tenant"] for line in usage.read_text(encoding="utf-8").splitlines()}
        if not (tenants >= {"team-a", "team-b"} and exceeded):
            return False
        from scenarios import runner

        with patch.dict(os.environ, {
            "PROVIDER": "mock", "FALLBACK": "", "MOCK_FAIL": "",
            "RPM_LIMIT": "1000",
            "BUDGETS_FILE": "", "USAGE_LOG": str(usage),
            "CACHE_DIR": str(Path(directory) / "runner-cache"),
        }):
            result = runner.run(runner.SAMPLE_QUESTIONS, Path(directory) / "answers.json", usage)
        return len(result["answers"]) == 10 and all(isinstance(item["answer"], str) for item in result["answers"])


def check_fallback() -> bool:
    def fake_ask(question, tenant=None):
        if os.environ.get("MOCK_FAIL") == "429" and os.environ.get("FALLBACK") != "fast,mock":
            raise RuntimeError("主要供應者回傳 429，卻未使用 fallback")
        return SimpleNamespace(text="已回答", fallback_used=True)

    with patch.dict(os.environ, {"PROVIDER": "fast", "FALLBACK": "fast,mock", "MOCK_FAIL": "429"}):
        with patch.object(tasks, "ask", fake_ask):
            result = tasks.run_with_fallback("主要供應者失敗時仍須回答")
    return bool(result.text) and result.fallback_used


CHECKS = (
    ("單次請求 token 上限", check_input_limit, "請用 gateway.pricing.estimate_tokens 限制輸入；中文每字算 1 token，並預留 SHORT_ANSWER_INSTRUCTION 的 token 數，保留題目末尾。"),
    ("多租戶 usage 與超額預算", check_tenant_budget, "請在 run_task 傳入 tenant；agent.ask 回傳 Result，且十題 runner 須能跑完。"),
    ("429 時仍可透過 fallback 回答", check_fallback, "請保留 FALLBACK 設定的別名鏈。"),
)


def main() -> int:
    passed = 0
    for title, check, hint in CHECKS:
        try:
            success = check()
        except Exception as exc:
            success = False
            hint = f"{hint}（{type(exc).__name__}: {exc}）"
        passed += success
        print(f"{symbol(success)} {title}" + ("" if success else f"：{hint}"))
    print(f"L2：{passed}/3")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    import sys
    from hw.console import configure_console

    configure_console()
    sys.exit(main())
