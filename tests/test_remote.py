import json
import os
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest

from gateway import llm
from scenarios.runner import SAMPLE_QUESTIONS, load_questions
from submit import claim, submit


@pytest.fixture
def gateway_stub():
    requests = []
    routes = {}

    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            requests.append((self.command, self.path, dict(self.headers), body))
            status, data, headers = routes.get(self.path, (404, {"detail": "not found"}, {}))
            encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(encoded)

        do_GET = handle_request
        do_POST = handle_request

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", routes, requests
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


@pytest.fixture
def remote_env(tmp_path, monkeypatch, gateway_stub):
    url, _, _ = gateway_stub
    monkeypatch.setenv("GATEWAY_URL", url)
    monkeypatch.setenv("GATEWAY_TOKEN", "test-token")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("CLAIM_WRITE_ENV", raising=False)
    monkeypatch.setenv("USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("RPM_LIMIT", "1000")
    monkeypatch.setenv("MAX_RETRIES", "0")
    monkeypatch.setenv("REMOTE_LOCK_DIR", str(tmp_path))
    for key in ("FALLBACK", "BUDGETS_FILE", "CLAIM_URL", "GATEWAY_MODEL"):
        monkeypatch.delenv(key, raising=False)
    llm._requests.clear()


def test_remote_completion_usage_and_cache(remote_env, gateway_stub):
    _, routes, requests = gateway_stub
    routes["/v1/chat/completions"] = (200, {
        "choices": [{"message": {"content": "遠端回答"}}],
        "model": "gemini-2.5-flash",
        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        "x_cost_usd": 0.004,
    }, {})
    assert llm.list_aliases()["class"] == "fast"
    message = [{"role": "user", "content": "測試"}]
    first = llm.chat(message, alias="class")
    second = llm.chat(message, alias="class")
    assert (first.text, first.provider, first.model) == ("遠端回答", "remote", "gemini-2.5-flash")
    assert (first.input_tokens, first.output_tokens, first.cost_usd) == (7, 3, 0.004)
    assert second.cached and second.cost_usd == 0
    assert len(requests) == 1
    method, path, headers, data = requests[0]
    assert (method, path, headers["Authorization"]) == ("POST", "/v1/chat/completions", "Bearer test-token")
    assert json.loads(data) == {"model": "fast", "messages": llm.prepared_messages(message)}


@pytest.mark.parametrize("alias", ["smart", "gemini-3.8-flash", "", "FAST"])
def test_remote_rejects_non_student_model(remote_env, gateway_stub, monkeypatch, alias):
    monkeypatch.setenv("GATEWAY_MODEL", alias)
    with pytest.raises(llm.ConfigError, match="fast 或 mock"):
        llm.chat([{"role": "user", "content": "測試"}], alias="class")
    assert gateway_stub[2] == []


def test_cached_class_still_rejects_invalid_model(remote_env, gateway_stub, monkeypatch):
    _, routes, _ = gateway_stub
    routes["/v1/chat/completions"] = (200, {
        "choices": [{"message": {"content": "已回答"}}], "model": "mock",
        "usage": {"prompt_tokens": 2, "completion_tokens": 2}, "x_cost_usd": 0.000001,
    }, {})
    messages = [{"role": "user", "content": "相同問題"}]
    llm.chat(messages, alias="class")
    monkeypatch.setenv("GATEWAY_MODEL", "smart")
    with pytest.raises(llm.ConfigError, match="fast 或 mock"):
        llm.chat(messages, alias="class")
    monkeypatch.setenv("GATEWAY_MODEL", "mock")
    assert not llm.chat(messages, alias="class").cached


@pytest.mark.parametrize(("status", "headers", "error"), [
    (401, {}, llm.ConfigError),
    (429, {"Retry-After": "2"}, llm.RateLimited),
    (402, {}, llm.BudgetExceeded),
    (403, {}, llm.ProviderError),
    (500, {}, llm.ProviderError),
])
def test_remote_http_errors(remote_env, gateway_stub, status, headers, error):
    _, routes, _ = gateway_stub
    routes["/v1/chat/completions"] = (status, {"detail": "error"}, headers)
    with pytest.raises(error) as exc:
        llm.chat([{"content": "錯誤"}], alias="class")
    if status == 401:
        assert "make claim" in str(exc.value)
    if status == 429:
        assert exc.value.retry_after == 2
    if status == 402:
        assert exc.value.tenant == "default"


def test_remote_invalid_response_is_provider_error(remote_env, gateway_stub):
    _, routes, _ = gateway_stub
    routes["/v1/chat/completions"] = (200, {"choices": []}, {})
    with pytest.raises(llm.ProviderError, match="格式"):
        llm.chat([{"content": "invalid"}], alias="class")


def test_remote_lock_times_out_and_releases(remote_env, gateway_stub, monkeypatch):
    from gateway.providers import remote

    entered, release = Event(), Event()
    original = remote.urlopen

    def slow_open(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    gateway_stub[1]["/v1/chat/completions"] = (200, {
        "choices": [{"message": {"content": "完成"}}], "model": "mock",
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}, "x_cost_usd": 0.0,
    }, {})
    monkeypatch.setenv("REMOTE_LOCK_TIMEOUT_S", "0.15")
    monkeypatch.setattr(remote, "urlopen", slow_open)
    first = Thread(target=remote.chat, args=([{"content": "先"}],), daemon=True)
    first.start()
    assert entered.wait(5)
    try:
        with pytest.raises(llm.ProviderError, match="上一個真實請求.*逾時"):
            remote.chat([{"content": "後"}])
    finally:
        release.set()
        first.join(timeout=5)
    assert not first.is_alive()
    assert remote.chat([{"content": "再次"}]).text == "完成"


def test_remote_lock_waits_for_thread_then_succeeds(remote_env, gateway_stub, monkeypatch):
    from gateway.providers import remote

    url = gateway_stub[0]
    entered, release = Event(), Event()

    def hold():
        with remote._one_request(url, "test-token"):
            entered.set()
            assert release.wait(5)

    monkeypatch.setenv("REMOTE_LOCK_TIMEOUT_S", "1")
    first = Thread(target=hold, daemon=True)
    first.start()
    assert entered.wait(5)
    timer = Thread(target=lambda: (time.sleep(0.1), release.set()), daemon=True)
    timer.start()
    try:
        with remote._one_request(url, "test-token"):
            assert release.is_set()
    finally:
        release.set()
        first.join(timeout=5)
        timer.join(timeout=5)
    assert not first.is_alive()


CHILD_CONTENDER = """
import sys
from gateway.llm import ProviderError
from gateway.providers.remote import _one_request
try:
    with _one_request(sys.argv[1], "test-token"):
        print("取得了不該取得的鎖", file=sys.stderr)
        sys.exit(3)
except ProviderError as exc:
    if "逾時" in str(exc):
        sys.exit(0)
    print("非預期的錯誤：" + str(exc), file=sys.stderr)
    sys.exit(2)
"""


def child_env(tmp_path, **extra):
    """子行程沿用完整環境（Windows 需要 SYSTEMROOT 等），只換掉暫存目錄與逾時。"""
    different_temp = tmp_path / "different-temp"
    different_temp.mkdir(exist_ok=True)
    env = os.environ.copy()
    env.update({"REMOTE_LOCK_TIMEOUT_S": "0.1", "TMPDIR": str(different_temp),
                "TMP": str(different_temp), "TEMP": str(different_temp)})
    env.update(extra)
    return env


def run_child(script, url, env):
    return subprocess.run(
        [sys.executable, "-c", script, url], cwd=Path(__file__).resolve().parents[1],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=30, check=False,
    )


def test_remote_lock_is_shared_across_processes(remote_env, gateway_stub, tmp_path):
    """父行程持鎖時，另一個暫存目錄設定不同的行程必須逾時，而不是拿到鎖。

    CI 失敗根因：舊版子行程環境只有 PATH，macOS 少了 TMPDIR、Windows 少了 TEMP，
    gettempdir() 與父行程不同，兩邊用的是不同的鎖檔，子行程拿到鎖（結束碼 1、stderr 空）。
    """
    from gateway.providers import remote

    url = gateway_stub[0]
    env = child_env(tmp_path)
    with remote._one_request(url, "test-token"):
        held = run_child(CHILD_CONTENDER, url, env)
    assert held.returncode == 0, (held.returncode, held.stderr)
    released = run_child(CHILD_CONTENDER.replace("sys.exit(3)", "sys.exit(0)"), url, env)
    assert released.returncode == 0, (released.returncode, released.stderr)


def test_lock_path_ignores_temp_directory_when_explicit(remote_env, gateway_stub, tmp_path):
    """行為測試：鎖檔位置只取決於 REMOTE_LOCK_DIR，與 TMPDIR／TEMP／平台無關。"""
    from gateway.providers import remote

    url = gateway_stub[0]
    script = (
        "import sys\n"
        "from gateway.providers.remote import lock_path\n"
        "print(lock_path(sys.argv[1], 'test-token'))\n"
    )
    child = run_child(script, url, child_env(tmp_path))
    assert child.returncode == 0, child.stderr
    assert Path(child.stdout.strip()) == remote.lock_path(url, "test-token")
    assert remote.lock_path(url, "test-token").parent == tmp_path.resolve()
    assert remote.lock_path(url, "other-token") != remote.lock_path(url, "test-token")


def test_default_lock_dir_does_not_depend_on_temp_directory(remote_env, gateway_stub, tmp_path, monkeypatch):
    """未指定 REMOTE_LOCK_DIR 時，兩個暫存目錄不同的行程仍會合在使用者家目錄。"""
    from gateway.providers import remote

    monkeypatch.delenv("REMOTE_LOCK_DIR")
    url = gateway_stub[0]
    script = (
        "import sys\n"
        "from gateway.providers.remote import lock_path\n"
        "print(lock_path(sys.argv[1], 'test-token'))\n"
    )
    child = run_child(script, url, child_env(tmp_path))
    assert child.returncode == 0, child.stderr
    assert Path(child.stdout.strip()) == remote.lock_path(url, "test-token")
    assert remote.lock_path(url, "test-token").parent == (tmp_path / ".hwdc26" / "locks").resolve()


def test_remote_lock_waits_for_child_then_times_out(remote_env, gateway_stub, monkeypatch):
    from gateway.providers import remote

    url = gateway_stub[0]
    script = """
import sys
from gateway.providers.remote import _one_request
with _one_request(sys.argv[1], "test-token"):
    print("ready", flush=True)
    sys.stdin.read(1)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, url], cwd=Path(__file__).resolve().parents[1],
        env=os.environ.copy(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
    )
    ready, line = Event(), []
    reader = Thread(target=lambda: (line.append(child.stdout.readline()), ready.set()), daemon=True)
    reader.start()
    try:
        assert ready.wait(5), "子行程未啟動"
        assert line == ["ready\n"], child.poll()
        monkeypatch.setenv("REMOTE_LOCK_TIMEOUT_S", "0.1")
        with pytest.raises(llm.ProviderError, match="逾時"):
            with remote._one_request(url, "test-token"):
                pytest.fail("子行程持鎖時不能取得鎖")
    finally:
        output, errors = child.communicate(input="x", timeout=5)
        reader.join(timeout=5)
    assert child.returncode == 0, errors
    with remote._one_request(url, "test-token"):
        pass


def test_claim_writes_only_token_and_preserves_env(remote_env, gateway_stub, tmp_path, monkeypatch, capsys):
    _, routes, requests = gateway_stub
    routes["/claim"] = (200, {"token": "new-secret-token", "expires_at": 123456789}, {})
    monkeypatch.setenv("CLASS_CODE", "workshop")
    monkeypatch.setenv("NICKNAME", "student")
    env_path = tmp_path / ".env"
    env_path.write_text("PROVDER=mock\nGATEWAY_TOKEN=old-token\n", encoding="utf-8")
    claim.claim(env_path)
    assert "PROVDER=mock" in env_path.read_text(encoding="utf-8")
    assert env_path.read_text(encoding="utf-8").count("GATEWAY_TOKEN=") == 1
    assert "new-secret-token" in env_path.read_text(encoding="utf-8")
    assert json.loads(requests[0][3]) == {"class_code": "workshop", "nickname": "student"}
    assert "new-secret-token" not in capsys.readouterr().out


def test_claim_cli_loads_env_without_exposing_token(remote_env, gateway_stub, tmp_path, monkeypatch, capsys):
    _, routes, _ = gateway_stub
    routes["/claim"] = (200, {"token": "private-token", "expires_at": "2099-01-01T00:00:00Z"}, {})
    monkeypatch.chdir(tmp_path)
    Path(".env").write_text(
        "CLASS_CODE=workshop\nNICKNAME=student\nGATEWAY_URL=" + gateway_stub[0] + "\n",
        encoding="utf-8",
    )
    for key in ("CLASS_CODE", "NICKNAME", "GATEWAY_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(sys, "argv", ["claim"])
    claim.main()
    stored = tmp_path / ".hwdc26" / "token"
    assert stored.read_text(encoding="utf-8").strip() == "private-token"
    assert "GATEWAY_TOKEN" not in Path(".env").read_text(encoding="utf-8")
    if os.name == "posix":
        assert stored.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert "private-token" not in output and "專案外" in output and "2099-01-01" in output


def test_claim_env_opt_in_and_file_fallback(remote_env, gateway_stub, tmp_path, monkeypatch, capsys):
    gateway_stub[1]["/claim"] = (200, {
        "token": "leapkey-hwdc26-" + "20261016-c2xpcDAx-c2lnbmF0dXJl",
        "expires_at": "2026-10-16T23:59:59+08:00",
    }, {})
    monkeypatch.setenv("CLASS_CODE", "workshop")
    monkeypatch.setenv("NICKNAME", "student")
    monkeypatch.delenv("GATEWAY_TOKEN")
    monkeypatch.setattr(sys, "argv", ["claim"])
    claim.main()
    from gateway.token_store import read_token
    token = read_token()
    assert token.startswith("leapkey-hwdc26-")
    assert token not in capsys.readouterr().out
    monkeypatch.setattr(sys, "argv", ["claim", "--env"])
    monkeypatch.chdir(tmp_path)
    claim.main()
    assert token in (tmp_path / ".env").read_text(encoding="utf-8")
    assert token not in capsys.readouterr().out
    monkeypatch.setenv("GATEWAY_TOKEN", "override")
    assert read_token() == "override"


def test_submit_remote_questions_and_fallback(remote_env, gateway_stub, tmp_path, monkeypatch, capsys):
    _, routes, requests = gateway_stub
    sample = json.loads(SAMPLE_QUESTIONS.read_text(encoding="utf-8"))
    routes["/questions"] = (200, {"questions": sample}, {})
    assert len(submit.remote_questions("test-token")) == 10
    assert requests[-1][2]["Authorization"] == "Bearer test-token"
    routes["/questions"] = (503, {"detail": "unavailable"}, {})
    usage = tmp_path / "usage.jsonl"
    usage.write_text("".join(
        json.dumps({"cost_usd": 0.001, "provider": "mock", "status": "ok"}) + "\n"
        for _ in range(10)
    ), encoding="utf-8")
    questions = load_questions(SAMPLE_QUESTIONS)
    answers = tmp_path / "answers.json"
    answers.write_text(json.dumps({
        "answers": [{"id": q["id"], "answer": " ".join(q["expected_keywords"])} for q in questions],
        "usage_start": 0, "usage_end": 10,
    }, ensure_ascii=False), encoding="utf-8")
    l1, l2 = tmp_path / "l1", tmp_path / "l2"
    l1.write_text("0/6", encoding="utf-8")
    l2.write_text("0/3", encoding="utf-8")
    monkeypatch.setattr("sys.argv", [
        "submit", "--dry-run", "--answers", str(answers), "--usage", str(usage),
        "--l1-result", str(l1), "--l2-result", str(l2),
    ])
    submit.main()
    output = capsys.readouterr()
    assert json.loads(output.out)["quality_passed"] == 10
    assert "questions.sample.json" in output.err
    routes["/questions"] = (200, sample, {})
    routes["/submit"] = (200, {"ok": True}, {})
    monkeypatch.setenv("CLASS_CODE", "workshop")
    monkeypatch.setenv("NICKNAME", "student")
    monkeypatch.setattr("sys.argv", [
        "submit", "--answers", str(answers), "--usage", str(usage),
        "--l1-result", str(l1), "--l2-result", str(l2),
    ])
    submit.main()
    method, path, headers, data = requests[-1]
    assert (method, path, headers["Authorization"]) == ("POST", "/submit", "Bearer test-token")
    assert json.loads(data)["level"] == "L3"
    assert json.loads(data)["passed"] is True
