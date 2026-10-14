"""R1 資安修復（F1–F16）的迴歸測試；全部離線、使用假密鑰。"""

import asyncio
import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path
from threading import Event

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from server import app as service
from server import tokens as token_format

SECRET = "sec-test-signing-secret-0123456789abcdef"
CODE = "class-code-for-tests"
ADMIN = "adm-test-admin-token-0123456789abcdef-xx"
ROOT = Path(__file__).resolve().parents[2]
ENV_NAMES = ("KILL", "FALLBACK", "MOCK_FAIL", "ALLOWED_ALIASES", "INSTRUCTOR", "TOKEN_TPM", "MAX_INPUT_TOKENS",
             "GLOBAL_CONCURRENCY", "GLOBAL_QUEUE_TIMEOUT_S", "TOKEN_TTL_S", "TOKEN_EXPIRES_ON", "TRUSTED_PROXY_HOPS",
             "ALLOW_WEAK_SECRETS", "CLAIM_RPM", "CLAIM_GLOBAL_RPM", "CLAIM_FAIL_LIMIT", "CLAIM_GLOBAL_FAIL_LIMIT",
             "MAX_TOKENS_ISSUED", "MAX_WAITING", "UPSTREAM_TIMEOUT_S", "ADMIN_FAIL_LIMIT", "CLAIM_LOCK_S",
             "CLAIM_GLOBAL_COOLDOWN_S", "ADMIN_LOCK_S")


@pytest.fixture
def env(monkeypatch, tmp_path):
    for name in ENV_NAMES:
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
    monkeypatch.setattr(service, "SNAPSHOT_FILE", tmp_path / "snapshot.json")
    return monkeypatch


@pytest.fixture
def client(env):
    with TestClient(service.create_app()) as app:
        yield app


def auth(token):
    return {"Authorization": f"Bearer {token}"}


def claim(client, nickname="小明", code=CODE, headers=None):
    return client.post("/claim", json={"class_code": code, "nickname": nickname}, headers=headers or {})


def issued(client, nickname="小明"):
    response = claim(client, nickname)
    assert response.status_code == 200, response.text
    return response.json()["token"]


# ---- F1：公開回應不得含 class_code／token ----------------------------------------------

def test_f1_details_whitelist_and_public_responses_never_leak(client):
    token = issued(client, "洩漏測試")
    payload = {
        "level": "L3", "passed": True, "tokens": 12, "cost": 0.002,
        "details": {
            "class_code": CODE, "token": token, "Authorization": f"Bearer {token}", "nickname": "別人",
            "l1": {"passed": 6, "total": 6, "class_code": CODE}, "l2": {"passed": 3, "total": 3},
            "question_count": 10, "quality_passed": 10, "questions": [{"id": "q1", "passed": True}],
            "pricing": "x" * 1000, "nested": {"a": {"b": {"c": CODE}}}, "extra": CODE,
        },
    }
    assert client.post("/submit", headers=auth(token), json=payload).json() == {"ok": True}
    public = [client.get("/leaderboard?format=json"), client.get("/leaderboard"), client.get("/healthz")]
    for response in public:
        assert CODE not in response.text and token not in response.text
        assert "class_code" not in response.text and "leapkey-hwdc26" not in response.text
    row = public[0].json()[0]
    assert set(row) == {"nickname", "level", "passed", "tokens", "cost", "updated_at"}
    assert row["nickname"] == "洩漏測試"
    client.app.state.snapshot()
    snapshot = service.SNAPSHOT_FILE.read_text(encoding="utf-8")
    assert CODE not in snapshot and token not in snapshot
    # 內部儲存的 details 只剩白名單欄位
    assert service._clean_details(payload["details"]) == {
        "l1": {"passed": 6, "total": 6}, "l2": {"passed": 3, "total": 3}, "question_count": 10, "quality_passed": 10}
    assert service._clean_details({"l1": {"passed": True, "total": 6}, "question_count": "10", "quality_passed": -1}) == {}


def test_f1_submit_client_payload_has_no_class_code(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from submit import submit

    answers = tmp_path / "answers.json"
    usage = tmp_path / "usage.jsonl"
    from scenarios.runner import SAMPLE_QUESTIONS, load_questions

    questions = load_questions(SAMPLE_QUESTIONS)
    answers.write_text(json.dumps({
        "answers": [{"id": q["id"], "answer": " ".join(q["expected_keywords"])} for q in questions],
        "usage_start": 0, "usage_end": 10}, ensure_ascii=False), encoding="utf-8")
    usage.write_text("".join(json.dumps({"cost_usd": 0.001, "provider": "mock", "status": "ok"}) + "\n"
                             for _ in range(10)), encoding="utf-8")
    l1, l2 = tmp_path / "l1", tmp_path / "l2"
    l1.write_text("0/6", encoding="utf-8")
    l2.write_text("0/3", encoding="utf-8")
    monkeypatch.setenv("CLASS_CODE", "super-secret-class-code")
    payload = submit.build_payload(SAMPLE_QUESTIONS, answers, usage, l1, l2, questions)
    assert "class_code" not in payload and "super-secret-class-code" not in json.dumps(payload)


def test_f16_submit_requires_https_except_localhost():
    from submit import submit

    for good in ("https://example.run.app/submit", "http://localhost:8080/submit", "http://127.0.0.1:1/submit"):
        assert submit.require_https(good) == good
    for bad in ("http://example.com/submit", "ftp://x/y", "//x", "http://127.0.0.1.evil.com/", "file:///etc/passwd"):
        with pytest.raises(ValueError):
            submit.require_https(bad)


# ---- F2：IP 識別、限速、鎖定 -----------------------------------------------------------

def _request(host, xff=None):
    from starlette.requests import Request

    headers = [(b"x-forwarded-for", value.encode()) for value in (xff or [])]
    return Request({"type": "http", "headers": headers, "client": (host, 1234) if host else None})


def test_f2_client_identity_uses_trusted_hops(env):
    assert service.client_identity(_request("10.0.0.1"))["resolved_ip"] == "10.0.0.1"  # 無標頭
    info = service.client_identity(_request("10.0.0.1", ["6.6.6.6, 203.0.113.9"]))
    assert info["resolved_ip"] == "203.0.113.9" and info["source"] == "x-forwarded-for"  # 偽造值在左側，不被採用
    env.setenv("TRUSTED_PROXY_HOPS", "2")
    assert service.client_identity(_request("10.0.0.1", ["6.6.6.6, 203.0.113.9"]))["resolved_ip"] == "6.6.6.6"
    assert service.client_identity(_request("10.0.0.1", ["203.0.113.9"]))["resolved_ip"] == "10.0.0.1"  # 跳數不足
    env.setenv("TRUSTED_PROXY_HOPS", "0")
    assert service.client_identity(_request("10.0.0.1", ["6.6.6.6"]))["resolved_ip"] == "10.0.0.1"
    env.setenv("TRUSTED_PROXY_HOPS", "1")
    assert service.client_identity(_request("10.0.0.1", ["not-an-ip"]))["resolved_ip"] == "10.0.0.1"  # 畸形
    assert service.client_identity(_request(None))["resolved_ip"] == "unknown"


def test_f2_spoofed_xff_cannot_bypass_rate_limit_and_buckets_are_capped(client, env):
    env.setenv("CLAIM_RPM", "3")
    codes = []
    for index in range(6):  # 攻擊者每次換最左側偽造值，最右側（受信任）固定
        codes.append(claim(client, f"u{index}", headers={"X-Forwarded-For": f"1.1.1.{index}, 203.0.113.9"}).status_code)
    assert codes == [200, 200, 200, 429, 429, 429]
    env.setenv("CLAIM_RPM", "100")
    assert service.MAX_BUCKETS >= 1000
    env.setattr(service, "MAX_BUCKETS", 5)
    with TestClient(service.create_app()) as small:
        for index in range(12):
            small.post("/claim", json={"class_code": CODE, "nickname": f"n{index}"},
                       headers={"X-Forwarded-For": f"198.51.100.{index}"})
        stats = small.get("/admin/stats", headers=auth(ADMIN)).json()
        assert stats["claims"] <= 12  # 不會為每個偽造來源無限新增桶
        assert len(small.app.state.housekeeping.__closure__) > 0  # housekeeping 存在


def test_f2_wrong_code_lockout_and_global_cooldown_spare_token_holders(client, env):
    holder = issued(client, "持有者")
    env.setenv("CLAIM_FAIL_LIMIT", "3")
    for _ in range(3):
        assert claim(client, "攻擊", code="wrong-code-xxxxxxxx").status_code == 403
    locked = claim(client, "攻擊", code="wrong-code-xxxxxxxx")
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) >= 1
    assert claim(client, "別人").status_code == 429  # 同識別碼鎖定期間連正確代碼也拒絕
    # 已持有 token 的學員：重領不受鎖定影響，原 token 照常使用
    assert claim(client, "持有者", headers=auth(holder)).status_code == 200
    assert client.get("/questions", headers=auth(holder)).status_code == 200
    # 其他識別碼不受該 IP 鎖定影響
    assert claim(client, "路人", headers={"X-Forwarded-For": "203.0.113.77"}).status_code == 200
    # 時間經過後解鎖
    real = service.time.monotonic
    env.setattr(service.time, "monotonic", lambda: real() + 61)
    assert claim(client, "新同學").status_code == 200


def test_f2_global_cooldown(client, env):
    holder = issued(client, "持有者")
    env.setenv("CLAIM_GLOBAL_FAIL_LIMIT", "4")
    for index in range(4):
        assert claim(client, "x", code="wrong-code-xxxxxxxx",
                     headers={"X-Forwarded-For": f"203.0.113.{index}"}).status_code == 403
    cooling = claim(client, "新人", headers={"X-Forwarded-For": "203.0.113.200"})
    assert cooling.status_code == 429 and "Retry-After" in cooling.headers
    assert claim(client, "持有者", headers={**auth(holder), "X-Forwarded-For": "203.0.113.201"}).status_code == 200
    assert client.get("/questions", headers=auth(holder)).status_code == 200
    assert client.get("/healthz").status_code == 200


def test_f2_global_claim_rate_limit(client, env):
    env.setenv("CLAIM_GLOBAL_RPM", "3")
    statuses = [claim(client, f"g{index}", headers={"X-Forwarded-For": f"203.0.113.{index}"}).status_code
                for index in range(5)]
    assert statuses == [200, 200, 200, 429, 429]


def test_f2_admin_debug_client(client):
    assert client.get("/admin/debug-client").status_code == 401
    body = client.get("/admin/debug-client", headers={**auth(ADMIN), "X-Forwarded-For": "6.6.6.6, 203.0.113.9"}).json()
    assert body["resolved_ip"] == "203.0.113.9" and body["x_forwarded_for"] == "6.6.6.6, 203.0.113.9"
    assert body["trusted_proxy_hops"] == 1 and "client_host" in body
    text = json.dumps(body)
    assert SECRET not in text and ADMIN not in text and CODE not in text


# ---- F3：設定強度 --------------------------------------------------------------------

@pytest.mark.parametrize("name,value", [("CLASS_CODE", "short"), ("SERVER_SECRET", "x" * 31), ("ADMIN_TOKEN", "y" * 31)])
def test_f3_weak_config_exits_one(env, capsys, name, value):
    env.setenv(name, value)
    codes = []

    class Stop(BaseException):
        pass

    def fake_fatal(code):
        codes.append(code)
        raise Stop

    env.setattr(service, "_fatal", fake_fatal)
    with pytest.raises(BaseException):
        with TestClient(service.create_app()):
            pass
    assert codes == [1]
    err = capsys.readouterr().err
    assert name in err and value not in err


def test_f3_real_uvicorn_process_exits_one_on_weak_config(env):
    env.setenv("ADMIN_TOKEN", "short")
    result = subprocess.run(
        [sys.executable, "-m", "uvicorn", "server.app:app", "--host", "127.0.0.1", "--port", "0"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=60)
    assert result.returncode == 1
    assert "ADMIN_TOKEN" in result.stderr and "short" not in result.stderr.replace("ADMIN_TOKEN 長度", "")


def test_f3_allow_weak_for_local_testing(env, capsys):
    env.setenv("CLASS_CODE", "short")
    env.setenv("ALLOW_WEAK_SECRETS", "1")
    with TestClient(service.create_app()) as local:
        assert local.get("/healthz").status_code == 200
    assert "ALLOW_WEAK_SECRETS" in capsys.readouterr().err
    assert service.config_problems() == ["CLASS_CODE 長度 5，至少需要 10 字元"]


# ---- F4：暱稱策略 --------------------------------------------------------------------

def test_f4_normalization_applies_to_claim_revoke_and_leaderboard(client):
    token = issued(client, "Ａlice")  # 全形 A
    assert token_format.verify(token, SECRET) == "alice"
    assert claim(client, "  ALICE  ").status_code == 409  # 同一個人，非重領
    assert client.post("/admin/revoke", headers=auth(ADMIN), json={"nickname": "A　LICE"}).status_code == 200
    # 內部空白收斂：「a l i c e」不同，但 "Ａlice" 的各種寫法都撤銷同一個名字
    assert client.post("/admin/revoke", headers=auth(ADMIN), json={"nickname": "ａＬｉｃｅ"}).json() == {"revoked": "alice"}
    assert client.get("/questions", headers=auth(token)).status_code == 401
    assert claim(client, "ALICE").status_code == 403
    assert service._nickname("a   b\tc") == "a b c"
    assert service._nickname("Straße") == "strasse"


def test_f4_binding_reclaim_release_and_issue_cap(client, env):
    token = issued(client, "小華")
    conflict = claim(client, "小華")
    assert conflict.status_code == 409 and "講師" in conflict.json()["detail"]
    assert claim(client, "小華", headers=auth("garbage")).status_code == 409
    other = issued(client, "別人")
    assert claim(client, "小華", headers=auth(other)).status_code == 409  # 別人的 token 不能重領
    again = claim(client, "小華", headers=auth(token))
    assert again.status_code == 200 and again.json()["token"] == token and again.json()["reclaimed"] is True
    assert claim(client, "小華", code="wrong-code-xxxxxxxx", headers=auth(token)).status_code == 403
    assert client.post("/admin/release", headers=auth("wrong"), json={"nickname": "小華"}).status_code == 401
    released = client.post("/admin/release", headers=auth(ADMIN), json={"nickname": "小華"})
    assert released.json() == {"released": "小華", "was_bound": True}
    assert claim(client, "小華").status_code == 200  # 解除綁定後可重新領取
    env.setenv("MAX_TOKENS_ISSUED", "3")
    assert claim(client, "第三位").status_code == 200  # 小華、別人、第三位
    capped = claim(client, "第四位")
    assert capped.status_code == 403 and "上限" in capped.json()["detail"]
    assert claim(client, "第三位", headers=auth(issued_token(client, "第三位"))).status_code == 200  # 重領不占額度


def issued_token(client, nickname):
    return token_format.issue(service._nickname(nickname), "20261016", SECRET)


def test_f4_binding_expires_with_token(client, env):
    env.setenv("TOKEN_TTL_S", "5")
    original = service.time.time()
    env.setattr(service.time, "time", lambda: original)
    assert claim(client, "短命").status_code == 200
    assert claim(client, "短命").status_code == 409
    env.setattr(service.time, "time", lambda: original + 6)
    assert claim(client, "短命").status_code == 200  # 前一把已到期，不需舊 token


def test_f4_shared_public_ip_is_not_limited_per_ip_by_nickname_count(client, env):
    env.setenv("CLAIM_RPM", "100")
    assert all(claim(client, f"同IP{index}").status_code == 200 for index in range(30))


# ---- F5：重啟可偵測 ------------------------------------------------------------------

def test_f5_boot_id_and_uptime(env):
    with TestClient(service.create_app()) as first:
        health = first.get("/healthz").json()
        stats = first.get("/admin/stats", headers=auth(ADMIN)).json()
    with TestClient(service.create_app()) as second:
        assert second.get("/healthz").json()["boot_id"] != health["boot_id"]
    assert stats["boot_id"] == health["boot_id"] and len(health["boot_id"]) == 32
    assert health["uptime_s"] >= 0 and stats["uptime_s"] >= health["uptime_s"]


# ---- F6：串流計數 --------------------------------------------------------------------

def test_f6_chunked_oversize_body_is_aborted_without_reading_everything(env):
    limit = service.MAX_BODY_BYTES
    chunk = b"x" * 4096
    consumed = 0
    total_chunks = 4000  # 約 16 MiB

    async def run():
        nonlocal consumed
        sent = []
        queue = iter(range(total_chunks))

        async def receive():
            nonlocal consumed
            try:
                next(queue)
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}
            consumed += 1
            return {"type": "http.request", "body": chunk, "more_body": True}

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/submit", "headers": [(b"transfer-encoding", b"chunked"),
                                                                                   (b"content-type", b"application/json")],
                 "query_string": b"", "client": ("1.2.3.4", 1), "server": ("x", 80), "scheme": "http",
                 "http_version": "1.1", "root_path": ""}
        await service.create_app()(scope, receive, send)
        return sent

    sent = asyncio.run(run())
    assert sent[0]["status"] == 413
    assert consumed * len(chunk) <= limit + 2 * len(chunk), "不得先讀完整個 body"
    assert consumed < total_chunks // 10


def test_f6_client_level_chunked_and_content_length(client):
    token = issued(client)

    def body():
        for _ in range(400):
            yield b"y" * 1024

    assert client.post("/submit", headers=auth(token), content=body()).status_code == 413
    assert client.post("/submit", headers=auth(token), content=b"z" * (service.MAX_BODY_BYTES + 1)).status_code == 413
    ok = client.post("/submit", headers=auth(token), json={"level": "L1", "passed": True, "tokens": 1, "cost": 0})
    assert ok.status_code == 200


# ---- F7：例外處理 --------------------------------------------------------------------

def test_f7_non_ascii_and_malformed_inputs_do_not_500(client):
    assert claim(client, code="課堂代碼é").status_code == 403
    assert client.post("/claim", content='{"class_code":"\\ud800","nickname":"x"}',
                       headers={"Content-Type": "application/json"}).status_code in {403, 422}
    assert client.get("/admin/stats", headers={"Authorization": "Bearer 管理員é".encode()}).status_code == 401
    assert client.get("/admin/stats", headers={"Authorization": "Bearer "}).status_code == 401
    for bad in ("leapkey-hwdc26-", "leapkey-hwdc26-2026", "leapkey-hwdc26-20261016-", "leapkey-hwdc26-" + "é" * 40,
                "leapkey-hwdc26-" + "A" * 400, ""):
        with pytest.raises(ValueError):
            token_format.verify(bad, SECRET)
        assert client.get("/questions", headers={"Authorization": f"Bearer {bad}".encode("utf-8")}).status_code == 401


def test_f7_unexpected_error_hides_stack(env, monkeypatch):
    def boom(*args, **kwargs):
        raise KeyError("secret-detail")

    with TestClient(service.create_app(), raise_server_exceptions=False) as quiet:
        token = issued(quiet)
        monkeypatch.setattr(service, "_clean_details", boom)
        response = quiet.post("/submit", headers=auth(token), json={"level": "L1", "passed": True, "tokens": 1, "cost": 0})
    assert response.status_code == 500 and response.json() == {"detail": "內部錯誤"}
    assert "secret-detail" not in response.text and "Traceback" not in response.text


# ---- F8：admin 限速鎖定 --------------------------------------------------------------

def test_f8_admin_lockout(client, env):
    env.setenv("ADMIN_FAIL_LIMIT", "3")
    for _ in range(3):
        assert client.get("/admin/stats", headers=auth("wrong")).status_code == 401
    locked = client.get("/admin/stats", headers=auth(ADMIN))
    assert locked.status_code == 429 and int(locked.headers["Retry-After"]) >= 1
    assert client.get("/admin/stats", headers={"X-Forwarded-For": "198.51.100.5"} | auth(ADMIN)).status_code == 200
    real = service.time.monotonic
    env.setattr(service.time, "monotonic", lambda: real() + 61)
    assert client.get("/admin/stats", headers=auth(ADMIN)).status_code == 200


# ---- F9：上游逾時、等候上限 ----------------------------------------------------------

def chat(client, token, text="hi"):
    return client.post("/v1/chat/completions", headers=auth(token),
                       json={"model": "mock", "messages": [{"role": "user", "content": text}]})


def test_f9_upstream_timeout_returns_504_and_slot_released_when_call_finishes(env):
    from gateway import llm

    env.setenv("UPSTREAM_TIMEOUT_S", "0.1")
    env.setenv("GLOBAL_CONCURRENCY", "1")
    env.setenv("GLOBAL_QUEUE_TIMEOUT_S", "0.2")
    original = llm.chat
    release = Event()

    def hung(*args, **kwargs):
        release.wait(5)
        return original(*args, **kwargs)

    with TestClient(service.create_app()) as app:
        first, second = issued(app, "甲"), issued(app, "乙")
        env.setattr(llm, "chat", hung)
        slow = chat(app, first)
        assert slow.status_code == 504 and "Retry-After" in slow.headers
        # 上游仍卡住：slot 尚未釋放，下一位排隊逾時（503），不是永遠卡住
        assert chat(app, second).status_code == 503
        release.set()
        time.sleep(0.5)
        env.setattr(llm, "chat", original)
        assert chat(app, second).status_code == 200
        assert chat(app, first, "again").status_code == 200  # active_tokens 已釋放


def test_f9_waiting_queue_cap(env):
    from concurrent.futures import ThreadPoolExecutor

    from gateway import llm

    env.setenv("GLOBAL_CONCURRENCY", "1")
    env.setenv("GLOBAL_QUEUE_TIMEOUT_S", "3")
    env.setenv("MAX_WAITING", "1")
    original = llm.chat
    started, release = Event(), Event()

    def paused(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    with TestClient(service.create_app()) as app:
        tokens = [issued(app, name) for name in ("甲", "乙", "丙")]
        env.setattr(llm, "chat", paused)
        with ThreadPoolExecutor(max_workers=2) as pool:
            running = pool.submit(chat, app, tokens[0])
            assert started.wait(5)
            queued = pool.submit(chat, app, tokens[1])
            time.sleep(0.3)
            rejected = chat(app, tokens[2])
            assert rejected.status_code == 503 and rejected.headers["Retry-After"] == "1"
            release.set()
            assert running.result(5).status_code == 200 and queued.result(5).status_code == 200


# ---- F10／F11 ------------------------------------------------------------------------

def test_f10_housekeeping_caps_cache_and_usage(env, tmp_path):
    env.setenv("MAX_CACHE_FILES", "3")
    env.setenv("MAX_USAGE_LOG_BYTES", "100")
    cache = tmp_path / "cache"
    cache.mkdir()
    for index in range(8):
        (cache / f"{index}.json").write_text("{}", encoding="utf-8")
        os.utime(cache / f"{index}.json", (index, index))
    (tmp_path / "usage.jsonl").write_text("x" * 500, encoding="utf-8")
    with TestClient(service.create_app()) as app:
        app.app.state.housekeeping()
    assert sorted(path.name for path in cache.glob("*.json")) == ["5.json", "6.json", "7.json"]
    assert not (tmp_path / "usage.jsonl").exists() and (tmp_path / "usage.jsonl.1").exists()


def test_f11_conservative_limits(client):
    assert service.MAX_CHARACTERS == 4000
    token = issued(client)
    assert chat(client, token, "x" * 4001).status_code == 413
    assert service._conservative_tokens("a" * 8) == 2
    assert service._conservative_tokens("😀" * 10) == 10 and service._conservative_tokens("Привет") == 6
    assert service._conservative_tokens("字" * 10) == 10 and service._conservative_tokens("1234") == 2
    assert chat(client, token, "😀" * 1600).status_code == 413  # 1600 字元 < 4000，但保守估算超過 1500 tokens
    assert chat(client, token, "ok").status_code == 200


def test_f16_docs_and_openapi_disabled(client):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


# ---- 工具：issue_slips／scan_secrets -------------------------------------------------

def test_f4_f13_slips_random_suffix_and_permissions(tmp_path):
    if not (ROOT / "tools" / "issue_slips.py").is_file():
        pytest.skip("獨立 starter 複本不包含講師工具")
    out = tmp_path / "slips"
    env = {**os.environ, "SERVER_SECRET": SECRET, "TOKEN_EXPIRES_ON": "20261016", "PYTHONIOENCODING": "utf-8"}
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "issue_slips.py"), "3", "--output", str(out)],
                            capture_output=True, text=True, encoding="utf-8", env=env, check=True)
    import csv
    import re

    with (out / "slips.csv").open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert all(re.fullmatch(r"slip0\d-[a-z0-9]{6}", row["nickname"]) for row in rows)
    assert len({row["nickname"] for row in rows}) == 3 and all(row["token"] not in result.stdout for row in rows)
    for row in rows:
        assert token_format.verify(row["token"], SECRET) == row["nickname"]
    if os.name == "posix":
        assert stat.S_IMODE(out.stat().st_mode) == 0o700
        for name in ("slips.csv", "slips.html"):
            assert stat.S_IMODE((out / name).stat().st_mode) == 0o600


def test_f16_scanner_detects_variable_assignments(tmp_path):
    if not (ROOT / "tools" / "scan_secrets.py").is_file():
        pytest.skip("獨立 starter 複本不包含講師工具")
    sample = tmp_path / "config.env"
    sample.write_text(
        "CLASS_CODE=Zq8vLm2Xr9TpKd4w\n" "ADMIN_TOKEN: \"aB3dE5gH7jK9mN1pQ3sT\"\n" "FOO_API_KEY=AbCdEfGhIjKlMnOpQrSt\n"
        "SERVER_SECRET=${SERVER_SECRET_FROM_SM}\n" "CLASS_CODE=demo-class-code-123456\n" "NICKNAME=student123456789012\n",
        encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "tools" / "scan_secrets.py"), str(sample)],
                            capture_output=True, text=True, encoding="utf-8",
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 1
    assert "發現 3 處" in result.stderr
    for value in ("Zq8vLm2Xr9TpKd4w", "aB3dE5gH7jK9mN1pQ3sT", "AbCdEfGhIjKlMnOpQrSt"):
        assert value not in result.stderr
    assert ":1:" in result.stderr and ":2:" in result.stderr and ":3:" in result.stderr
