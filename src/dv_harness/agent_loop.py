"""AgentLoop: loop mínimo que compõe LLMClient, ToolExecutor e ContextManager (Módulo 4).

Um passo = uma chamada ao LLM. Ordem de cada turno:

1. ``ContextManager.prepare`` monta o payload (o log ``messages`` nunca é alterado).
2. ``llm.complete`` é chamado. Exceções viram ``status="provider_error"``, com os totais
   acumulados até ali preservados.
3. A resposta é contabilizada e ``_check_budgets`` roda **antes** de qualquer ferramenta.
   Se um orçamento estourar, as ``tool_calls`` do turno não são executadas. Elas recebem
   observação ``not executed``, para o log manter o protocolo.
4. Sem ``tool_calls``, o loop termina com ``completed``.
5. Com ``tool_calls``, cada chamada vai ao ``ToolExecutor``. Erros viram observação
   ``role="tool"``, e o loop continua. Depois verifica-se ``max_steps``.

``final_answer`` é o último texto que o modelo emitiu, ou ``None`` se não houve texto.
Nunca é ``""``. O ``status`` diz se a execução concluiu. Um ``final_answer`` com
``status != "completed"`` não é solução verificada.

Limites: ``max_steps`` conta chamadas bem-sucedidas. ``max_tokens`` e ``max_cost_usd``
são verificados após cada chamada. Por isso a última chamada pode ultrapassar o limite
antes de a execução parar. Se ``max_cost_usd`` estiver definido e algum passo tiver custo
desconhecido (``cost_usd=None``), o orçamento não pode ser verificado. Nesse caso o status
é ``budget_unverifiable``. Custo desconhecido nunca vira ``0.0``. Se uma chamada falhar,
o custo dela também é desconhecido, então ``total_cost_usd`` vira ``None``.

Telemetria: ``calls`` tem um ``CallRecord`` por chamada, inclusive falhas. Os totais
agregam só as chamadas medidas. Quando algum total ficou incompleto, o campo
``total_cached_input_tokens`` ou ``total_reasoning_tokens`` vale ``None``. ``unmeasured``
declara explicitamente o que este núcleo não mede: verificação, roteamento, planejamento,
escalonamento, retentativas e data do preço.

``chars_saved_by_masking`` soma, por chamada, os caracteres que o ContextManager deixou
de enviar. Uma mesma observação antiga pode contar em várias chamadas. O valor mede
contexto não enviado, não caracteres únicos.

Erros de ferramenta (nome inexistente, JSON inválido, argumentos errados, exceção
inesperada, caminho fora do workspace) viram observação para o modelo se recuperar.
Não derrubam o loop.

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
- **Divergência documentada de defaults:** o mini-swe-agent usa ``step_limit=0`` (sem
  limite) e ``cost_limit=3.0``. Este módulo usa ``max_steps=30`` e ``max_cost_usd=None``,
  conforme a especificação do pedido, e não os padrões do mini-swe-agent. Os valores
  usados em benchmark devem ser passados explicitamente e registrados no protocolo.
- Composição sobre as 4 primitivas: alinhada ao ``earendil-works/pi``
  (https://github.com/earendil-works/pi). Verificado por resumo de busca.
- Contabilização agregada de tokens e orçamento: SWE-Effi (arXiv:2509.09853, métricas
  EuTB e EuCB). Na métrica primária do repositório, ``TOTAL_SYSTEM_TOKENS /
  VERIFIED_SOLVED_TASK`` (``experiments/p1/run-accounting-contract-v1.json``,
  ``docs/PROJECT-OBJECTIVE.md``), este módulo produz o numerador ``total_tokens`` por
  execução. A razão depende de verificação independente, que não está no loop.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from dv_harness.context_manager import ContextManager
from dv_harness.llm_client import LLMResponse, ToolCall
from dv_harness.tool_executor import TOOL_SCHEMAS, ToolExecutor, ToolResult

STATUS_COMPLETED = "completed"
STATUS_MAX_STEPS = "max_steps_reached"
STATUS_BUDGET = "budget_exceeded"
STATUS_UNVERIFIABLE = "budget_unverifiable"
STATUS_PROVIDER_ERROR = "provider_error"

DEFAULT_MAX_STEPS = 30
ERROR_MAX_CHARS = 300

_TOOL_NAMES = ("read_file", "write_file", "edit_file", "run_bash")

# Métricas do contrato p1-run-accounting-v1 que este núcleo não mede. Declaradas
# explicitamente, para que nenhuma seja lida como zero.
UNMEASURED_CORE: dict[str, str] = {
    "verification": "outside core: no independent verifier attached; executor status is not verification",
    "routing": "outside core: single model configured, no routing layer",
    "planning": "outside core: no planning layer",
    "escalation_handoff": "outside core: no escalation layer",
    "retry_replanning": "outside core: no retry or replanning layer; provider-internal retries are not counted",
    "pricing_date": "not exposed by litellm; pricing_source records only the litellm version",
}


@dataclass(frozen=True)
class CallRecord:
    """Telemetria de uma chamada ao LLM. Campos ``None`` significam não medido."""

    index: int
    outcome: str  # "ok" | "provider_error"
    model: str | None
    provider: str | None
    api_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    pricing_source: str | None
    wall_clock_ms: float
    error: str | None = None


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
    error: str | None = None
    provider_calls_failed: int = 0
    total_cached_input_tokens: int | None = None
    total_reasoning_tokens: int | None = None
    total_wall_clock_ms: float = 0.0
    llm_wall_clock_ms: float = 0.0
    tool_wall_clock_ms: float = 0.0
    calls: list[CallRecord] = field(default_factory=list)
    unmeasured: dict[str, str] = field(default_factory=dict)


@dataclass
class _RunState:
    """Acumulador interno de uma execução. Não é exposto."""

    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost: float = 0.0
    cost_unknown: bool = False
    cached: int | None = 0
    reasoning: int | None = 0
    chars_saved: int = 0
    llm_ms: float = 0.0
    tool_ms: float = 0.0
    failed: int = 0
    calls: list[CallRecord] = field(default_factory=list)

    def add_ok(self, r: LLMResponse) -> None:
        self.steps += 1
        self.prompt_tokens += r.prompt_tokens
        self.completion_tokens += r.completion_tokens
        self.total_tokens += r.total_tokens
        if r.cost_usd is None:
            self.cost_unknown = True
        elif not self.cost_unknown:
            self.cost += r.cost_usd
        if self.cached is not None:
            self.cached = None if r.cached_input_tokens is None else self.cached + r.cached_input_tokens
        if self.reasoning is not None:
            self.reasoning = None if r.reasoning_tokens is None else self.reasoning + r.reasoning_tokens
        self.llm_ms += r.wall_clock_ms
        self.calls.append(CallRecord(
            index=len(self.calls) + 1,
            outcome="ok",
            model=r.model or None,
            provider=r.provider,
            api_id=r.api_id,
            prompt_tokens=r.prompt_tokens,
            completion_tokens=r.completion_tokens,
            total_tokens=r.total_tokens,
            cached_input_tokens=r.cached_input_tokens,
            reasoning_tokens=r.reasoning_tokens,
            cost_usd=r.cost_usd,
            pricing_source=r.pricing_source,
            wall_clock_ms=r.wall_clock_ms,
        ))

    def add_failure(self, model: str | None, error: str, wall_clock_ms: float) -> None:
        # Uso de uma chamada que falhou não é conhecido. Tokens, cache, raciocínio e custo
        # ficam como não medidos. O custo vira desconhecido, para não ser somado como zero.
        self.failed += 1
        self.cost_unknown = True
        self.cached = None
        self.reasoning = None
        self.llm_ms += wall_clock_ms
        self.calls.append(CallRecord(
            index=len(self.calls) + 1,
            outcome="provider_error",
            model=model,
            provider=None,
            api_id=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            cached_input_tokens=None,
            reasoning_tokens=None,
            cost_usd=None,
            pricing_source=None,
            wall_clock_ms=wall_clock_ms,
            error=error,
        ))


def _check_budgets(state: _RunState, max_tokens: int | None, max_cost_usd: float | None) -> str | None:
    """Status de parada por orçamento, ou ``None`` se o orçamento ainda permite continuar."""
    if max_cost_usd is not None and state.cost_unknown:
        return STATUS_UNVERIFIABLE
    if max_tokens is not None and state.total_tokens >= max_tokens:
        return STATUS_BUDGET
    if max_cost_usd is not None and state.cost >= max_cost_usd:
        return STATUS_BUDGET
    return None


class AgentLoop:
    """Compõe ``llm`` (com ``complete``), ``executor`` (ToolExecutor) e ``context`` (ContextManager).

    Não guarda estado entre execuções. Cada ``run`` cria seu próprio ``_RunState`` e sua
    própria lista de mensagens.
    """

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
        state = _RunState()
        run_started = time.perf_counter()

        while True:
            prepared = self.context.prepare(messages)
            state.chars_saved += prepared.stats.chars_saved

            call_started = time.perf_counter()
            try:
                response: LLMResponse = self.llm.complete(prepared.messages, tools=TOOL_SCHEMAS)
            except Exception as exc:  # noqa: BLE001 - provedor é a fronteira; a falha é registrada, não silenciada
                elapsed_ms = (time.perf_counter() - call_started) * 1000.0
                state.add_failure(
                    model=getattr(self.llm, "default_model", None),
                    error=_error_text(exc),
                    wall_clock_ms=elapsed_ms,
                )
                return self._finish(STATUS_PROVIDER_ERROR, None, state, messages, run_started,
                                    error=_error_text(exc))

            state.add_ok(response)
            final_answer = response.content or None
            messages.append(_assistant_message(response))

            # Orçamento é checado ANTES de executar qualquer ferramenta do turno.
            budget_status = _check_budgets(state, max_tokens, max_cost_usd)
            if budget_status is not None:
                for call in response.tool_calls:
                    messages.append(_tool_message(call, f"not executed: run stopped with status {budget_status}"))
                return self._finish(budget_status, final_answer, state, messages, run_started)

            if not response.tool_calls:
                return self._finish(STATUS_COMPLETED, final_answer, state, messages, run_started)

            self._execute_tool_calls(response, state, messages)

            if state.steps >= max_steps:
                return self._finish(STATUS_MAX_STEPS, final_answer, state, messages, run_started)

    # --- internos ------------------------------------------------------------

    @staticmethod
    def _finish(
        status: str,
        final_answer: str | None,
        state: _RunState,
        messages: list[dict[str, Any]],
        run_started: float,
        error: str | None = None,
    ) -> AgentResult:
        unmeasured = dict(UNMEASURED_CORE)
        if state.cached is None:
            unmeasured["cached_input_tokens"] = "not exposed by provider for at least one call"
        if state.reasoning is None:
            unmeasured["reasoning_tokens"] = "not exposed by provider for at least one call"
        if state.failed:
            unmeasured["failed_call_usage"] = "provider error returned no usage; tokens and cost of failed call not measured"
        return AgentResult(
            status=status,
            final_answer=final_answer,
            steps=state.steps,
            total_prompt_tokens=state.prompt_tokens,
            total_completion_tokens=state.completion_tokens,
            total_tokens=state.total_tokens,
            total_cost_usd=None if state.cost_unknown else state.cost,
            chars_saved_by_masking=state.chars_saved,
            messages=messages,
            error=error,
            provider_calls_failed=state.failed,
            total_cached_input_tokens=state.cached,
            total_reasoning_tokens=state.reasoning,
            total_wall_clock_ms=(time.perf_counter() - run_started) * 1000.0,
            llm_wall_clock_ms=state.llm_ms,
            tool_wall_clock_ms=state.tool_ms,
            calls=list(state.calls),
            unmeasured=unmeasured,
        )

    def _execute_tool_calls(self, response: LLMResponse, state: _RunState, messages: list[dict[str, Any]]) -> None:
        for call in response.tool_calls:
            tool_started = time.perf_counter()
            observation = self._dispatch(call)
            state.tool_ms += (time.perf_counter() - tool_started) * 1000.0
            messages.append(_tool_message(call, observation))

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


def _error_text(exc: BaseException) -> str:
    text = str(exc)
    if len(text) > ERROR_MAX_CHARS:
        text = text[:ERROR_MAX_CHARS] + "..."
    return f"{type(exc).__name__}: {text}"


def _assistant_message(response: LLMResponse) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": response.content,
        "tool_calls": [
            {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
            for c in response.tool_calls
        ],
    }


def _tool_message(call: ToolCall, content: str) -> dict[str, Any]:
    return {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": content}


def _format_observation(result: ToolResult) -> str:
    if result.ok:
        return result.output
    head = f"error: {result.error}" if result.error else "error"
    return f"{head}\n{result.output}".rstrip() if result.output else head
