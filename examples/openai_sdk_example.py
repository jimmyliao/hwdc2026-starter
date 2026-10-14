"""選配 SDK 示範：明確執行時 mock 為離線，fast 會使用真實上游。"""

import os

from gateway.token_store import read_token


def main() -> None:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise SystemExit("缺少 openai；請在 starter/ 執行 uv run --extra examples python examples/openai_sdk_example.py") from exc
    url = os.getenv("GATEWAY_URL", "").strip()
    token = read_token()
    if not url or not token:
        raise SystemExit("缺少 GATEWAY_URL 或課堂 token，請先 make claim（或 uv run hw claim）")
    client = OpenAI(base_url=url.rstrip("/") + "/v1", api_key=token)
    for model in ("mock", "fast"):
        response = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": "請用一句話說明 gateway"}],
        )
        print(f"{model}：{response.choices[0].message.content}")


if __name__ == "__main__":
    from hw.console import configure_console

    configure_console()
    main()
