"""Testes do Módulo 1 (LLMClient). Sem chamada real de API.

Executar com (a partir da raiz do repositório, com litellm e pytest instalados):
    python -m pytest tests/test_llm_client.py -v

As respostas são mocks com o formato de ``litellm.ModelResponse``. O teste de custo usa a
tabela de preços local do litellm (sem rede), com ``gpt-4o-mini`` como referência.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dv_harness.llm_client import LLMClient, LLMResponse, ToolCall  # noqa: E402

MESSAGES = [{"role": "user", "content": "list files"}]
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "run a shell command",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    }
]


def _raw(content=None, tool_calls=None, prompt=0, completion=0, total=None):
    """Forma mínima compatível com ``litellm.ModelResponse`` (choices[0].message, usage)."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage = SimpleNamespace(
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion if total is None else total,
    )
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


def _tc(call_id, name, arguments):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def _client(raw, cost=0.0123, recorder=None):
    def fake_completion(**kwargs):
        if recorder is not None:
            recorder.append(kwargs)
        return raw

    def fake_cost(completion_response, model):
        return cost

    return LLMClient(
        default_model="fake/model",
        completion_fn=fake_completion,
        cost_fn=fake_cost,
    )


# --- tool_calls -------------------------------------------------------------


def test_returns_tool_calls_with_id_name_and_arguments():
    raw = _raw(
        tool_calls=[
            _tc("call_1", "bash", '{"command": "ls -la"}'),
            _tc("call_2", "bash", '{"command": "pwd"}'),
        ],
        prompt=10,
        completion=5,
    )
    resp = _client(raw).complete(MESSAGES, tools=TOOLS)

    assert isinstance(resp, LLMResponse)
    assert resp.content is None
    assert resp.tool_calls == (
        ToolCall(id="call_1", name="bash", arguments='{"command": "ls -la"}'),
        ToolCall(id="call_2", name="bash", arguments='{"command": "pwd"}'),
    )
    assert resp.tool_calls[0].parsed_arguments == {"command": "ls -la"}


def test_no_tool_calls_returns_empty_tuple_and_content():
    resp = _client(_raw(content="done", tool_calls=None, prompt=3, completion=2)).complete(MESSAGES)
    assert resp.tool_calls == ()
    assert resp.content == "done"


def test_tools_are_forwarded_to_provider_when_given():
    recorder: list[dict] = []
    _client(_raw(content="x"), recorder=recorder).complete(MESSAGES, tools=TOOLS)
    assert recorder[0]["tools"] == TOOLS
    assert recorder[0]["messages"] == MESSAGES
    assert recorder[0]["model"] == "fake/model"


def test_empty_tools_are_not_sent():
    recorder: list[dict] = []
    _client(_raw(content="x"), recorder=recorder).complete(MESSAGES, tools=[])
    assert "tools" not in recorder[0]


def test_invalid_tool_arguments_raise_on_parse():
    raw = _raw(tool_calls=[_tc("c", "bash", "not-json")])
    tc = _client(raw).complete(MESSAGES).tool_calls[0]
    with pytest.raises(ValueError):
        tc.parsed_arguments


# --- contabilização de tokens -------------------------------------------------


def test_token_totals_are_sum_of_prompt_and_completion():
    raw = _raw(content="ok", prompt=1200, completion=340)
    resp = _client(raw).complete(MESSAGES)
    assert resp.prompt_tokens == 1200
    assert resp.completion_tokens == 340
    assert resp.total_tokens == 1540


def test_total_is_recomputed_not_trusted_from_provider():
    # Provedor reporta total inconsistente: o contrato usa a soma, não o valor do provedor.
    raw = _raw(content="ok", prompt=100, completion=50, total=999)
    assert _client(raw).complete(MESSAGES).total_tokens == 150


def test_missing_usage_counts_as_zero():
    raw = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="a"))], usage=None)
    resp = _client(raw).complete(MESSAGES)
    assert (resp.prompt_tokens, resp.completion_tokens, resp.total_tokens) == (0, 0, 0)


# --- cost_usd -----------------------------------------------------------------


def test_cost_is_taken_from_cost_function_with_the_response_and_model():
    seen = {}

    def fake_cost(completion_response, model):
        seen["response"] = completion_response
        seen["model"] = model
        return 0.0456

    raw = _raw(content="ok", prompt=10, completion=10)
    client = LLMClient(
        default_model="fake/model",
        completion_fn=lambda **_: raw,
        cost_fn=fake_cost,
    )
    resp = client.complete(MESSAGES)
    assert resp.cost_usd == pytest.approx(0.0456)
    assert seen == {"response": raw, "model": "fake/model"}


def test_unknown_price_yields_none_not_zero():
    def failing_cost(completion_response, model):
        raise litellm.exceptions.BadRequestError(message="no price", model=model, llm_provider="x")

    client = LLMClient(
        default_model="fake/model",
        completion_fn=lambda **_: _raw(content="ok"),
        cost_fn=failing_cost,
    )
    assert client.complete(MESSAGES).cost_usd is None


def test_cost_with_litellm_price_table_is_offline_and_exact():
    # gpt-4o-mini: 0,15 USD/1M entrada e 0,60 USD/1M saída -> 1000*0.15e-6 + 500*0.60e-6 = 0.00045
    raw = litellm.ModelResponse(
        model="gpt-4o-mini",
        choices=[{"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
        usage={"prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500},
    )
    client = LLMClient(default_model="gpt-4o-mini", completion_fn=lambda **_: raw)
    resp = client.complete(MESSAGES)
    assert resp.total_tokens == 1500
    assert resp.cost_usd == pytest.approx(0.00045, rel=1e-9)


# --- configuração de modelo ---------------------------------------------------


def test_model_per_call_overrides_default():
    recorder: list[dict] = []

    def fake_completion(**kwargs):
        recorder.append(kwargs)
        return _raw(content="x")

    client = LLMClient(
        default_model="default/model",
        completion_fn=fake_completion,
        cost_fn=lambda **_: 0.0,
    )
    resp = client.complete(MESSAGES, model="other/model")
    assert recorder[0]["model"] == "other/model"
    assert resp.model == "other/model"


def test_missing_model_raises():
    client = LLMClient(completion_fn=lambda **_: _raw(content="x"), cost_fn=lambda **_: 0.0)
    with pytest.raises(ValueError, match="no model"):
        client.complete(MESSAGES)


def test_empty_messages_raise():
    client = _client(_raw(content="x"))
    with pytest.raises(ValueError, match="non-empty"):
        client.complete([])


def test_provider_errors_propagate_unchanged():
    def boom(**_):
        raise RuntimeError("provider down")

    client = LLMClient(default_model="fake/model", completion_fn=boom, cost_fn=lambda **_: 0.0)
    with pytest.raises(RuntimeError, match="provider down"):
        client.complete(MESSAGES)
