"""選配 Gemini provider；未安裝 extra 時不影響 mock。"""

import os

from gateway.llm import ConfigError, ProviderError, RateLimited, Result
from gateway.pricing import MAX_OUTPUT_TOKENS, cost_usd

MODELS = {"fast": "gemini-3.1-flash-lite", "smart": "gemini-3.8-flash"}


def chat(messages: list[dict], model: str) -> Result:
    if model != MODELS["fast"] and not (os.getenv("INSTRUCTOR") == "1" and model == MODELS["smart"]):
        raise ConfigError("學員僅可使用 fast（gemini-3.1-flash-lite）")
    key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not key:
        raise ConfigError("缺少 GEMINI_API_KEY 或 GOOGLE_API_KEY")
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise ConfigError("缺少 google-genai，請安裝 gemini extra") from exc

    try:
        client = genai.Client(api_key=key)
    except Exception as exc:
        raise ConfigError("Gemini client 初始化失敗") from exc
    contents = [
        types.Content(
            role="model" if message.get("role") == "assistant" else "user",
            parts=[types.Part.from_text(text=str(message.get("content", "")))],
        )
        for message in messages if message.get("role") != "system"
    ]
    system_instruction = "\n".join(
        str(message.get("content", "")) for message in messages if message.get("role") == "system"
    )
    try:
        response = client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction or None,
                max_output_tokens=MAX_OUTPUT_TOKENS,
                thinking_config=types.ThinkingConfig(
                    thinking_budget=0 if model == MODELS["fast"] else None,
                    thinking_level=types.ThinkingLevel.LOW if model == MODELS["smart"] else None,
                )
            ),
        )
    except Exception as exc:
        if getattr(exc, "code", None) == 429 or getattr(exc, "status_code", None) == 429:
            headers = getattr(getattr(exc, "response", None), "headers", {})
            retry_after = headers.get("Retry-After") if headers else None
            try:
                delay = float(retry_after) if retry_after is not None else None
            except ValueError:
                delay = None
            raise RateLimited("Gemini 回傳 429", retry_after=delay) from exc
        raise ProviderError(f"Gemini 請求失敗：{exc.__class__.__name__}") from exc
    text = response.text
    if not text:
        raise ProviderError("Gemini 回傳空內容")
    usage = response.usage_metadata
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    thoughts_tokens = int(getattr(usage, "thoughts_token_count", 0) or 0)
    output_tokens += thoughts_tokens
    return Result(
        text=text,
        provider="gemini",
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd(model, input_tokens, output_tokens),
        thoughts_tokens=thoughts_tokens,
        truncated=str(getattr(response.candidates[0], "finish_reason", "")).upper().endswith("MAX_TOKENS")
        if getattr(response, "candidates", None) else False,
    )
