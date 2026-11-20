"""LLMClient: adaptador fino e multi-provedor sobre ``litellm`` com tool calling nativo.

Módulo 1 do harness modular (``feat/modular-harness-core``). Escopo deliberadamente
restrito: uma chamada de completion por invocação, com contabilização de tokens e
custo por chamada. Não contém loop de agente, executor nem gerenciamento de contexto.

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
  não fatura do provedor). Para modelo sem preço conhecido, ``cost_usd`` é ``None``.
  Nunca é reportado como ``0.0`` silenciosamente.
- Erros do provedor não são capturados aqui. Propagam como levantados pelo ``litellm``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import litellm

Message = Mapping[str, Any]
Tool = Mapping[str, Any]


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
    """Resposta normalizada de uma chamada ao modelo, com contabilização por chamada."""

    content: str | None
    tool_calls: tuple[ToolCall, ...] = field(default_factory=tuple)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float | None = None


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
        """
        effective_model = model or self.default_model
        if not effective_model:
            raise ValueError("no model: pass model= to complete() or default_model to LLMClient")
        if not messages:
            raise ValueError("messages must be a non-empty sequence")

        kwargs: dict[str, Any] = dict(self._default_params)
        if tools:
            kwargs["tools"] = list(tools)

        raw = self._completion(model=effective_model, messages=list(messages), **kwargs)

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

        return LLMResponse(
            content=getattr(message, "content", None),
            tool_calls=tool_calls,
            model=effective_model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost_usd=self._cost_or_none(raw, effective_model),
        )

    def _cost_or_none(self, raw: Any, model: str) -> float | None:
        try:
            return float(self._cost(completion_response=raw, model=model))
        except Exception:  # noqa: BLE001 - modelo sem preço na tabela do litellm
            return None
