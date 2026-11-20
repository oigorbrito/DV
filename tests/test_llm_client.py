"""Testes do Módulo 1 (LLMClient), protocolo v2: retentativas, tentativas, cost_basis e sentinelas.

Sem chamada real de API. Executar: python -m pytest tests/test_llm_client.py -v
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from support import ROOT  # noqa: F401
from dv_harness.llm_client import (
    ABORT_EXCEPTIONS,
    LLMCallFailed,
    LLMClient,
    LLMResponse,
    RetryPolicy,
    ToolCall,
)
from litellm.exceptions import AuthenticationError, BadRequestError

MESSAGES = [{"role": "user", "content": "list files"}]


def _raw(content=None, tool_calls=None, prompt=10, completion=5, model="gpt-x", rid="chatcmpl-1",
         usage_obj=None, hidden=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    if usage_obj is None and prompt is not None:
        usage_obj = SimpleNamespace(prompt_tokens=prompt, completion_tokens=completion,
                                    total_tokens=prompt + completion)
    raw = SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage_obj, model=model, id=rid)
    if hidden is not None:
        raw._hidden_params = hidden
    return raw


def _client(completion_fn, cost=0.25, retry=None, sleeps=None):
    sleep_log = sleeps if sleeps is not None else []
    return LLMClient(
        default_model="fake/model",
        completion_fn=completion_fn,
        cost_fn=lambda **_: cost,
        retry=retry or RetryPolicy(max_attempts=3, base_delay_s=1.0, multiplier=2.0, max_delay_s=60.0),
        sleep_fn=sleep_log.append,
    )


# --- contabilização da chamada bem-sucedida -------------------------------------


def test_success_fields_are_measured_and_total_is_sum():
    client = _client(lambda **_: _raw(content="ok", prompt=30, completion=12), cost=0.5)
    resp = client.complete(MESSAGES)
    assert resp.prompt_tokens == 30 and resp.completion_tokens == 12 and resp.total_tokens == 42
    assert resp.cost_usd == pytest.approx(0.5)
    assert resp.cost_basis == "priced_by_table"
    assert resp.model == "fake/model"
    assert resp.model_returned == "gpt-x"  # raw.model: modelo exato retornado pela API
    assert resp.api_id == "chatcmpl-1"
    assert resp.wall_clock_ms is not None and resp.wall_clock_ms >= 0
    assert len(resp.attempts) == 1 and resp.attempts[0].outcome == "ok"


def test_tools_forwarded_only_when_non_empty():
    seen = []

    def fake(**kwargs):
        seen.append(kwargs)
        return _raw(content="x")

    client = _client(fake)
    client.complete(MESSAGES, tools=[{"type": "function"}])
    client.complete(MESSAGES, tools=[])
    assert "tools" in seen[0] and "tools" not in seen[1]


def test_tool_calls_are_parsed():
    tc = SimpleNamespace(id="c1", function=SimpleNamespace(name="read_file", arguments='{"path": "a"}'))
    resp = _client(lambda **_: _raw(content=None, tool_calls=[tc])).complete(MESSAGES)
    assert resp.tool_calls == (ToolCall(id="c1", name="read_file", arguments='{"path": "a"}'),)
    assert resp.tool_calls[0].parsed_arguments == {"path": "a"}


# --- I6: usage ausente é None, nunca zero ---------------------------------------


def test_missing_usage_is_none_not_zero():
    resp = _client(lambda **_: _raw(content="ok", usage_obj=None, prompt=None)).complete(MESSAGES)
    assert resp.prompt_tokens is None
    assert resp.completion_tokens is None
    assert resp.total_tokens is None


def test_partial_usage_makes_total_none():
    usage = SimpleNamespace(prompt_tokens=10)  # sem completion_tokens
    resp = _client(lambda **_: _raw(content="ok", usage_obj=usage)).complete(MESSAGES)
    assert resp.prompt_tokens == 10
    assert resp.completion_tokens is None
    assert resp.total_tokens is None


def test_boolean_is_not_a_token_count():
    usage = SimpleNamespace(prompt_tokens=True, completion_tokens=2)
    resp = _client(lambda **_: _raw(content="ok", usage_obj=usage)).complete(MESSAGES)
    assert resp.prompt_tokens is None


def test_cached_and_reasoning_none_when_not_exposed_and_zero_when_measured_zero():
    absent = _client(lambda **_: _raw(content="ok")).complete(MESSAGES)
    assert absent.cached_input_tokens is None and absent.reasoning_tokens is None
    usage = SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                            completion_tokens_details=None)
    measured = _client(lambda **_: _raw(content="ok", usage_obj=usage)).complete(MESSAGES)
    assert measured.cached_input_tokens == 0
    assert measured.reasoning_tokens is None


def test_llm_response_defaults_are_none_not_sentinels():
    resp = LLMResponse(content=None)
    assert resp.model is None and resp.prompt_tokens is None and resp.completion_tokens is None
    assert resp.total_tokens is None and resp.cost_usd is None and resp.wall_clock_ms is None


# --- cost_basis --------------------------------------------------------------------


def test_zero_table_price_is_priced_zero_not_measured_free():
    resp = _client(lambda **_: _raw(content="ok"), cost=0.0).complete(MESSAGES)
    assert resp.cost_usd == 0.0
    assert resp.cost_basis == "priced_zero_by_table"


def test_unknown_price_is_none_and_basis_unknown():
    def no_price(completion_response, model):
        raise BadRequestError(message="no price", model=model, llm_provider="x")

    client = LLMClient(default_model="m", completion_fn=lambda **_: _raw(content="ok"), cost_fn=no_price,
                       sleep_fn=lambda s: None)
    resp = client.complete(MESSAGES)
    assert resp.cost_usd is None and resp.cost_basis == "unknown" and resp.pricing_source is None


def test_unexpected_cost_error_propagates_not_wrapped_as_unknown():
    def buggy(completion_response, model):
        raise RuntimeError("bug in cost function")

    client = LLMClient(default_model="m", completion_fn=lambda **_: _raw(content="ok"), cost_fn=buggy,
                       sleep_fn=lambda s: None)
    with pytest.raises(RuntimeError, match="bug in cost function"):
        client.complete(MESSAGES)


# --- retentativas e tentativas (A4) ---------------------------------------------------


def test_transient_error_is_retried_and_both_attempts_recorded():
    state = {"n": 0}
    sleeps: list[float] = []

    def flaky(**_):
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("429 rate limited")
        return _raw(content="ok", prompt=4, completion=1)

    resp = _client(flaky, sleeps=sleeps).complete(MESSAGES)
    assert [a.outcome for a in resp.attempts] == ["error", "ok"]
    assert resp.attempts[0].error_type == "RuntimeError"
    assert resp.attempts[0].retryable is True
    assert resp.attempts[0].backoff_s == pytest.approx(1.0)
    assert sleeps == [pytest.approx(1.0)]
    assert resp.total_tokens == 5


def test_abort_exception_is_not_retried():
    calls = {"n": 0}
    sleeps: list[float] = []

    def auth_fail(**_):
        calls["n"] += 1
        raise AuthenticationError(message="bad key", llm_provider="x", model="m")

    client = _client(auth_fail, sleeps=sleeps)
    with pytest.raises(LLMCallFailed) as info:
        client.complete(MESSAGES)
    assert calls["n"] == 1
    assert sleeps == []
    assert len(info.value.attempts) == 1
    assert info.value.attempts[0].retryable is False
    assert info.value.attempts[0].backoff_s is None


def test_exhausted_retries_raise_with_every_attempt_and_no_final_sleep():
    sleeps: list[float] = []

    def always_fail(**_):
        raise RuntimeError("timeout")

    client = _client(always_fail, sleeps=sleeps, retry=RetryPolicy(max_attempts=3, base_delay_s=1.0, multiplier=2.0))
    with pytest.raises(LLMCallFailed) as info:
        client.complete(MESSAGES)
    attempts = info.value.attempts
    assert len(attempts) == 3
    assert all(a.outcome == "error" for a in attempts)
    assert [a.backoff_s for a in attempts] == [1.0, 2.0, None]
    assert sleeps == [1.0, 2.0]
    assert info.value.wall_clock_ms >= 0
    assert isinstance(info.value.cause, RuntimeError)


def test_backoff_is_exponential_and_capped():
    policy = RetryPolicy(max_attempts=10, base_delay_s=1.0, multiplier=2.0, max_delay_s=5.0)
    assert [policy.delay_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 5.0, 5.0]


def test_retry_policy_validates_parameters():
    with pytest.raises(ValueError):
        RetryPolicy(max_attempts=0)
    with pytest.raises(ValueError):
        RetryPolicy(multiplier=0.5)


def test_abort_list_matches_reference_classes():
    names = {t.__name__ for t in ABORT_EXCEPTIONS}
    assert names == {"UnsupportedParamsError", "NotFoundError", "PermissionDeniedError",
                     "ContextWindowExceededError", "AuthenticationError"}


def test_retry_policy_describe_is_serialisable():
    desc = RetryPolicy().describe()
    assert desc["max_attempts"] == 5 and "AuthenticationError" in desc["abort_on"]


# --- validações e propagação ------------------------------------------------------


def test_missing_model_and_empty_messages_raise():
    client = LLMClient(completion_fn=lambda **_: _raw(content="x"), cost_fn=lambda **_: 0.0)
    with pytest.raises(ValueError, match="no model"):
        client.complete(MESSAGES)
    client2 = LLMClient("m", completion_fn=lambda **_: _raw(content="x"), cost_fn=lambda **_: 0.0)
    with pytest.raises(ValueError, match="non-empty"):
        client2.complete([])


def test_model_per_call_overrides_default():
    seen = []

    def fake(**kwargs):
        seen.append(kwargs["model"])
        return _raw(content="x")

    client = _client(fake)
    assert client.complete(MESSAGES, model="other").model == "other"
    assert seen == ["other"]


def test_provider_from_hidden_params_when_exposed():
    resp = _client(lambda **_: _raw(content="x", hidden={"custom_llm_provider": "openai"})).complete(MESSAGES)
    assert resp.provider == "openai"
    assert _client(lambda **_: _raw(content="x")).complete(MESSAGES).provider is None


def test_wall_clock_measures_the_call():
    import time as _t

    def slow(**_):
        _t.sleep(0.02)
        return _raw(content="ok")

    assert _client(slow).complete(MESSAGES).wall_clock_ms >= 10.0


def test_tool_arguments_valid_json_but_not_object_are_rejected():
    tc = ToolCall(id="c", name="read_file", arguments="[1, 2]")
    with pytest.raises(ValueError, match="must decode to an object"):
        _ = tc.parsed_arguments
    assert ToolCall(id="c", name="read_file", arguments="").parsed_arguments == {}
