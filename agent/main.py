from __future__ import annotations

import argparse
import os
import sys

from gateway import llm

from agent import config


def ask(question: str, tenant: str | None = None) -> llm.Result:
    config.load_environment()
    messages = config.truncate([{"role": "user", "content": question}])
    # 批次練習明確指定 tenant，預設只走 mock；CLI 仍保留 L1 的預設別名練習。
    return llm.chat(
        messages,
        alias=os.getenv("PROVIDER", "mock") if tenant is not None else config.DEFAULT_ALIAS,
        tenant=tenant or os.getenv("TENANT", "default"),
    )


def cli_ask(question: str) -> int:
    try:
        result = ask(question)
    except llm.LLMError as exc:
        if os.getenv("PROVIDER") == "fast" and "API_KEY" in str(exc):
            print("今天請用 mock 或 class", file=sys.stderr)
        else:
            print("")
        return 0

    print(result.text)
    print(
        f"provider: {result.provider} | model: {result.model} | "
        f"tokens: {result.input_tokens} in / {result.output_tokens} out | "
        f"cost: ${result.cost_usd:.6f} USD"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 問答練習")
    commands = parser.add_subparsers(dest="command", required=True)
    ask_parser = commands.add_parser("ask", help="提出問題")
    ask_parser.add_argument("question", help="要詢問的問題")
    commands.add_parser("models", help="列出可用模型別名")
    args = parser.parse_args()

    if args.command == "ask":
        return cli_ask(args.question)

    for alias, model in llm.list_aliases().items():
        print(f"{alias}: {model}")
    return 0


if __name__ == "__main__":
    from hw.console import configure_console

    configure_console()
    sys.exit(main())
