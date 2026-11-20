"""AgentLoop: loop mínimo que compõe LLMClient, ToolExecutor e ContextManager (Módulo 4).

Um passo = uma chamada ao LLM. Se a resposta não tiver ``tool_calls``, o loop termina com
``status="completed"``. Caso contrário, despacha cada chamada para o ``ToolExecutor``,
anexa as observações como ``role="tool"`` e verifica os limites.

Erros de ferramenta (nome inexistente, JSON inválido, argumentos errados, exceção
inesperada) viram observação para o modelo se recuperar. Não derrubam o loop.
Erros do provedor de LLM propagam, como no Módulo 1.

Limites são verificados ao fim de cada turno. Por isso a última chamada pode ultrapassar
``max_tokens`` ou ``max_cost_usd`` antes de o loop parar. Se ``max_cost_usd`` estiver
definido e algum passo tiver custo desconhecido (``cost_usd=None``), o orçamento não pode
ser verificado. Nesse caso o loop para com ``status="budget_unverifiable"``. Custo
desconhecido nunca é tratado como ``0.0``.

``chars_saved_by_masking`` soma, por chamada ao LLM, os caracteres que o ContextManager
deixou de enviar. Uma mesma observação antiga pode contar em várias chamadas. O valor
mede o contexto não enviado em todas as chamadas, não o número de caracteres únicos.

Não há roteador, memória, planejamento nem sumarização. Não é um orquestrador.

Atribuições empíricas
---------------------
- Loop mínimo com limites de passos e de custo e recuperação de erro via observação:
  padrão do ``SWE-agent/mini-swe-agent`` (https://github.com/SWE-agent/mini-swe-agent),
  versão 2.4.6 (``src/minisweagent/__init__.py``). Em ``agents/default.py``, ``step_limit``
  (padrão 0, sem limite) e ``cost_limit`` (padrão 3.0) são verificados antes de cada
  chamada e levantam ``LimitsExceeded``. ``max_consecutive_format_errors`` (padrão 3)
  encerra após erros consecutivos. A mensagem exata de erro de formato devolvida ao modelo
  não foi verificada, e não é copiada aqui.
- Composição sobre as 4 primitivas: alinhada ao ``earendil-works/pi``
  (https://github.com/earendil-works/pi). Verificado por resumo de busca.
- Contabilização agregada de tokens e orçamento: SWE-Effi (arXiv:2509.09853, métricas
  EuTB e EuCB). Na métrica primária do repositório, ``TOTAL_SYSTEM_TOKENS /
  VERIFIED_SOLVED_TASK`` (``experiments/p1/run-accounting-contract-v1.json``,
  ``docs/PROJECT-OBJECTIVE.md``), este módulo produz o numerador ``total_tokens`` por
  execução. A razão em si depende de verificação independente, que não está no loop.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from dv_harness.context_manager import ContextManager
from dv_harness.llm_client import LLMResponse, ToolCall
from dv_harness.tool_executor import TOOL_SCHEMAS, ToolExecutor, ToolResult

STATUS_COMPLETED = "completed"
STATUS_MAX_STEPS = "max_steps_reached"
STATUS_BUDGET = "budget_exceeded"
STATUS_UNVERIFIABLE = "budget_unverifiable"

DEFAULT_MAX_STEPS = 30

_TOOL_NAMES = ("read_file", "write_file", "edit_file", "run_bash")


@dataclass
class AgentResult:
    status: str
    final_answer: str | None
    steps: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cost_usd: float | None
    chars_saved_by_masking: int
    messages: list[dict[str, Any]] = field(default_factory=list)


class AgentLoop:
    """Compõe ``llm`` (com ``complete``), ``executor`` (ToolExecutor) e ``context`` (ContextManager)."""

    def __init__(self, llm: Any, executor: ToolExecutor, context: ContextManager) -> None:
        self.llm = llm
        self.executor = executor
        self.context = context

    def run(
        self,
        task: str,
        system_prompt: str,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
    ) -> AgentResult:
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if max_tokens is not None and max_tokens < 1:
            raise ValueError("max_tokens must be >= 1 when set")
        if max_cost_usd is not None and max_cost_usd <= 0:
            raise ValueError("max_cost_usd must be > 0 when set")

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        steps = 0
        prompt_tokens = completion_tokens = total_tokens = 0
        cost: float | None = 0.0
        cost_unknown = False
        chars_saved = 0

        while True:
            prepared = self.context.prepare(messages)
            chars_saved += prepared.stats.chars_saved
            response: LLMResponse = self.llm.complete(prepared.messages, tools=TOOL_SCHEMAS)
            steps += 1

            prompt_tokens += response.prompt_tokens
            completion_tokens += response.completion_tokens
            total_tokens += response.total_tokens
            if response.cost_usd is None:
                cost_unknown = True
            elif not cost_unknown:
                cost += response.cost_usd

            if not response.tool_calls:
                messages.append(_assistant_message(response))  # log completo inclui a resposta final
                return self._result(STATUS_COMPLETED, response.content or "", steps,
                                    prompt_tokens, completion_tokens, total_tokens,
                                    None if cost_unknown else cost, chars_saved, messages)

            messages.append(_assistant_message(response))
            for call in response.tool_calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "name": call.name,
                    "content": self._dispatch(call),
                })

            # Verificação de limites ao fim de cada turno.
            if max_cost_usd is not None and cost_unknown:
                return self._result(STATUS_UNVERIFIABLE, None, steps, prompt_tokens,
                                    completion_tokens, total_tokens, None, chars_saved, messages)
            if max_tokens is not None and total_tokens >= max_tokens:
                return self._result(STATUS_BUDGET, None, steps, prompt_tokens,
                                    completion_tokens, total_tokens,
                                    None if cost_unknown else cost, chars_saved, messages)
            if max_cost_usd is not None and cost >= max_cost_usd:
                return self._result(STATUS_BUDGET, None, steps, prompt_tokens,
                                    completion_tokens, total_tokens, cost, chars_saved, messages)
            if steps >= max_steps:
                return self._result(STATUS_MAX_STEPS, None, steps, prompt_tokens,
                                    completion_tokens, total_tokens,
                                    None if cost_unknown else cost, chars_saved, messages)

    # --- internos ------------------------------------------------------------

    @staticmethod
    def _result(status, final_answer, steps, p, c, t, cost, saved, messages) -> AgentResult:
        return AgentResult(
            status=status,
            final_answer=final_answer,
            steps=steps,
            total_prompt_tokens=p,
            total_completion_tokens=c,
            total_tokens=t,
            total_cost_usd=cost,
            chars_saved_by_masking=saved,
            messages=messages,
        )

    def _dispatch(self, call: ToolCall) -> str:
        """Executa uma chamada e devolve a observação em texto. Nunca levanta exceção."""
        if call.name not in _TOOL_NAMES:
            return f"error: unknown tool '{call.name}'. Available tools: {', '.join(_TOOL_NAMES)}"
        try:
            args = call.parsed_arguments
        except (ValueError, json.JSONDecodeError) as exc:
            return f"error: invalid JSON arguments for {call.name}: {exc}"
        try:
            method = getattr(self.executor, call.name)
            result: ToolResult = method(**args)
        except TypeError as exc:
            return f"error: invalid arguments for {call.name}: {exc}"
        except Exception as exc:  # noqa: BLE001 - erro de ferramenta vira observação, não derruba o loop
            return f"error: {call.name} failed: {type(exc).__name__}: {exc}"
        return _format_observation(result)


def _assistant_message(response: LLMResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.content,
        "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in response.tool_calls
        ],
    }


def _format_observation(result: ToolResult) -> str:
    if result.ok:
        return result.output
    head = f"error: {result.error}" if result.error else "error"
    return f"{head}\n{result.output}".rstrip() if result.output else head
