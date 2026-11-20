"""LLMClient: adaptador fino e multi-provedor sobre ``litellm`` com tool calling nativo.

Módulo 1 do harness modular, revisado no protocolo p1 v2.

Escopo: uma chamada lógica ao modelo por invocação, com retentativas por política
(backoff exponencial, lista de abortos) e registro de **cada tentativa**, inclusive as
que falharam. Não contém loop de agente, executor nem gerenciamento de contexto.

Classificação (AGENTS.md, "Architecture default"): REUSE/WRAP. O ``litellm`` satisfaz o
contrato de troca de provedor, tool calling e contagem. Este módulo normaliza a resposta
e aplica a política de retentativa.

Atribuições empíricas
---------------------
- Padrão ``LLMClient`` fino sobre ``litellm`` com tool calling nativo:
  ``SWE-agent/mini-swe-agent`` 2.4.6 (https://github.com/SWE-agent/mini-swe-agent),
  ``src/minisweagent/models/litellm_model.py``.
- Lista de abortos (sem retentativa): mesma lista de ``litellm_model.py:50-57``
  (``UnsupportedParamsError``, ``NotFoundError``, ``PermissionDeniedError``,
  ``ContextWindowExceededError``, ``AuthenticationError``, ``KeyboardInterrupt``).
- Política de retentativa com backoff exponencial: ``models/utils/retry.py:19-25``
  do mini (tenacity). Diferenças deliberadas: backoff determinístico, sem jitter, para
  reprodutibilidade, e ``max_attempts=5`` (mini 10, ctrap 20). Ambas as referências
  **não** contabilizam o custo das tentativas que falharam; aqui isso é obrigatório.
- Contabilização de tokens e custo por chamada: SWE-Effi (arXiv:2509.09853).

Semântica de sentinelas (invariante I6)
--------------------------------------
- Campo ``None`` significa **não medido**. ``0`` significa medido e zero.
- Usage ausente do provedor resulta em ``None``, nunca em ``0``.
- ``cost_basis``: ``priced_by_table`` (custo > 0 da tabela), ``priced_zero_by_table``
  (``0.0`` da tabela, ex.: modelos ``ollama/*``, que a tabela cadastra com preço zero),
  ou ``unknown`` (sem preço). Zero de tabela não é custo medido nem custo zero real.
- ``wall_clock_ms`` inclui todas as tentativas e as esperas de backoff.
"""

from __future__ import annotations

import importlib.metadata
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import litellm
from litellm.exceptions import (
    AuthenticationError,
    BadRequestError,
    ContextWindowExceededError,
    NotFoundError,
    PermissionDeniedError,
    UnsupportedParamsError,
)

from dv_harness.auth_provider import AuthCredentials, AuthProvider, sanitize_auth_secrets
from dv_harness.protocol import COST_BASIS_SUBSCRIPTION, cost_basis_for

Message = Mapping[str, Any]
Tool = Mapping[str, Any]

try:
    _LITELLM_VERSION = importlib.metadata.version("litellm")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover
    _LITELLM_VERSION = "unknown"
PRICING_SOURCE = f"litellm=={_LITELLM_VERSION} bundled price table"

# Lista de abortos de litellm_model.py:50-57 (KeyboardInterrupt não é Exception e não entra aqui).
ABORT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    UnsupportedParamsError,
    NotFoundError,
    PermissionDeniedError,
    ContextWindowExceededError,
    AuthenticationError,
)

ERROR_MAX_CHARS = 300


@dataclass(frozen=True)
class RetryPolicy:
    """Política de retentativa. Backoff ``base * multiplier**(n-1)`` limitado a ``max_delay_s``."""

    max_attempts: int = 5
    base_delay_s: float = 1.0
    multiplier: float = 2.0
    max_delay_s: float = 60.0
    abort_on: tuple[type[BaseException], ...] = ABORT_EXCEPTIONS

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.base_delay_s < 0 or self.max_delay_s < 0 or self.multiplier < 1:
            raise ValueError("invalid backoff parameters")

    def is_retryable(self, exc: BaseException) -> bool:
        return not isinstance(exc, self.abort_on)

    def delay_for(self, failed_attempt: int) -> float:
        """Espera após a tentativa de número ``failed_attempt`` (1-based)."""
        return min(self.max_delay_s, self.base_delay_s * (self.multiplier ** (failed_attempt - 1)))

    def describe(self) -> dict[str, Any]:
        return {
            "max_attempts": self.max_attempts,
            "base_delay_s": self.base_delay_s,
            "multiplier": self.multiplier,
            "max_delay_s": self.max_delay_s,
            "abort_on": [t.__name__ for t in self.abort_on],
        }


@dataclass(frozen=True)
class AttemptRecord:
    """Uma tentativa de chamada. Tentativas que falharam não têm uso de tokens conhecido."""

    index: int
    outcome: str  # "ok" | "error"
    wall_clock_ms: float
    error_type: str | None = None
    error_message: str | None = None
    retryable: bool | None = None
    backoff_s: float | None = None


class LLMCallFailed(Exception):
    """Todas as tentativas falharam, ou a falha não é retentável. Carrega tentativas e tempo total."""

    def __init__(
        self,
        attempts: tuple[AttemptRecord, ...],
        cause: BaseException,
        wall_clock_ms: float,
    ) -> None:
        self.attempts = attempts
        self.cause = cause
        self.wall_clock_ms = wall_clock_ms
        super().__init__(f"{type(cause).__name__}: {_truncate(sanitize_auth_secrets(str(cause)))}")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str

    @property
    def parsed_arguments(self) -> dict[str, Any]:
        value = json.loads(self.arguments) if self.arguments else {}
        if not isinstance(value, dict):
            raise ValueError(f"tool call arguments must decode to an object, got {type(value).__name__}")
        return value


@dataclass(frozen=True)
class LLMResponse:
    """Resposta normalizada e contabilizada de uma chamada lógica (com suas tentativas).

    Campos ``None`` significam não medidos.
    """

    content: str | None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    model: str | None = None
    model_returned: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cost_usd: float | None = None
    cost_basis: str = "unknown"
    provider: str | None = None
    api_id: str | None = None
    wall_clock_ms: float | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    pricing_source: str | None = None
    attempts: tuple[AttemptRecord, ...] = field(default_factory=tuple)


CompletionFn = Callable[..., Any]
CostFn = Callable[..., float]
SleepFn = Callable[[float], None]


class LLMClient:
    """Cliente fino sobre ``litellm.completion`` com política de retentativa."""

    def __init__(
        self,
        default_model: str | None = None,
        *,
        auth_provider: AuthProvider | None = None,
        completion_fn: CompletionFn | None = None,
        cost_fn: CostFn | None = None,
        retry: RetryPolicy | None = None,
        sleep_fn: SleepFn | None = None,
        **default_params: Any,
    ) -> None:
        self.default_model = default_model
        self.auth_provider = auth_provider
        self._completion = completion_fn or litellm.completion
        self._cost = cost_fn or litellm.completion_cost
        self.retry = retry or RetryPolicy()
        self._sleep = sleep_fn or time.sleep
        self._default_params = default_params

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """Executa a chamada lógica, com retentativas, e devolve ``LLMResponse``.

        Levanta ``LLMCallFailed`` (com as tentativas) quando a falha não é retentável ou
        quando o limite de tentativas se esgota. Outras exceções, como erro de custo ou de
        parsing da resposta, propagam como bugs.
        """
        effective_model = model or self.default_model
        if not effective_model:
            raise ValueError("no model: pass model= to complete() or default_model to LLMClient")
        if not messages:
            raise ValueError("messages must be a non-empty sequence")

        kwargs: dict[str, Any] = dict(self._default_params)
        if tools:
            kwargs["tools"] = list(tools)

        auth_creds: AuthCredentials | None = None
        if self.auth_provider:
            auth_creds = self.auth_provider.resolve_auth(effective_model)
            if auth_creds.headers:
                current_headers = dict(kwargs.get("extra_headers") or {})
                current_headers.update(auth_creds.headers)
                kwargs["extra_headers"] = current_headers
            if auth_creds.api_key:
                kwargs["api_key"] = auth_creds.api_key

        attempts: list[AttemptRecord] = []
        call_started = time.perf_counter()
        raw: Any = None
        attempt_no = 0
        while True:
            attempt_no += 1
            started = time.perf_counter()
            try:
                raw = self._completion(model=effective_model, messages=list(messages), **kwargs)
            except Exception as exc:  # noqa: BLE001 - fronteira do provedor; registrado, nunca silenciado
                elapsed = (time.perf_counter() - started) * 1000.0
                retryable = self.retry.is_retryable(exc)
                more = retryable and attempt_no < self.retry.max_attempts
                backoff = self.retry.delay_for(attempt_no) if more else None
                attempts.append(AttemptRecord(
                    index=attempt_no,
                    outcome="error",
                    wall_clock_ms=elapsed,
                    error_type=type(exc).__name__,
                    error_message=_truncate(sanitize_auth_secrets(str(exc))),
                    retryable=retryable,
                    backoff_s=backoff,
                ))
                if not more:
                    total_ms = (time.perf_counter() - call_started) * 1000.0
                    raise LLMCallFailed(tuple(attempts), exc, total_ms) from exc
                self._sleep(backoff)  # type: ignore[arg-type]
                continue
            attempts.append(AttemptRecord(
                index=attempt_no,
                outcome="ok",
                wall_clock_ms=(time.perf_counter() - started) * 1000.0,
            ))
            break
        wall_clock_ms = (time.perf_counter() - call_started) * 1000.0

        message = raw.choices[0].message
        tool_calls = tuple(
            ToolCall(id=str(tc.id), name=tc.function.name, arguments=tc.function.arguments or "")
            for tc in (getattr(message, "tool_calls", None) or ())
        )

        usage = getattr(raw, "usage", None)
        prompt_tokens = _int_or_none(getattr(usage, "prompt_tokens", None))
        completion_tokens = _int_or_none(getattr(usage, "completion_tokens", None))
        total_tokens = (
            None if prompt_tokens is None or completion_tokens is None else prompt_tokens + completion_tokens
        )

        is_sub = (auth_creds.cost_basis == COST_BASIS_SUBSCRIPTION) if auth_creds else False
        if is_sub:
            cost = None
            cost_basis = COST_BASIS_SUBSCRIPTION
            pricing_source = auth_creds.pricing_source if auth_creds else "subscription_session"
        else:
            cost = self._cost_or_none(raw, effective_model)
            cost_basis = cost_basis_for(cost)
            pricing_source = PRICING_SOURCE if cost is not None else None

        return LLMResponse(
            content=getattr(message, "content", None),
            tool_calls=tool_calls,
            model=effective_model,
            model_returned=_str_or_none(getattr(raw, "model", None)),
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost,
            cost_basis=cost_basis,
            provider=_provider_of(raw),
            api_id=_str_or_none(getattr(raw, "id", None)),
            wall_clock_ms=wall_clock_ms,
            cached_input_tokens=_detail(usage, "prompt_tokens_details", "cached_tokens"),
            reasoning_tokens=_detail(usage, "completion_tokens_details", "reasoning_tokens"),
            pricing_source=pricing_source,
            attempts=tuple(attempts),
        )

    def _cost_or_none(self, raw: Any, model: str) -> float | None:
        """Custo em USD, ou ``None`` só quando o litellm não tem preço para o modelo.

        Outras exceções propagam. Bug na função de custo não pode virar "preço desconhecido".
        """
        try:
            return float(self._cost(completion_response=raw, model=model))
        except BadRequestError:
            return None


def _truncate(text: str) -> str:
    return text if len(text) <= ERROR_MAX_CHARS else text[:ERROR_MAX_CHARS] + "..."


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _detail(usage: Any, container: str, field_name: str) -> int | None:
    details = getattr(usage, container, None)
    value = getattr(details, field_name, None) if details is not None else None
    return _int_or_none(value)


def _provider_of(raw: Any) -> str | None:
    hidden = getattr(raw, "_hidden_params", None)
    if isinstance(hidden, Mapping):
        return _str_or_none(hidden.get("custom_llm_provider"))
    return None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
