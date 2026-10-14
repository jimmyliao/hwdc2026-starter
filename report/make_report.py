"""將 gateway 的 usage JSONL 轉為離線可讀的單檔報表。"""

import argparse
from collections import defaultdict
from html import escape
import json
import os
from pathlib import Path


def load_usage(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("必須是 JSON 物件")
                for field in ("ts", "tenant", "alias", "provider", "model", "status"):
                    if not isinstance(record.get(field), str):
                        raise ValueError(f"{field} 必須是字串")
                if record["status"] != "ok" and not record["status"].startswith("error:"):
                    raise ValueError("status 必須是 ok 或 error:<Class>")
                for field in ("input_tokens", "output_tokens"):
                    value = record.get(field)
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ValueError(f"{field} 必須是非負整數")
                cost = record.get("cost_usd")
                if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not 0 <= cost < float("inf"):
                    raise ValueError("cost_usd 必須是有限的非負數")
                for field in ("cached", "fallback_used"):
                    if not isinstance(record.get(field), bool):
                        raise ValueError(f"{field} 必須是布林值")
                records.append(record)
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return records


def h(value: object) -> str:
    return escape(str(value), quote=True)


def money(value: float) -> str:
    return f"${value:,.6f}"


def table(headers: tuple[str, ...], rows: list[tuple[object, ...]]) -> str:
    head = "".join(f"<th scope=\"col\">{h(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{h(cell)}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    if not rows:
        body = f'<tr><td colspan="{len(headers)}">無資料</td></tr>'
    return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def grouped(records: list[dict], key: str) -> list[tuple[object, ...]]:
    groups = defaultdict(lambda: [0, 0, 0.0])
    for record in records:
        item = groups[record[key]]
        item[0] += 1
        item[1] += record["input_tokens"] + record["output_tokens"]
        item[2] += record["cost_usd"]
    return [
        (name, count, f"{tokens:,}", money(cost))
        for name, (count, tokens, cost) in sorted(groups.items())
    ]


def request_rows(records: list[dict]) -> list[tuple[object, ...]]:
    return [
        (
            record["ts"],
            record["tenant"],
            record["alias"],
            record["provider"],
            record["model"],
            f'{record["input_tokens"] + record["output_tokens"]:,}',
            money(record["cost_usd"]),
            record["status"],
        )
        for record in records
    ]


def render_report(records: list[dict]) -> str:
    tokens = sum(record["input_tokens"] + record["output_tokens"] for record in records)
    cost = sum(record["cost_usd"] for record in records)
    cached = sum(record["cached"] for record in records)
    fallback = sum(record["fallback_used"] for record in records)
    errors = [record for record in records if record["status"].startswith("error:")]
    expensive = sorted(records, key=lambda record: record["cost_usd"], reverse=True)[:5]
    group_headers = ("名稱", "請求數", "Token 數", "成本 (USD)")
    request_headers = ("時間", "Tenant", "別名", "Provider", "模型", "Token 數", "成本 (USD)", "狀態")

    return f"""<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>用量與成本報表</title>
<style>
:root {{ color-scheme: light dark; font-family: system-ui, sans-serif; }}
body {{ max-width: 1100px; margin: auto; padding: 1rem 1.5rem 3rem; background: Canvas; color: CanvasText; line-height: 1.5; }}
h1, h2 {{ line-height: 1.3; }}
.cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: .75rem; }}
.card {{ border: 1px solid GrayText; border-radius: .5rem; padding: .8rem; }}
.card strong {{ display: block; font-size: 1.4rem; overflow-wrap: anywhere; }}
.table-wrap {{ overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ text-align: left; padding: .55rem; border-bottom: 1px solid GrayText; overflow-wrap: anywhere; }}
th {{ font-weight: 700; }}
</style>
</head>
<body>
<main>
<h1>用量與成本報表</h1>
<div class="cards">
<div class="card">總請求<strong>{len(records):,}</strong></div>
<div class="card">總 Token<strong>{tokens:,}</strong></div>
<div class="card">總成本 (USD)<strong>{money(cost)}</strong></div>
<div class="card">快取命中 (cached)<strong>{cached:,}</strong></div>
<div class="card">備援使用 (fallback)<strong>{fallback:,}</strong></div>
</div>
<section><h2>依 Tenant 分組</h2>{table(group_headers, grouped(records, "tenant"))}</section>
<section><h2>依 Provider 分組</h2>{table(group_headers, grouped(records, "provider"))}</section>
<section><h2>成本最高的前 5 筆請求</h2>{table(request_headers, request_rows(expensive))}</section>
<section><h2>錯誤請求 ({len(errors):,})</h2>{table(request_headers, request_rows(errors))}</section>
</main>
</body>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="將 USAGE_LOG 轉成單檔 HTML 報表")
    parser.add_argument("--input", type=Path, default=Path(os.getenv("USAGE_LOG", "./usage.jsonl")))
    parser.add_argument("--output", type=Path, default=Path("report.html"))
    args = parser.parse_args(argv)
    try:
        records = load_usage(args.input)
        args.output.write_text(render_report(records), encoding="utf-8")
    except (OSError, ValueError) as exc:
        parser.exit(1, f"無法產生報表：{exc}\n")
    print(f"已產生報表：{args.output}")
    return 0


if __name__ == "__main__":
    from hw.console import configure_console

    configure_console()
    raise SystemExit(main())
