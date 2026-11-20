"""Auxiliares de teste compartilhados (LLM roteirizado, chamadas, falhas de provedor).

Importado pelos módulos de teste via ``sys.path``. Não é um módulo de teste.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any
from collections.abc import Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dv_harness.llm_client import AttemptRecord, LLMCallFailed, LLMResponse, ToolCall  # noqa: E402


def make_response(
    content: str | None = None,
    calls: tuple[ToolCall, ...] | list[ToolCall] = (),
    *,
    prompt: int | None = 10,
    completion: int | None = 5,
    cost: float | None = 0.01,
    attempts: tuple[AttemptRecord, ...] = (),
    model: str = "mock",
    model_returned: str | None = "mock-returned",
) -> LLMResponse:
    total = None if prompt is None or completion is None else prompt + completion
    basis = "unknown" if cost is None else ("priced_zero_by_table" if cost == 0.0 else "priced_by_table")
    ok_attempt = (AttemptRecord(index=1, outcome="ok", wall_clock_ms=1.0),)
    return LLMResponse(
        content=content,
        tool_calls=tuple(calls),
        model=model,
        model_returned=model_returned,
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=total,
        cost_usd=cost,
        cost_basis=basis,
        provider="mock",
        api_id="mock-id",
        wall_clock_ms=1.0,
        pricing_source="mock" if cost is not None else None,
        attempts=attempts or ok_attempt,
    )


def call(call_id: str, name: str, args: dict[str, Any] | str) -> ToolCall:
    arguments = args if isinstance(args, str) else json.dumps(args)
    return ToolCall(id=call_id, name=name, arguments=arguments)


def submit(call_id: str, answer: str) -> ToolCall:
    return call(call_id, "submit", {"answer": answer})


def provider_failure(message: str = "upstream unavailable", *, attempts: int = 1,
                     error_type: type[BaseException] = RuntimeError) -> LLMCallFailed:
    """Falha de provedor já no formato ``LLMCallFailed`` (todas as tentativas com erro)."""
    cause = error_type(message)
    records = tuple(
        AttemptRecord(index=i + 1, outcome="error", wall_clock_ms=2.0, error_type=type(cause).__name__,
                      error_message=message, retryable=True, backoff_s=None)
        for i in range(attempts)
    )
    return LLMCallFailed(records, cause, wall_clock_ms=2.0 * attempts)


class ScriptedLLM:
    """LLM roteirizado. Cada item é ``LLMResponse``, exceção (``LLMCallFailed`` ou outra) ou função
    ``(messages) -> LLMResponse``. Após o fim do roteiro, repete o último item. Registra as chamadas
    com cópia profunda do payload recebido."""

    def __init__(self, script: list[Any], *, default_model: str = "mock") -> None:
        self.script = list(script)
        self.calls: list[dict[str, Any]] = []
        self.default_model = default_model

    def complete(self, messages, tools=None, model=None):
        self.calls.append({"messages": copy.deepcopy(list(messages)), "tools": tools})
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        if callable(item) and not isinstance(item, LLMResponse):
            return item(messages)
        return item


Factory = Callable[[list[dict[str, Any]]], LLMResponse]


def tool_contents(messages: list[dict[str, Any]]) -> list[str]:
    return [m["content"] for m in messages if m["role"] == "tool"]
