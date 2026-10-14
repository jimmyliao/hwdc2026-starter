"""供離線工作坊使用的決定性 provider。"""

import os

from gateway.llm import ProviderError, RateLimited, Result
from gateway.pricing import MAX_OUTPUT_TOKENS, cost_usd, estimate_tokens


def chat(messages: list[dict]) -> Result:
    failure = os.getenv("MOCK_FAIL")
    if failure == "429":
        raise RateLimited("mock 回傳 429", retry_after=0)
    if failure == "timeout":
        raise ProviderError("mock 請求逾時")
    if failure == "empty":
        raise ProviderError("mock 回傳空內容")

    text = str(messages[-1].get("content", "")) if messages else ""
    response = f"Mock 回答：{text}"
    input_tokens = sum(estimate_tokens(str(message.get("content", ""))) for message in messages)
    truncated = estimate_tokens(response) > MAX_OUTPUT_TOKENS
    if truncated:
        response = response[:120] + " … " + response[-120:]
    output_tokens = estimate_tokens(response)
    return Result(
        text=response,
        provider="mock",
        model="mock",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd("mock", input_tokens, output_tokens),
        truncated=truncated,
    )
