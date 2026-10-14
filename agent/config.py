from pathlib import Path

from dotenv import load_dotenv


DEFAULT_ALIAS = "gemni-fast"


def load_environment() -> None:
    load_dotenv(".env")


def truncate(messages: list[dict]) -> list[dict]:
    # TODO(L2): 限制過長的上下文，避免單次請求耗費過多 token。
    return messages
