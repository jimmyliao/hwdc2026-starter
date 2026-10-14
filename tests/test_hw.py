import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from hw import cli
from hw.console import symbol


ROOT = Path(__file__).resolve().parents[1]


def invoke(*arguments: str, **environment: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "hw", *arguments], cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "cp950", "PYTHONUTF8": "0", **environment},
        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30,
        check=False,
    )


def test_command_entrypoints_and_legacy_encoding():
    models = invoke("models")
    assert models.returncode == 0 and "mock:" in models.stdout
    for level, total in (("L1", 6), ("L2", 3)):
        result = invoke("check", level)
        assert result.returncode in (0, 1)
        assert f"{level}：" in result.stdout and f"/{total}" in result.stdout
        assert "UnicodeEncodeError" not in result.stderr
    direct = subprocess.run(
        [sys.executable, "-m", "checks.l1"], cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "cp950", "PYTHONUTF8": "0"},
        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30,
        check=False,
    )
    assert direct.returncode in (0, 1) and "L1：" in direct.stdout
    assert "UnicodeEncodeError" not in direct.stderr


def test_doctor_offline_and_nonfatal_network(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GATEWAY_TOKEN", raising=False)
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="uv 0.11.6"))
    assert cli.main(["doctor"]) == 0
    assert "PASS" in capsys.readouterr().out
    monkeypatch.setenv("GATEWAY_URL", "http://127.0.0.1:1")
    assert cli.main(["doctor"]) == 0
    assert "FAIL 選用 gateway" in capsys.readouterr().out


def test_doctor_missing_uv_fails(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    assert cli.main(["doctor"]) == 1
    assert "FAIL uv" in capsys.readouterr().out


def test_commands_dispatch_without_shell(monkeypatch):
    from agent import main as agent
    from scenarios import runner
    from report import make_report
    from submit import claim, submit

    calls = []
    for module in (agent, runner, make_report, claim, submit):
        monkeypatch.setattr(module, "main", lambda module=module: (calls.append((module.__name__, sys.argv[:])), 0)[1])
    for arguments in (
        ["ask", "你好"], ["models"], ["run"], ["report"],
        ["claim", "--env"], ["submit", "--dry-run"],
    ):
        assert cli.main(arguments) == 0
    assert [name for name, _ in calls] == [
        "agent.main", "agent.main", "scenarios.runner", "report.make_report",
        "submit.claim", "submit.submit",
    ]
    assert calls[-1][1][1:] == ["--require-ready", "--dry-run"]
    assert calls[2][1] == ["run"]


def test_symbol_falls_back_when_console_cannot_encode(monkeypatch):
    monkeypatch.setattr(sys, "stdout", SimpleNamespace(encoding="ascii"))
    assert symbol(True) == "[OK]"
    assert symbol(False) == "[X]"


def test_check_exit_code_and_prefix_match_score():
    """不論目前是出廠或已修復：滿分才是 0，其餘非零；輸出前綴固定為「Lx：n/total」。"""
    import re

    for level, total in (("L1", 6), ("L2", 3)):
        result = invoke("check", level)
        match = re.search(rf"^{level}：(\d+)/{total}$", result.stdout, flags=re.MULTILINE)
        assert match, result.stdout
        assert result.returncode == (0 if int(match.group(1)) == total else 1)


def test_makefile_targets_are_thin_wrappers_for_hw():
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    commands = {"ask", "models", "check", "run", "report", "claim", "submit", "test", "doctor"}
    used = set()
    for line in makefile.splitlines():
        if line.startswith("\tuv run hw "):
            used.add(line.split()[3])
    assert used == commands - {"doctor"} and used <= commands
    help_text = invoke("--help").stdout
    for command in commands:
        assert command in help_text


def test_hw_rejects_unknown_arguments_and_levels():
    assert invoke("check", "L9").returncode == 2
    assert invoke("models", "--bogus").returncode == 2
    assert invoke().returncode == 2


def test_doctor_reports_pass_and_remedy_for_each_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""))
    (tmp_path / ".env").mkdir()  # .env 是資料夾 -> 應 FAIL 並指出修法
    assert cli.main(["doctor"]) == 1
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert all(line.startswith(("PASS ", "FAIL ")) for line in lines)
    failures = [line for line in lines if line.startswith("FAIL ")]
    assert len(failures) == 2  # uv 與 .env
    assert "https://docs.astral.sh/uv/" in output
    assert "copy .env.example .env" in output and "cp .env.example .env" in output
    assert "make setup" not in output  # 相依套件在測試環境已安裝


def test_doctor_notes_legacy_console_encoding(tmp_path, monkeypatch, capsys):
    from hw import console

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("GATEWAY_URL", raising=False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="uv 0.11.6"))
    monkeypatch.setattr(console, "ORIGINAL_ENCODING", "cp950")
    monkeypatch.setattr(console, "configure_console", lambda: None)
    monkeypatch.setattr(cli, "configure_console", lambda: None)
    assert cli.main(["doctor"]) == 0
    assert "PASS UTF-8 輸出（主控台為 cp950" in capsys.readouterr().out


def test_hw_test_skips_server_tests_without_extra(tmp_path):
    """乾淨環境（未裝 fastapi）收集階段不得失敗：conftest 會略過 server 測試。"""
    spec = importlib.util.spec_from_file_location("plain_conftest", Path(__file__).with_name("conftest.py"))
    plain = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(plain)
    assert plain.SERVER_TEST_FILES == ("test_server.py", "test_server_security.py")
    assert isinstance(plain.collect_ignore, list)
    original = importlib.util.find_spec
    try:
        importlib.util.find_spec = lambda name, *a, **k: None if name in {"fastapi", "httpx"} else original(name, *a, **k)
        assert plain.server_extra_installed() is False
    finally:
        importlib.util.find_spec = original
