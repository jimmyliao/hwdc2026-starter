import json
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from server import app as service
from server import tokens as token_format


SECRET = "test-signing-secret-0123456789abcdef-xyz"  # >= 32 字元
CODE = "classroom-code-0001"  # >= 10 字元
ADMIN = "admin-test-token-0123456789abcdef-0123"  # >= 32 字元


@pytest.fixture
def client(monkeypatch, tmp_path):
    for name in ("KILL", "FALLBACK", "MOCK_FAIL", "ALLOWED_ALIASES", "QUESTIONS_FILE", "INSTRUCTOR",
                 "TOKEN_TPM", "MAX_INPUT_TOKENS", "GLOBAL_CONCURRENCY", "GLOBAL_QUEUE_TIMEOUT_S",
                 "TOKEN_TTL_S", "TOKEN_EXPIRES_ON", "TRUSTED_PROXY_HOPS", "ALLOW_WEAK_SECRETS", "CLAIM_RPM",
                 "CLAIM_GLOBAL_RPM", "CLAIM_FAIL_LIMIT", "CLAIM_GLOBAL_FAIL_LIMIT", "MAX_TOKENS_ISSUED",
                 "MAX_WAITING", "UPSTREAM_TIMEOUT_S", "ADMIN_FAIL_LIMIT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SERVER_SECRET", SECRET)
    monkeypatch.setenv("CLASS_CODE", CODE)
    monkeypatch.setenv("ADMIN_TOKEN", ADMIN)
    monkeypatch.setenv("QUESTIONS_FILE", str(Path(__file__).resolve().parents[1] / "questions.sample.json"))
    monkeypatch.setenv("USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("RPM_LIMIT", "1000")
    monkeypatch.setenv("MAX_RETRIES", "0")
    monkeypatch.setenv("TOKEN_RPM", "10")
    monkeypatch.setenv("TOKEN_BUDGET_USD", "0.05")
    monkeypatch.setenv("GLOBAL_BUDGET_USD", "10")
    monkeypatch.setattr(service, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    with TestClient(service.create_app()) as app:
        yield app


def claim(client, nickname="小明"):
    response = client.post("/claim", json={"class_code": CODE, "nickname": nickname})
    assert response.status_code == 200, response.text
    return response.json()


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def chat(client, token, text="測試", model="mock"):
    return client.post(
        "/v1/chat/completions",
        headers=headers(token),
        json={"model": model, "messages": [{"role": "user", "content": text}]},
    )


def test_claim_auth_expiration_and_ip_limit(client, monkeypatch):
    monkeypatch.setenv("CLAIM_RPM", "5")
    assert client.post("/claim", json={"class_code": "wrong", "nickname": "ok"}).status_code == 403
    issued = claim(client, "小\x00明")
    assert issued["expires_at"] == "2026-10-16T23:59:59+08:00"
    assert issued["expires_on"] == "20261016"
    assert issued["token"].startswith("leapkey-hwdc26-" + "20261016-")
    assert client.get("/questions", headers=headers(issued["token"])).status_code == 200
    assert client.get("/questions", headers=headers(issued["token"] + "x")).status_code == 401
    assert client.get("/questions", headers=headers("garbage")).status_code == 401
    assert client.get("/questions").status_code == 401
    assert client.post("/claim", json={"class_code": CODE, "nickname": "a" * 21}).status_code == 422
    assert claim(client, "another")
    assert client.post("/claim", json={"class_code": "wrong", "nickname": "ok"}).status_code == 403
    limited = client.post("/claim", json={"class_code": CODE, "nickname": "ok"})
    assert limited.status_code == 429 and "Retry-After" in limited.headers


def test_expired_token(client, monkeypatch):
    monkeypatch.setenv("TOKEN_TTL_S", "1")
    original = service.time.time()
    monkeypatch.setattr(service.time, "time", lambda: original)
    issued = claim(client)
    token = issued["token"]
    assert issued["expires_at"] == datetime.fromtimestamp(original + 1, token_format.TAIPEI).isoformat()
    monkeypatch.setattr(service.time, "time", lambda: original + 2)
    assert client.get("/questions", headers=headers(token)).status_code == 401


def test_invalid_or_elapsed_issue_date(client, monkeypatch):
    monkeypatch.setenv("TOKEN_EXPIRES_ON", "20261342")
    invalid = client.post("/claim", json={"class_code": CODE, "nickname": "ok"})
    assert invalid.status_code == 503 and "TOKEN_EXPIRES_ON" in invalid.json()["detail"]
    monkeypatch.setenv("TOKEN_EXPIRES_ON", "20261012")
    elapsed = client.post("/claim", json={"class_code": CODE, "nickname": "ok"})
    assert elapsed.status_code == 503 and "TOKEN_EXPIRES_ON" in elapsed.json()["detail"]


def test_taipei_boundary_tamper_legacy_and_revocation(client, monkeypatch):
    zone = timezone(timedelta(hours=8))
    end = datetime(2026, 10, 16, 23, 59, 59, tzinfo=zone)
    monkeypatch.setattr(service.time, "time", lambda: end.timestamp())
    token = claim(client)["token"]
    assert token_format.verify(token, SECRET, now=end) == "小明"
    assert client.get("/questions", headers=headers(token)).status_code == 200
    monkeypatch.setattr(service.time, "time", lambda: (end + timedelta(seconds=1)).timestamp())
    assert client.get("/questions", headers=headers(token)).status_code == 401
    with pytest.raises(ValueError, match="過期"):
        token_format.verify(token, SECRET, now=end + timedelta(seconds=1))
    monkeypatch.setattr(service.time, "time", lambda: end.timestamp())
    for index in (len(token_format.PREFIX), len(token_format.PREFIX) + 9, len(token) - 1):
        replacement = "A" if token[index] != "A" else "B"
        changed = token[:index] + replacement + token[index + 1:]
        assert client.get("/questions", headers=headers(changed)).status_code == 401
    legacy = client.get("/questions", headers=headers("cGF5bG9hZA.c2ln"))
    assert legacy.status_code == 401 and "重新 make claim" in legacy.json()["detail"]
    admin = headers(ADMIN)
    assert client.get("/admin/revoked").status_code == 401
    assert client.post("/admin/revoke", headers=headers("wrong"), json={"nickname": "小明"}).status_code == 401
    assert client.post("/admin/revoke", headers=admin, json={"nickname": "小\x00明"}).json() == {"revoked": "小明"}
    assert client.get("/admin/revoked", headers=admin).json() == {"revoked": ["小明"]}
    assert client.get("/questions", headers=headers(token)).json()["detail"] == "token 已被講師撤銷"
    assert client.post("/claim", json={"class_code": CODE, "nickname": "小明"}).status_code == 403
    assert client.post("/admin/unrevoke", headers=admin, json={"nickname": "小明"}).json() == {"unrevoked": "小明"}
    assert client.get("/questions", headers=headers(token)).status_code == 200


def test_chat_openai_shape_tenant_and_validation(client, monkeypatch, tmp_path):
    token = claim(client)["token"]
    response = chat(client, token, "hello")
    assert response.status_code == 200, response.text
    answer = response.json()
    assert answer["object"] == "chat.completion"
    assert answer["choices"] == [{"index": 0, "message": {"role": "assistant", "content": "Mock 回答：hello"}, "finish_reason": "stop"}]
    assert answer["usage"]["total_tokens"] == answer["usage"]["prompt_tokens"] + answer["usage"]["completion_tokens"]
    assert answer["x_cost_usd"] > 0
    assert chat(client, token, "hello").json()["x_cost_usd"] == 0
    records = [json.loads(row) for row in (tmp_path / "usage.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [row["tenant"] for row in records] == ["小明", "小明"]
    assert client.get("/admin/stats", headers=headers(ADMIN)).json()["total_cost_usd"] == answer["x_cost_usd"]
    assert chat(client, token, model="smart").status_code == 400
    assert chat(client, token, "x" * (service.MAX_CHARACTERS + 1)).status_code == 413
    assert client.post("/v1/chat/completions", headers=headers(token), json={
        "model": "mock", "messages": [{"role": "developer", "content": "hi"}],
    }).status_code == 422
    assert client.post("/v1/chat/completions", headers=headers(token), json={
        "model": "mock", "messages": [{"role": "user", "content": "x"}] * 21,
    }).status_code == 422
    assert client.post("/submit", headers=headers(token), content=b"x" * (service.MAX_BODY_BYTES + 1)).status_code == 413
    monkeypatch.setenv("FALLBACK", "smart")
    assert chat(client, token).status_code == 503
    monkeypatch.delenv("FALLBACK")
    monkeypatch.setenv("ALLOWED_ALIASES", "smart,mock")
    assert chat(client, token, model="smart").status_code == 400


def test_budgets_rpm_upstream_errors_and_kill(client, monkeypatch):
    token = claim(client)["token"]
    monkeypatch.setenv("TOKEN_RPM", "1")
    assert chat(client, token).status_code == 200
    response = chat(client, token, "different")
    assert response.status_code == 429 and "Retry-After" in response.headers
    monkeypatch.setenv("TOKEN_RPM", "10")
    other = claim(client, "另一位")["token"]
    monkeypatch.setenv("MOCK_FAIL", "429")
    upstream = chat(client, other, "upstream failure")
    assert upstream.status_code == 429 and "Retry-After" in upstream.headers
    monkeypatch.delenv("MOCK_FAIL")
    monkeypatch.setenv("TOKEN_BUDGET_USD", "0.00000001")
    denied = chat(client, other, "new")
    assert denied.status_code == 402
    assert denied.json()["detail"]["remaining_usd"] == pytest.approx(0.00000001)
    assert denied.json()["detail"]["required_usd"] > 0.00000001
    third = claim(client, "第三位")["token"]
    monkeypatch.setenv("GLOBAL_BUDGET_USD", "0.00000001")
    assert chat(client, third).status_code == 402
    assert client.post("/admin/kill", headers=headers("wrong")).status_code == 401
    assert client.post("/admin/kill", headers=headers(ADMIN)).json() == {"killed": True}
    health = client.get("/healthz").json()
    assert health["ok"] is True and health["killed"] is True
    assert client.get("/questions", headers=headers(token)).status_code == 503
    assert client.post("/admin/revive", headers=headers(ADMIN)).json() == {"killed": False}
    monkeypatch.setenv("KILL", "1")
    assert client.post("/admin/revive", headers=headers(ADMIN)).json() == {"killed": True}


def test_questions_submissions_leaderboard_and_snapshot(client, monkeypatch, tmp_path):
    path = tmp_path / "questions.json"
    path.write_text('[{"id":"q1","question":"你好"}]', encoding="utf-8")
    monkeypatch.setenv("QUESTIONS_FILE", str(path))
    token = claim(client, "<script>")["token"]
    assert client.get("/questions", headers=headers(token)).json() == [{"id": "q1", "question": "你好"}]
    assert client.post("/submit", headers=headers(token), json={
        "level": "L1", "passed": True, "tokens": 7, "cost": 0.01, "details": {"note": "ok"},
    }).json() == {"ok": True}
    assert client.post("/submit", headers=headers(token), json={
        "level": "L1", "passed": True, "tokens": 0, "cost": -1,
    }).status_code == 422
    rows = client.get("/leaderboard?format=json").json()
    assert len(rows) == 1 and "updated_at" in rows[0]
    assert {key: value for key, value in rows[0].items() if key != "updated_at"} == {
        "nickname": "<script>", "level": "L1", "passed": True, "tokens": 7, "cost": 0.01}
    page = client.get("/leaderboard").text
    assert 'content="5"' in page and "&lt;script&gt;" in page and "<script>" not in page
    assert client.get("/leaderboard?format=xml").status_code == 400
    assert client.get("/admin/stats", headers=headers(ADMIN)).json()["submissions"] == 1
    client.app.state.snapshot()
    snapshot = (tmp_path / "snapshot.json").read_text(encoding="utf-8")
    assert token not in snapshot and SECRET not in snapshot
    assert json.loads(snapshot)["submissions"] == rows
    path.unlink()
    assert client.get("/questions", headers=headers(token)).status_code == 503


def test_server_never_calls_real_gemini(client, monkeypatch):
    from gateway.providers import gemini

    monkeypatch.setattr(gemini, "chat", lambda *args, **kwargs: pytest.fail("不可呼叫真實 Gemini"))
    token = claim(client)["token"]
    assert chat(client, token).status_code == 200
    monkeypatch.setenv("ALLOWED_ALIASES", "mock")
    assert chat(client, token, model="fast").status_code == 400


def test_server_does_not_inherit_local_five_rpm_limit(client, monkeypatch):
    from gateway import llm

    monkeypatch.setattr(llm, "_throttle", lambda *args: pytest.fail("server 不應套用學員本機 RPM"))
    token = claim(client)["token"]
    assert chat(client, token, "第一題").status_code == 200
    assert chat(client, token, "第二題").status_code == 200


def test_budget_reserves_next_request_before_call(client, monkeypatch):
    token = claim(client)["token"]
    from gateway.pricing import worst_cost_usd
    reserved = worst_cost_usd("mock", [{"role": "user", "content": "你好"}])
    monkeypatch.setenv("TOKEN_BUDGET_USD", str(reserved))
    first = chat(client, token, "你好")
    assert first.status_code == 200
    remaining = reserved - first.json()["x_cost_usd"]
    second = chat(client, token, "不同問題")
    assert second.status_code == 402
    assert second.json()["detail"]["remaining_usd"] == pytest.approx(remaining)
    assert first.json()["x_cost_usd"] <= reserved


def test_upstream_budget_error_reports_gateway_remaining(client, monkeypatch):
    from gateway import llm

    token = claim(client)["token"]
    def exhausted(*args, **kwargs):
        raise llm.BudgetExceeded("小明")
    monkeypatch.setattr(llm, "chat", exhausted)
    denied = chat(client, token)
    assert denied.status_code == 402
    assert denied.json()["detail"]["remaining_usd"] == pytest.approx(0.05)


def test_input_limit_counts_cjk_and_does_not_consume_rpm(client, monkeypatch):
    token = claim(client)["token"]
    monkeypatch.setenv("TOKEN_RPM", "1")
    denied = chat(client, token, "字" * 1500)
    assert denied.status_code == 413
    assert "截斷 context" in denied.json()["detail"]
    assert chat(client, token, "ok").status_code == 200
    monkeypatch.setenv("MAX_INPUT_TOKENS", "8")
    assert chat(client, claim(client, "另一位")["token"], "abcdefghi").status_code == 413


def test_tpm_window_and_failed_requests(client, monkeypatch):
    from gateway import llm
    from gateway.pricing import MAX_OUTPUT_TOKENS, SHORT_ANSWER_INSTRUCTION, estimate_tokens

    token = claim(client)["token"]
    units = estimate_tokens(SHORT_ANSWER_INSTRUCTION) + estimate_tokens("a") + MAX_OUTPUT_TOKENS
    monkeypatch.setenv("TOKEN_TPM", str(units))
    assert chat(client, token, "a").status_code == 200
    limited = chat(client, token, "b")
    assert limited.status_code == 429 and int(limited.headers["Retry-After"]) > 0
    other = claim(client, "另一位")["token"]
    assert chat(client, other, "a").status_code == 200
    original_clock = service.time.monotonic
    monkeypatch.setattr(service.time, "monotonic", lambda: original_clock() + 61)
    assert chat(client, token, "b").status_code == 200

    third = claim(client, "第三位")["token"]
    original = llm.chat
    def fail(*args, **kwargs):
        raise llm.ProviderError("offline")
    monkeypatch.setattr(llm, "chat", fail)
    assert chat(client, third, "a").status_code == 502
    monkeypatch.setattr(llm, "chat", original)
    assert chat(client, third, "a").status_code == 200


def test_single_flight_queue_timeout_and_releases(client, monkeypatch):
    from gateway import llm

    monkeypatch.setenv("GLOBAL_QUEUE_TIMEOUT_S", "0.05")
    # The semaphore is constructed at app creation, so use a fresh app for this override.
    monkeypatch.setenv("GLOBAL_CONCURRENCY", "1")
    with TestClient(service.create_app()) as limited_client:
        first = claim(limited_client)["token"]
        second = claim(limited_client, "另一位")["token"]
        started, release = Event(), Event()
        original = llm.chat

        def paused(*args, **kwargs):
            started.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(llm, "chat", paused)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(chat, limited_client, first, "first")
            try:
                assert started.wait(5)
                busy = chat(limited_client, first, "second")
                assert busy.status_code == 429
                assert busy.json()["detail"] == "請等上一個請求完成"
                assert busy.headers["Retry-After"] == "1"
                queued = chat(limited_client, second)
                assert queued.status_code == 503
                assert queued.headers["Retry-After"] == "1"
            finally:
                release.set()
            assert pending.result(timeout=5).status_code == 200
        assert chat(limited_client, second).status_code == 200
        assert chat(limited_client, first, "again").status_code == 200


def test_concurrent_budget_reservation_and_failure_cleanup(client, monkeypatch):
    from gateway import llm
    from gateway.pricing import worst_cost_usd

    monkeypatch.setenv("GLOBAL_CONCURRENCY", "2")
    with TestClient(service.create_app()) as shared_client:
        first = claim(shared_client)["token"]
        second = claim(shared_client, "另一位")["token"]
        reserved = worst_cost_usd("mock", [{"role": "user", "content": "hold"}])
        monkeypatch.setenv("GLOBAL_BUDGET_USD", str(reserved))
        started, release = Event(), Event()
        original = llm.chat

        def paused(*args, **kwargs):
            started.set()
            assert release.wait(5)
            raise llm.ProviderError("offline")

        monkeypatch.setattr(llm, "chat", paused)
        with ThreadPoolExecutor(max_workers=2) as pool:
            pending = pool.submit(chat, shared_client, first, "hold")
            try:
                assert started.wait(5)
                rejected = chat(shared_client, second, "hold")
                assert rejected.status_code == 402
                assert rejected.json()["detail"]["remaining_usd"] == pytest.approx(0)
            finally:
                release.set()
            assert pending.result(timeout=5).status_code == 502
        monkeypatch.setattr(llm, "chat", original)
        assert chat(shared_client, second, "hold").status_code == 200
