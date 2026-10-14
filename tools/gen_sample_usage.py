"""產生多 tenant、成本突增與故障情境的離線示範用量。"""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from gateway.pricing import MAX_OUTPUT_TOKENS, cost_usd


def sample_usage() -> list[dict]:
    start = datetime(2026, 1, 1, 9, tzinfo=timezone.utc)
    records = []
    for index in range(12):
        tenant = ("workshop-a", "workshop-b", "workshop-c")[index % 3]
        spike = index == 7
        cached = index == 4
        fallback = index in (6, 10)
        error = index == 9
        input_tokens = 12_000 if spike else 0 if cached else 120 + 20 * index
        output_tokens = MAX_OUTPUT_TOKENS if spike else 0 if cached or error else 40 + 5 * index
        cost = 0 if cached or error else cost_usd("gemini-3.1-flash-lite" if spike else "mock", input_tokens, output_tokens)
        records.append({
            "ts": (start + timedelta(minutes=index)).isoformat(),
            "tenant": tenant,
            "alias": "fast" if spike else "mock",
            "provider": "gemini" if spike else "mock",
            "model": "gemini-3.1-flash-lite" if spike else "mock",
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost,
            "cached": cached,
            "fallback_used": fallback,
            "status": "error:RateLimited" if error else "ok",
        })
    return records


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="產生可供報表、投影片與截圖使用的示範 usage JSONL")
    parser.add_argument("--output", type=Path, default=Path("usage.jsonl"))
    args = parser.parse_args(argv)
    try:
        with args.output.open("x", encoding="utf-8") as target:
            for record in sample_usage():
                target.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        parser.exit(1, f"無法建立示範用量（不覆寫既有檔案）：{exc}\n")
    print(f"已產生示範用量：{args.output}")
    return 0


if __name__ == "__main__":
    from hw.console import configure_console

    configure_console()
    raise SystemExit(main())
