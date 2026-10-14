"""mock 為教學用固定價格；Gemini 為官方 Standard 付費價，每百萬 token 的美元價格。

來源：ops/pricing-research.md；3.8 Flash 促銷價格只適用至 2026-12-31。
"""

import math
import unicodedata

MAX_OUTPUT_TOKENS = 256
SHORT_ANSWER_INSTRUCTION = "請簡短回答，不超過三句"

PRICES_USD_PER_MILLION = {
    "mock": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.8-flash": (0.75, 3.75),
}


def estimate_tokens(text: str) -> int:
    cjk = sum(
        unicodedata.east_asian_width(char) in {"W", "F"}
        and unicodedata.category(char).startswith(("L", "N"))
        for char in text
    )
    return cjk + math.ceil((len(text) - cjk) / 4)


def worst_cost_usd(model: str, messages: list[dict]) -> float:
    input_tokens = estimate_tokens(SHORT_ANSWER_INSTRUCTION) + sum(
        estimate_tokens(str(message.get("content", ""))) for message in messages
    )
    return cost_usd(model, input_tokens, MAX_OUTPUT_TOKENS)


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = PRICES_USD_PER_MILLION[model]
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000
