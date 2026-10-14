import importlib.util
import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from server.tokens import verify


ROOT = Path(__file__).resolve().parents[2]


def test_slips_issue_and_expiry(tmp_path):
    if not (ROOT / "tools" / "issue_slips.py").is_file():
        pytest.skip("獨立 starter 複本不包含講師工具")
    env = {**os.environ, "SERVER_SECRET": "test-only-secret", "TOKEN_EXPIRES_ON": "20261016"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "issue_slips.py"), "2", "--output", str(tmp_path)],
        capture_output=True, text=True, encoding="utf-8",
        env={**env, "PYTHONIOENCODING": "utf-8"}, check=True,
    )
    import csv
    with (tmp_path / "slips.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    import re

    nicknames = [row["nickname"] for row in rows]
    assert re.fullmatch(r"slip01-[a-z0-9]{6}", nicknames[0]) and re.fullmatch(r"slip02-[a-z0-9]{6}", nicknames[1])
    assert len(set(nicknames)) == 2
    html = (tmp_path / "slips.html").read_text(encoding="utf-8")
    for row in rows:
        token = row["token"]
        assert verify(token, "test-only-secret", now=datetime(2026, 10, 16, 23, 59, 59, tzinfo=timezone(timedelta(hours=8)))) == row["nickname"]
        assert token in html and token not in result.stdout
        with pytest.raises(ValueError, match="過期"):
            verify(token, "test-only-secret", now=datetime(2026, 10, 17, tzinfo=timezone(timedelta(hours=8))))


def test_scanner_masks_matches_and_ignores_files(tmp_path):
    if not (ROOT / "tools" / "scan_secrets.py").is_file():
        pytest.skip("獨立 starter 複本不包含講師工具")
    suspicious = tmp_path / "sample.txt"
    suspicious.write_text(
        "AIza" + "x" * 25 + "\n" + "sk-" + "z" * 25 + "\n"
        + "leapkey-hwdc26-" + "20261016-nick-signature\n", encoding="utf-8",
    )
    tool = str(ROOT / "tools" / "scan_secrets.py")
    result = subprocess.run(
        [sys.executable, tool, str(suspicious)], capture_output=True, text=True,
        encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 1
    assert all(f"{suspicious}:{line}:" in result.stderr for line in (1, 2, 3))
    assert "AIza" + "x" * 25 not in result.stderr
    assert "leapkey-hwdc26-" + "20261016" not in result.stderr
    (tmp_path / ".secretsignore").write_text("sample.txt\n", encoding="utf-8")
    allowed = subprocess.run(
        [sys.executable, tool, "sample.txt"], cwd=tmp_path, capture_output=True,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert allowed.returncode == 0
    teaching = subprocess.run(
        [sys.executable, tool, str(ROOT / "starter" / ".env.example")],
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert teaching.returncode == 1
    assert "AIzaSy" + "FAKE_EXAMPLE_KEY_DO_NOT_USE" not in teaching.stderr
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", "sample.txt"], check=True)
    staged = subprocess.run(
        [sys.executable, tool, "--staged"], cwd=tmp_path, capture_output=True,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert staged.returncode == 0  # 允許清單同樣適用暫存區
    (tmp_path / ".secretsignore").unlink()
    staged = subprocess.run(
        [sys.executable, tool, "--staged"], cwd=tmp_path, capture_output=True,
        text=True, encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert staged.returncode == 1 and "sample.txt:1:" in staged.stderr
    assert "AIza" + "x" * 25 not in staged.stderr


def test_openai_example_missing_extra(monkeypatch):
    path = Path(__file__).resolve().parents[1] / "examples" / "openai_sdk_example.py"
    spec = importlib.util.spec_from_file_location("sdk_example", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    import builtins
    original = builtins.__import__

    def without_openai(name, *args, **kwargs):
        if name == "openai":
            raise ImportError("missing optional extra")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_openai)
    with pytest.raises(SystemExit, match="uv run --extra examples"):
        module.main()
