import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from report.make_report import load_usage, render_report
from tools.gen_sample_usage import sample_usage


ROOT = Path(__file__).resolve().parents[1]


def usage(**changes):
    record = {
        "ts": "2026-01-01T00:00:00Z",
        "tenant": "alpha",
        "alias": "mock",
        "provider": "mock",
        "model": "mock-demo",
        "input_tokens": 10,
        "output_tokens": 5,
        "cost_usd": 0.001,
        "cached": False,
        "fallback_used": False,
        "status": "ok",
    }
    return record | changes


def test_report_totals_groups_ranking_errors_and_escaping():
    records = [
        usage(tenant="alpha", provider="mock", cost_usd=0.001),
        usage(tenant="beta", provider="gemini", cost_usd=0.005, fallback_used=True),
        usage(tenant="<script>alert(1)</script>", cost_usd=0.0, cached=True, status="error:RateLimited"),
        usage(cost_usd=0.002),
        usage(cost_usd=0.003),
        usage(cost_usd=0.004),
        usage(cost_usd=0.006),
    ]
    html = render_report(records)
    assert "<strong>7</strong>" in html
    assert "<strong>105</strong>" in html
    assert "<strong>$0.021000</strong>" in html
    assert "快取命中 (cached)<strong>1</strong>" in html
    assert "備援使用 (fallback)<strong>1</strong>" in html
    assert "<h2>錯誤請求 (1)</h2>" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>" not in html
    assert "依 Tenant 分組" in html and "依 Provider 分組" in html
    assert "<td>beta</td><td>1</td><td>15</td><td>$0.005000</td>" in html
    top_five = html.split("成本最高的前 5 筆請求</h2>", 1)[1].split("</section>", 1)[0]
    assert top_five.count("<tr>") == 6
    assert top_five.index("$0.006000") < top_five.index("$0.005000")
    assert "$0.001000" not in top_five


def test_empty_report():
    html = render_report([])
    assert "<strong>0</strong>" in html
    assert "<strong>$0.000000</strong>" in html
    assert html.count("無資料") == 4


def test_load_usage_rejects_bad_lines(tmp_path):
    path = tmp_path / "usage.jsonl"
    path.write_text(json.dumps(usage()) + "\n\n" + '{"cost_usd": "bad"}\n', encoding="utf-8")
    with pytest.raises(ValueError, match=r"usage.jsonl:3:"):
        load_usage(path)


def test_generator_and_report_cli(tmp_path):
    usage_path = tmp_path / "usage.jsonl"
    report_path = tmp_path / "report.html"
    generated = subprocess.run(
        [sys.executable, "-m", "tools.gen_sample_usage", "--output", str(usage_path)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert generated.returncode == 0, generated.stderr
    assert load_usage(usage_path) == sample_usage()
    records = sample_usage()
    assert len({record["tenant"] for record in records}) >= 2
    assert max(record["cost_usd"] for record in records) > 20 * sorted(
        record["cost_usd"] for record in records
    )[-2]
    assert any(record["cached"] for record in records)
    assert any(record["fallback_used"] for record in records)
    assert any(record["status"].startswith("error") for record in records)

    result = subprocess.run(
        [sys.executable, "-m", "report.make_report", "--output", str(report_path)],
        cwd=ROOT, env={**os.environ, "USAGE_LOG": str(usage_path), "PYTHONIOENCODING": "utf-8"},
        capture_output=True, text=True, encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr
    html = report_path.read_text(encoding="utf-8")
    assert html.startswith("<!doctype html>")
    assert "成本最高的前 5 筆請求" in html
    assert "0.003384" in html
    assert "http://" not in html and "https://" not in html
    assert subprocess.run(
        [sys.executable, "-m", "tools.gen_sample_usage", "--output", str(usage_path)],
        cwd=ROOT, capture_output=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    ).returncode != 0


def test_missing_log_does_not_create_report(tmp_path):
    output = tmp_path / "report.html"
    result = subprocess.run(
        [sys.executable, "-m", "report.make_report", "--input", str(tmp_path / "missing.jsonl"),
         "--output", str(output)],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode != 0
    assert "無法產生報表" in result.stderr
    assert not output.exists()
