import json
import random
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import llm
from gateway.pricing import MAX_OUTPUT_TOKENS, SHORT_ANSWER_INSTRUCTION, cost_usd, estimate_tokens, worst_cost_usd


@pytest.fixture(autouse=True)
def isolated_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("USAGE_LOG", str(tmp_path / "usage.jsonl"))
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("RPM_LIMIT", "1000")
    monkeypatch.setenv("MAX_RETRIES", "0")
    for name in ("PROVIDER", "FALLBACK", "MOCK_FAIL", "BUDGETS_FILE", "INSTRUCTOR"):
        monkeypatch.delenv(name, raising=False)
    llm._requests.clear()


def entries():
    import os

    with open(os.environ["USAGE_LOG"], encoding="utf-8") as stream:
        return [json.loads(line) for line in stream]


def test_aliases_and_configuration_errors(monkeypatch):
    assert set(llm.list_aliases()) == {"fast", "mock", "class"}
    with pytest.raises(llm.ConfigError):
        llm.chat([], alias="smart")
    monkeypatch.setenv("INSTRUCTOR", "1")
    assert "smart" in llm.list_aliases()
    monkeypatch.delenv("INSTRUCTOR")
    with pytest.raises(llm.ConfigError, match="PROVIDER"):
        llm.chat([{"role": "user", "content": "hi"}])
    with pytest.raises(llm.ConfigError, match="unknown"):
        llm.chat([], alias="unknown")
    assert [e["status"] for e in entries()] == ["error:ConfigError"] * 3


def test_mock_cost_tokens_cache_and_usage(monkeypatch):
    monkeypatch.setenv("PROVIDER", "mock")
    messages = [{"role": "user", "content": "abcdefgh"}]
    first = llm.chat(messages, tenant="alice")
    second = llm.chat(messages, tenant="alice")
    assert first.text == second.text == "Mock 回答：abcdefgh"
    assert first.provider == first.model == "mock"
    assert first.input_tokens == estimate_tokens(SHORT_ANSWER_INSTRUCTION) + 2
    assert first.output_tokens == estimate_tokens(first.text)
    assert first.cost_usd == cost_usd("mock", first.input_tokens, first.output_tokens)
    assert not first.cached and second.cached
    assert not first.fallback_used and not second.fallback_used
    assert second.cost_usd == 0
    recorded = entries()
    assert len(recorded) == 2
    assert recorded[0]["status"] == recorded[1]["status"] == "ok"
    assert recorded[0]["cost_usd"] == first.cost_usd
    assert recorded[1]["cached"] is True
    assert recorded[1]["cost_usd"] == 0
    assert set(recorded[0]) == {
        "ts", "tenant", "alias", "provider", "model", "input_tokens",
        "output_tokens", "thoughts_tokens", "truncated", "cost_usd", "cached", "fallback_used", "status",
    }


def test_budget_exceeded_on_next_call(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER", "mock")
    budget = tmp_path / "budgets.json"
    budget.write_text('{"alice": 0, "bob": 10}', encoding="utf-8")
    monkeypatch.setenv("BUDGETS_FILE", str(budget))
    llm.chat([{"content": "first"}], tenant="alice")
    with pytest.raises(llm.BudgetExceeded) as exc:
        llm.chat([{"content": "second"}], tenant="alice")
    assert exc.value.tenant == "alice"
    assert llm.chat([{"content": "second"}], tenant="bob").text
    assert [e["status"] for e in entries()] == [
        "ok", "error:BudgetExceeded", "ok"
    ]


def test_fallback_after_429_without_real_gemini(monkeypatch):
    from gateway.providers import gemini

    calls = []

    def limited(messages, model):
        calls.append(model)
        raise llm.RateLimited("429", retry_after=0)

    monkeypatch.setattr(gemini, "chat", limited)
    monkeypatch.setattr(llm.time, "sleep", lambda seconds: None)
    monkeypatch.setenv("FALLBACK", "fast,mock")
    monkeypatch.setenv("MAX_RETRIES", "2")
    result = llm.chat([{"content": "hello"}], alias="fast")
    assert calls == ["gemini-3.1-flash-lite"] * 3
    assert result.fallback_used and result.provider == "mock"
    assert entries()[0]["fallback_used"]
    assert entries()[0]["alias"] == "fast"


def test_retry_after_exponential_and_exhaustion(monkeypatch):
    from gateway.providers import mock

    pauses = []
    monkeypatch.setattr(llm.time, "sleep", pauses.append)
    monkeypatch.setattr(llm.random, "uniform", random.Random(42).uniform)
    monkeypatch.setenv("MAX_RETRIES", "2")
    monkeypatch.setattr(
        mock, "chat", lambda messages: (_ for _ in ()).throw(
            llm.RateLimited("429", retry_after=2)
        ),
    )
    with pytest.raises(llm.RateLimited) as exc:
        llm.chat([{"content": "retry"}], alias="mock")
    assert exc.value.retry_after == 2
    assert pauses == pytest.approx([1.2788535969, 0.1000430209])
    assert all(0 <= pause <= ceiling for pause, ceiling in zip(pauses, [2, 4]))
    assert entries()[0]["status"] == "error:RateLimited"


def test_different_alias_does_not_share_cache(monkeypatch):
    from gateway.providers import gemini

    def offline(messages, model):
        return llm.Result("答覆", "gemini", model, 1, 1, 0.01)

    monkeypatch.setattr(gemini, "chat", offline)
    first = llm.chat([{"content": "x"}], alias="mock")
    second = llm.chat([{"content": "x"}], alias="fast")
    assert first.model == "mock" and second.model == "gemini-3.1-flash-lite"
    assert not second.cached


@pytest.mark.parametrize(
    ("failure", "error"),
    [("429", llm.RateLimited), ("timeout", llm.ProviderError), ("empty", llm.ProviderError)],
)
def test_mock_failures_are_logged(monkeypatch, failure, error):
    monkeypatch.setenv("MOCK_FAIL", failure)
    with pytest.raises(error):
        llm.chat([{"content": "fail"}], alias="mock")
    assert entries()[0]["status"] == f"error:{error.__name__}"


def test_throttle_limits_requests(monkeypatch):
    clock = [100.0]
    pauses = []
    monkeypatch.setattr(llm.time, "monotonic", lambda: clock[0])

    def sleep(seconds):
        pauses.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(llm.time, "sleep", sleep)
    llm._throttle(1)
    llm._throttle(1)
    assert pauses == [60.0]
    assert list(llm._requests) == [160.0]


def test_gemini_without_key_is_config_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(llm.ConfigError, match="GEMINI_API_KEY.*GOOGLE_API_KEY"):
        llm.chat([], alias="fast")
    assert entries()[0]["status"] == "error:ConfigError"


def test_gemini_thoughts_are_billed_and_logged(monkeypatch):
    fake_types = SimpleNamespace(
        Content=lambda **kwargs: kwargs,
        Part=SimpleNamespace(from_text=lambda text: text),
        GenerateContentConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        ThinkingConfig=lambda **kwargs: SimpleNamespace(**kwargs),
        ThinkingLevel=SimpleNamespace(LOW="LOW"),
    )
    response = SimpleNamespace(
        text="答案",
        usage_metadata=SimpleNamespace(
            prompt_token_count=5, candidates_token_count=3, thoughts_token_count=50
        ),
    )
    generate = Mock(return_value=response)
    google = ModuleType("google")
    google.genai = SimpleNamespace(Client=lambda **kwargs: SimpleNamespace(
        models=SimpleNamespace(generate_content=generate)
    ))
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", google.genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", fake_types)
    google.genai.types = fake_types
    monkeypatch.setenv("GEMINI_API_KEY", "test-only")
    monkeypatch.setenv("INSTRUCTOR", "1")
    result = llm.chat([{"role": "user", "content": "hi"}], alias="smart")
    assert result.output_tokens == 53
    assert result.thoughts_tokens == 50
    assert result.cost_usd == cost_usd("gemini-3.8-flash", 5, 53)
    assert entries()[0]["thoughts_tokens"] == 50
    config = generate.call_args.kwargs["config"].thinking_config
    assert config.thinking_level == "LOW" and config.thinking_budget is None
    generate.reset_mock()
    response.candidates = [SimpleNamespace(finish_reason="MAX_TOKENS")]
    llm.chat([{"role": "user", "content": "fast"}], alias="fast")
    assert entries()[-1]["truncated"] is True
    config = generate.call_args.kwargs["config"].thinking_config
    assert config.thinking_budget == 0 and config.thinking_level is None
    assert generate.call_args.kwargs["config"].max_output_tokens == MAX_OUTPUT_TOKENS
    assert SHORT_ANSWER_INSTRUCTION in generate.call_args.kwargs["config"].system_instruction


def test_cjk_estimate_truncation_and_reserved_cost(monkeypatch):
    assert estimate_tokens("星河" * 1400) == 2800
    assert estimate_tokens("abcdefgh") == 2
    messages = [{"role": "user", "content": "星河" * 1400}]
    result = llm.chat(messages, alias="mock")
    assert result.truncated and result.output_tokens <= MAX_OUTPUT_TOKENS
    assert worst_cost_usd("mock", messages) >= result.cost_usd
    assert entries()[0]["truncated"]


def test_cache_key_includes_instruction(monkeypatch):
    messages = [{"role": "user", "content": "hello"}]
    assert llm._cache_path("mock", llm.prepared_messages(messages)) != llm._cache_path("mock", messages)
    assert llm.chat(messages, alias="mock").cached is False
    assert llm.chat(messages, alias="mock").cached is True


def test_legacy_cache_without_thoughts_still_loads(monkeypatch):
    messages = [{"role": "user", "content": "legacy"}]
    path = llm._cache_path("mock", llm.prepared_messages(messages))
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "text": "舊快取", "provider": "mock", "model": "mock",
        "input_tokens": 1, "output_tokens": 2, "cost_usd": 0.01,
    }), encoding="utf-8")
    result = llm.chat(messages, alias="mock")
    assert result.cached and result.thoughts_tokens == 0
    assert entries()[0]["thoughts_tokens"] == 0
