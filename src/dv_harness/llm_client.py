"""LLMClient: adaptador fino e multi-provedor sobre ``litellm`` com tool calling nativo.

Módulo 1 do harness modular (``feat/modular-harness-core``). Escopo deliberadamente
restrito: uma chamada de completion por invocação, com contabilização de tokens, custo,
latência e metadados de origem por chamada. Não contém loop de agente, executor nem
gerenciamento de contexto.

Classificação (AGENTS.md, "Architecture default"): REUSE/WRAP. O ``litellm`` é a
capacidade de prior art que satisfaz o contrato (troca de provedor via ``model``,
tool calling nativo, contagem de tokens e custo). Este módulo apenas normaliza a
resposta para um tipo estável. Não é um gateway de modelos próprio.

Atribuições empíricas
---------------------
- Padrão arquitetural (``LLMClient`` fino sobre ``litellm`` com tool calling nativo):
  baseado no ``SWE-agent/mini-swe-agent`` (https://github.com/swe-agent/mini-swe-agent),
  que usa LiteLLM como camada de acesso a modelos. A versão 2.4.6 foi confirmada como
  publicada no PyPI. Não foi verificado changelog específico dessa versão.
- Contabilização obrigatória de tokens e custo por chamada: fundamentada no SWE-Effi
  (https://arxiv.org/abs/2509.09853), que avalia eficácia sob orçamento de tokens
  (EuTB) e de custo (EuCB).

Limitações conhecidas
---------------------
- ``cost_usd`` é calculado pela tabela de preços do ``litellm`` (estimativa local,
  não fatura do provedor). Quando o modelo não tem preço na tabela, ``cost_usd`` é
  ``None``. Esse caso é o único que vira ``None``. Outras falhas na função de custo
  propagam, para não se disfarçarem de "preço desconhecido".
- ``pricing_source`` identifica a versão do litellm que forneceu o preço. A data do
  preço não é exposta pelo litellm, e fica registrada como não medida no ``AgentLoop``.
- ``cached_input_tokens`` e ``reasoning_tokens`` são lidos de ``usage`` quando o provedor
  os expõe. Caso contrário valem ``None`` (não medido), nunca ``0``.
- ``provider`` e ``api_id`` vêm de metadados que o litellm expõe. Quando ausentes, valem
  ``None``.
- Erros do provedor não são capturados aqui. Propagam como levantados pelo ``litellm``.
  Quem captura é o ``AgentLoop``.
"""

from __future__ import annotations

import importlib.metadata
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import litellm
from litellm.exceptions import BadRequestError

Message = Mapping[str, Any]
Tool = Mapping[str, Any]

try:
    _LITELLM_VERSION = importlib.metadata.version("litellm")
except importlib.metadata.PackageNotFoundError:  # pragma: no cover - ambiente sem pacote instalado
    _LITELLM_VERSION = "unknown"
PRICING_SOURCE = f"litellm=={_LITELLM_VERSION} bundled price table"


@dataclass(frozen=True)
class ToolCall:
    """Uma chamada de ferramenta solicitada pelo modelo.

    ``arguments`` é a string JSON bruta devolvida pelo provedor. Use ``parsed_arguments``
    para obter o dicionário decodificado.
    """

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
    """Resposta normalizada de uma chamada ao modelo, com contabilização por chamada.

    Campos opcionais valem ``None`` quando não medidos. ``0`` significa medido e zero.
    """

    content: str | None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None
    provider: str | None = None
    api_id: str | None = None
    wall_clock_ms: float = 0.0
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    pricing_source: str | None = None


CompletionFn = Callable[..., Any]
CostFn = Callable[..., float]


class LLMClient:
    """Cliente fino sobre ``litellm.completion``.

    Troca de modelo por configuração: passe ``default_model`` (ex.: ``"gpt-4o-mini"``,
    ``"anthropic/claude-haiku-5-5"``, ``"ollama/qwen2.5-coder"``) ou ``model`` por chamada.

    ``completion_fn`` e ``cost_fn`` são injetáveis para teste sem chamada de API.
    Por padrão usam ``litellm.completion`` e ``litellm.completion_cost``.
    """

    def __init__(
        self,
        default_model: str | None = None,
        *,
        completion_fn: CompletionFn | None = None,
        cost_fn: CostFn | None = None,
        **default_params: Any,
    ) -> None:
        self.default_model = default_model
        self._completion = completion_fn or litellm.completion
        self._cost = cost_fn or litellm.completion_cost
        self._default_params = default_params

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Tool] | None = None,
        model: str | None = None,
    ) -> LLMResponse:
        """Executa uma chamada de completion e devolve ``LLMResponse``.

        ``tools`` só é enviado quando não vazio, pois alguns provedores rejeitam lista vazia.
        ``wall_clock_ms`` mede somente a chamada ao provedor, sem o cálculo de custo.
        """
        effective_model = model or self.default_model
        if not effective_model:
            raise ValueError("no model: pass model= to complete() or default_model to LLMClient")
        if not messages:
            raise ValueError("messages must be a non-empty sequence")

        kwargs: dict[str, Any] = dict(self._default_params)
        if tools:
            kwargs["tools"] = list(tools)

        started = time.perf_counter()
        raw = self._completion(model=effective_model, messages=list(messages), **kwargs)
        wall_clock_ms = (time.perf_counter() - started) * 1000.0

        message = raw.choices[0].message
        tool_calls = tuple(
            ToolCall(
                id=str(tc.id),
                name=tc.function.name,
                arguments=tc.function.arguments or "",
            )
            for tc in (getattr(message, "tool_calls", None) or ())
        )

        usage = getattr(raw, "usage", None)
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        cost = self._cost_or_none(raw, effective_model)

        return LLMResponse(
            content=getattr(message, "content", None),
            tool_calls=tool_calls,
            model=effective_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=cost,
            provider=_provider_of(raw),
            api_id=_str_or_none(getattr(raw, "id", None)),
            wall_clock_ms=wall_clock_ms,
            cached_input_tokens=_detail(usage, "prompt_tokens_details", "cached_tokens"),
            reasoning_tokens=_detail(usage, "completion_tokens_details", "reasoning_tokens"),
            pricing_source=PRICING_SOURCE if cost is not None else None,
        )

    def _cost_or_none(self, raw: Any, model: str) -> float | None:
        """Custo em USD, ou ``None`` só quando o litellm não tem preço para o modelo.

        Outras exceções propagam. Um bug na função de custo não pode virar "preço desconhecido".
        """
        try:
            return float(self._cost(completion_response=raw, model=model))
        except BadRequestError:
            return None


def _detail(usage: Any, container: str, field_name: str) -> int | None:
    """Lê ``usage.<container>.<field_name>``. ``None`` quando o provedor não expõe o valor."""
    details = getattr(usage, container, None)
    value = getattr(details, field_name, None) if details is not None else None
    return int(value) if isinstance(value, int) and not isinstance(value, bool) else None


def _provider_of(raw: Any) -> str | None:
    hidden = getattr(raw, "_hidden_params", None)
    if isinstance(hidden, Mapping):
        return _str_or_none(hidden.get("custom_llm_provider"))
    return None


def _str_or_none(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None
