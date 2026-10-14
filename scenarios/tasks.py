"""L2 待修正的 agent 呼叫路徑。"""

import os


MAX_INPUT_TOKENS = 1280


def truncate(question: str, max_tokens: int = MAX_INPUT_TOKENS) -> str:
    # TODO(L2): 超長內容必須在呼叫 agent 前限制 token 數。
    return question


def ask(question: str, tenant: str | None = None):
    from agent.main import ask as agent_ask
    return agent_ask(question, tenant=tenant or "default")


def run_task(question: str, tenant: str = "default"):
    question = truncate(question)
    # TODO(L2): 把任務的 tenant 傳給 agent。
    return ask(question)


def run_with_fallback(question: str, tenant: str = "default"):
    configured = os.environ.get("FALLBACK")
    try:
        # TODO(L2): 不要覆蓋使用者設定的 fallback 鏈。
        os.environ["FALLBACK"] = os.environ.get("PROVIDER", "mock")
        return run_task(question, tenant=tenant)
    finally:
        if configured is None:
            os.environ.pop("FALLBACK", None)
        else:
            os.environ["FALLBACK"] = configured
