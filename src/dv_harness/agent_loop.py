"""AgentLoop: loop mínimo que compõe LLMClient, ToolExecutor e ContextManager (Módulo 4, protocolo v2).

Um passo = uma chamada lógica ao LLM (que pode ter várias tentativas, ver ``llm_client``).

Ordem por turno (protocolo v2, decisão D1):

1. ``ContextManager.prepare`` monta o payload. O log ``messages`` nunca é alterado.
2. ``llm.complete``. ``LLMCallFailed`` vira ``provider_error`` (com totais e tentativas).
3. A resposta é registrada. Então, em ordem de precedência, o orçamento é avaliado
   **antes** de qualquer ferramenta:
   a. ``max_tokens`` atingido (cota inferior medida >= limite) -> ``budget_exceeded``;
   b. ``max_cost_usd`` atingido pelo custo conhecido -> ``budget_exceeded``;
   c. limite definido e insumo desconhecido -> ``budget_unverifiable``.
4. As ferramentas do turno executam, na ordem. Depois, se houve ``submit`` válido, o
   run termina com ``completed`` e ``final_answer`` = resposta submetida.
5. Turno sem ``tool_calls`` e sem submissão é **erro de formato**, não conclusão.
6. ``error_limit`` erros de formato/ferramenta consecutivos -> ``error_limit_reached``.
7. ``max_steps`` chamadas lógicas bem-sucedidas -> ``max_steps_reached``.

Estados (camada 1, ``protocol.EXECUTOR_STATUSES``). O ``outcome`` (SOLVED, FAILURE,
INCONCLUSIVE) não é atribuído aqui. Ele vem de um verificador independente, via
``protocol.outcome_for``. ``completed`` é só a submissão do executor.

Limites: ``max_steps`` conta chamadas lógicas bem-sucedidas. ``max_tokens`` e
``max_cost_usd`` são avaliados sobre as medições conhecidas. Como a última chamada
já foi feita quando o limite é avaliado, ela pode ultrapassar o limite.

Erros de ferramenta (nome desconhecido, JSON inválido, argumentos errados, exceção
inesperada, caminho inválido) viram observação para o modelo, e contam para o teto de
erros consecutivos. ``ToolResult(ok=False)`` esperado (ex.: ``old_text`` ambíguo) é
resultado normal e não conta.

Auditoria (A6): cada ferramenta gera um ``ToolEvent`` com a saída bruta completa, seu
SHA-256 e a versão truncada que foi para o modelo. Se ``ledger`` for fornecido, todos
os eventos são gravados por linha antes de a execução prosseguir (I1, D7).

Não há roteador, memória, planejamento nem sumarização. Não é um orquestrador.

Atribuições empíricas
---------------------
- Loop mínimo com limites de passos e de custo e recuperação de erro via observação:
  ``SWE-agent/mini-swe-agent`` 2.4.6 (https://github.com/SWE-agent/mini-swe-agent),
  ``src/minisweagent/agents/default.py``. Limites verificados antes de cada chamada em
  ``default.py:132``. Teto de erros consecutivos em ``default.py:99-114`` (padrão 3, ``:32``).
  Término por submissão explícita: ``environments/local.py:47-56``.
- **Divergências de defaults em relação ao mini-swe-agent (B10):**
  - ``step_limit``: código ``0`` (sem limite, ``default.py:26``); YAML distribuído
    ``0`` (``config/default.yaml:104``). Aqui ``max_steps=30``.
  - ``cost_limit``: código ``3.0`` (``default.py:28``), mas o YAML distribuído define
    ``0.`` (``config/default.yaml:105``), ou seja, **desativado**. Aqui ``max_cost_usd=None``.
  Os valores usados em benchmark devem ser passados explicitamente e registrados no run.
- Composição sobre as primitivas ``read``, ``write``, ``edit`` e ``bash``: ``earendil-works/pi``.
- Contabilização agregada de tokens e orçamento: SWE-Effi (arXiv:2509.09853).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from dv_harness import protocol
from dv_harness.context_manager import ContextManager
from dv_harness.ledger import RunLedger
from dv_harness.llm_client import (
    PRICING_SOURCE,
    AttemptRecord,
    LLMCallFailed,
    LLMResponse,
    ToolCall,
)
from dv_harness.tool_executor import ENV_ALLOWLIST, TOOL_SCHEMAS, ToolExecutor, ToolResult

# Re-exports de estado (compatibilidade com o módulo anterior).
STATUS_COMPLETED = protocol.STATUS_COMPLETED
STATUS_MAX_STEPS = protocol.STATUS_MAX_STEPS
STATUS_BUDGET = protocol.STATUS_BUDGET
STATUS_UNVERIFIABLE = protocol.STATUS_UNVERIFIABLE
STATUS_PROVIDER_ERROR = protocol.STATUS_PROVIDER_ERROR
STATUS_ERROR_LIMIT = protocol.STATUS_ERROR_LIMIT
UNMEASURED_CORE = protocol.UNMEASURED_REASONS

DEFAULT_MAX_STEPS = 30
DEFAULT_ERROR_LIMIT = 3
ERROR_MAX_CHARS = 300
SUBMIT_TOOL_NAME = "submit"
TOOL_NAMES: tuple[str, ...] = ("read_file", "write_file", "edit_file", "run_bash")

SUBMIT_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SUBMIT_TOOL_NAME,
        "description": "Submit the final answer when the task is complete. This ends the run.",
        "parameters": {
            "type": "object",
            "properties": {"answer": {"type": "string", "description": "The final answer."}},
            "required": ["answer"],
        },
    },
}
REQUEST_TOOLS: list[dict[str, Any]] = [*TOOL_SCHEMAS, SUBMIT_SCHEMA]

NO_TOOL_CALL_MESSAGE = (
    "Format error: your response had no tool call. Use a tool to act, "
    "or call submit(answer) when the task is complete."
)


@dataclass(frozen=True)
class CallRecord:
    """Telemetria de uma chamada lógica ao LLM, com todas as tentativas. ``None`` = não medido."""

    index: int
    outcome: str  # "ok" | "provider_error"
    model: str | None
    model_returned: str | None
    provider: str | None
    api_id: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_input_tokens: int | None
    reasoning_tokens: int | None
    cost_usd: float | None
    cost_basis: str
    pricing_source: str | None
    wall_clock_ms: float | None
    attempts: tuple[AttemptRecord, ...]
    attempt_usage_complete: bool
    error: str | None = None


@dataclass(frozen=True)
class ToolEvent:
    """Uma chamada de ferramenta, com saída bruta completa para auditoria."""

    index: int  # número sequencial global do evento de ferramenta
    call_index: int  # chamada lógica ao LLM que originou esta ferramenta
    call_id: str
    name: str
    ok: bool
    format_error: bool
    observation: str  # versão truncada, enviada ao modelo
    raw_output: str  # versão completa, para auditoria
    raw_sha256: str
    truncated: bool
    wall_clock_ms: float | None  # None para submit, que não é executado por ferramenta


@dataclass
class AgentResult:
    status: str
    final_answer: str | None
    steps: int
    total_prompt_tokens: int | None
    total_completion_tokens: int | None
    total_tokens: int | None
    total_cost_usd: float | None
    cost_basis: str
    zero_priced_calls: int
    chars_saved_by_masking: int
    messages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    provider_calls_failed: int = 0
    provider_attempts: int = 0
    provider_attempts_failed: int = 0
    total_cached_input_tokens: int | None = None
    total_reasoning_tokens: int | None = None
    total_wall_clock_ms: float | None = None
    llm_wall_clock_ms: float | None = None
    tool_wall_clock_ms: float | None = None
    calls: list[CallRecord] = field(default_factory=list)
    tool_events: list[ToolEvent] = field(default_factory=list)
    partitions: dict[str, int | None] = field(default_factory=dict)
    metric: str = protocol.METRIC_NAME
    core_tokens_partial: int | None = None
    outcome: str | None = None
    run_id: str | None = None
    unmeasured: dict[str, str] = field(default_factory=dict)


@dataclass
class _RunState:
    """Estado mutável de uma execução. Criado por ``run``, nunca compartilhado."""

    messages: list[dict[str, Any]]
    calls: list[CallRecord] = field(default_factory=list)
    tool_events: list[ToolEvent] = field(default_factory=list)
    chars_saved: int = 0
    last_text: str | None = None
    consecutive_errors: int = 0
    tool_ms: float = 0.0


@dataclass(frozen=True)
class _Limits:
    max_steps: int
    max_tokens: int | None
    max_cost_usd: float | None


@dataclass
class _Dispatch:
    observation: str
    raw_output: str
    ok: bool
    truncated: bool
    format_error: bool


def _check_budgets(calls: list[CallRecord], max_tokens: int | None, max_cost_usd: float | None) -> str | None:
    """Estado de parada por orçamento, ou ``None``.

    Usa cotas inferiores: a soma das medições conhecidas. Se ela já atinge o limite, o
    estouro é certo (``budget_exceeded``). Só quando não há estouro certo e algum insumo
    é desconhecido, o orçamento é ``budget_unverifiable``.
    """
    known_tokens = [c.total_tokens for c in calls if c.total_tokens is not None]
    known_costs = [c.cost_usd for c in calls if c.cost_usd is not None]
    unknown_tokens = any(c.total_tokens is None or not c.attempt_usage_complete for c in calls)
    unknown_cost = any(c.cost_usd is None or not c.attempt_usage_complete for c in calls)

    if max_tokens is not None and sum(known_tokens) >= max_tokens:
        return protocol.STATUS_BUDGET
    if max_cost_usd is not None and sum(known_costs) >= max_cost_usd:
        return protocol.STATUS_BUDGET
    if max_cost_usd is not None and unknown_cost:
        return protocol.STATUS_UNVERIFIABLE
    if max_tokens is not None and unknown_tokens:
        return protocol.STATUS_UNVERIFIABLE
    return None


class AgentLoop:
    """Compõe ``llm`` (com ``complete``), ``executor`` (ToolExecutor) e ``context`` (ContextManager).

    Não guarda estado entre execuções. Cada ``run`` cria seu próprio ``_RunState``.
    """

    def __init__(
        self,
        llm: Any,
        executor: ToolExecutor,
        context: ContextManager,
        *,
        ledger: RunLedger | None = None,
        error_limit: int = DEFAULT_ERROR_LIMIT,
    ) -> None:
        if error_limit < 1:
            raise ValueError("error_limit must be >= 1")
        self.llm = llm
        self.executor = executor
        self.context = context
        self.ledger = ledger
        self.error_limit = error_limit

    def run(
        self,
        task: str,
        system_prompt: str,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_tokens: int | None = None,
        max_cost_usd: float | None = None,
        run_id: str | None = None,
    ) -> AgentResult:
        _validate_limits(max_steps, max_tokens, max_cost_usd)
        limits = _Limits(max_steps=max_steps, max_tokens=max_tokens, max_cost_usd=max_cost_usd)
        state = _RunState(messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ])
        run_started = time.perf_counter()
        self._log("run_started", {"run_id": run_id, "config": self._config_snapshot(limits)})

        decision: tuple[str, str | None, str | None] | None = None
        while decision is None:
            decision = self._turn(state, limits)
        status, final_answer, error = decision

        result = self._finish(status=status, final_answer=final_answer, error=error, state=state,
                              total_ms=(time.perf_counter() - run_started) * 1000.0, run_id=run_id)
        self._log("run_finished", {"status": status, "steps": result.steps, "run_id": run_id})
        return result

    # --- um turno ------------------------------------------------------------

    def _turn(self, state: _RunState, limits: _Limits) -> tuple[str, str | None, str | None] | None:
        """Executa um turno. Devolve ``(status, final_answer, error)`` ao terminar, ou ``None``."""
        prepared = self.context.prepare(state.messages)
        state.chars_saved += prepared.stats.chars_saved
        index = len(state.calls) + 1
        self._log("call_started", {"call_index": index})

        try:
            response: LLMResponse = self.llm.complete(prepared.messages, tools=REQUEST_TOOLS)
        except LLMCallFailed as exc:
            self._record_call(state, _failed_call_record(index, self.llm, exc))
            return protocol.STATUS_PROVIDER_ERROR, state.last_text, str(exc)

        self._record_call(state, _ok_call_record(index, response))
        if response.content:
            state.last_text = response.content
        state.messages.append(_assistant_message(response))

        # Orçamento antes de qualquer ferramenta do turno (D1).
        budget = _check_budgets(state.calls, limits.max_tokens, limits.max_cost_usd)
        if budget is not None:
            for call in response.tool_calls:
                state.messages.append(_tool_message(call, f"not executed: run stopped with status {budget}"))
            return budget, state.last_text, None

        submitted, turn_format_error = self._run_tools(state, index, response)
        if not response.tool_calls:
            state.messages.append({"role": "user", "content": NO_TOOL_CALL_MESSAGE})
            turn_format_error = True
        state.consecutive_errors = state.consecutive_errors + 1 if turn_format_error else 0

        if submitted is not None:
            return protocol.STATUS_COMPLETED, submitted, None
        if state.consecutive_errors >= self.error_limit:
            return protocol.STATUS_ERROR_LIMIT, state.last_text, None
        if _steps(state.calls) >= limits.max_steps:
            return protocol.STATUS_MAX_STEPS, state.last_text, None
        return None

    def _run_tools(self, state: _RunState, index: int, response: LLMResponse) -> tuple[str | None, bool]:
        """Executa as ferramentas do turno: primeiro as normais, na ordem, depois ``submit``.

        Devolve ``(resposta_submetida_ou_None, houve_erro_de_formato)``. Anexa as mensagens
        ``tool`` na ordem original das chamadas, para o protocolo de tool calling.
        """
        observations: dict[str, str] = {}
        turn_format_error = False
        start = len(state.tool_events)

        for call in response.tool_calls:
            if call.name == SUBMIT_TOOL_NAME:
                continue
            dispatch, event, elapsed_ms = self._execute(call, index, len(state.tool_events) + 1)
            state.tool_ms += elapsed_ms
            state.tool_events.append(event)
            observations[call.id] = dispatch.observation
            turn_format_error = turn_format_error or dispatch.format_error

        submitted: str | None = None
        for call in response.tool_calls:
            if call.name != SUBMIT_TOOL_NAME:
                continue
            observation, answer, format_error = self._handle_submit(call, already_accepted=submitted is not None)
            if answer is not None:
                submitted = answer
            turn_format_error = turn_format_error or format_error
            observations[call.id] = observation
            state.tool_events.append(_submit_event(index, len(state.tool_events) + 1, call, observation,
                                                   format_error=format_error, ok=answer is not None))

        for event in state.tool_events[start:]:
            self._log("tool_finished", {"call_index": index, "tool_event": dataclasses.asdict(event)})
        for call in response.tool_calls:
            state.messages.append(_tool_message(call, observations[call.id]))
        return submitted, turn_format_error

    def _handle_submit(self, call: ToolCall, *, already_accepted: bool) -> tuple[str, str | None, bool]:
        """Valida um ``submit``. Devolve ``(observação, resposta_aceita_ou_None, erro_de_formato)``."""
        if already_accepted:
            return "not executed: a submission was already accepted this turn", None, False
        answer, problem = _submit_answer(call)
        if problem is not None:
            return problem, None, True
        return "submitted", answer, False

    def _execute(self, call: ToolCall, call_index: int, event_no: int) -> tuple[_Dispatch, ToolEvent, float]:
        started = time.perf_counter()
        dispatch = self._dispatch(call)
        elapsed = (time.perf_counter() - started) * 1000.0
        event = ToolEvent(
            index=event_no,
            call_index=call_index,
            call_id=call.id,
            name=call.name,
            ok=dispatch.ok,
            format_error=dispatch.format_error,
            observation=dispatch.observation,
            raw_output=dispatch.raw_output,
            raw_sha256=hashlib.sha256(dispatch.raw_output.encode("utf-8")).hexdigest(),
            truncated=dispatch.truncated,
            wall_clock_ms=elapsed,
        )
        return dispatch, event, elapsed

    def _dispatch(self, call: ToolCall) -> _Dispatch:
        """Executa uma ferramenta. Nunca levanta exceção."""
        if call.name not in TOOL_NAMES:
            text = f"error: unknown tool '{call.name}'. Available tools: {', '.join(TOOL_NAMES)}"
            return _Dispatch(text, text, ok=False, truncated=False, format_error=True)
        try:
            args = call.parsed_arguments
        except (ValueError, json.JSONDecodeError) as exc:
            text = f"error: invalid JSON arguments for {call.name}: {exc}"
            return _Dispatch(text, text, ok=False, truncated=False, format_error=True)
        try:
            method = getattr(self.executor, call.name)
            result: ToolResult = method(**args)
        except TypeError as exc:
            text = f"error: invalid arguments for {call.name}: {exc}"
            return _Dispatch(text, text, ok=False, truncated=False, format_error=True)
        except Exception as exc:  # noqa: BLE001 - erro de ferramenta vira observação, não derruba o loop
            text = f"error: {call.name} failed: {type(exc).__name__}: {exc}"
            return _Dispatch(text, text, ok=False, truncated=False, format_error=True)
        return _Dispatch(
            observation=_format_observation(result, raw=False),
            raw_output=_format_observation(result, raw=True),
            ok=result.ok,
            truncated=result.truncated,
            format_error=False,
        )

    # --- registro, configuração e fechamento ------------------------------------

    def _record_call(self, state: _RunState, record: CallRecord) -> None:
        state.calls.append(record)
        self._log("call_finished", {"call_index": record.index, "record": dataclasses.asdict(record)})

    def _config_snapshot(self, limits: _Limits) -> dict[str, Any]:
        retry = getattr(self.llm, "retry", None)
        return {
            "max_steps": limits.max_steps,
            "max_tokens": limits.max_tokens,
            "max_cost_usd": limits.max_cost_usd,
            "error_limit": self.error_limit,
            "tool_names": list(TOOL_NAMES),
            "submit_tool": SUBMIT_TOOL_NAME,
            "retry_policy": retry.describe() if retry is not None else None,
            "default_model": getattr(self.llm, "default_model", None),
            "keep_recent_observations": self.context.keep_recent_observations,
            "placeholder": self.context.placeholder,
            "max_output_chars": self.executor.max_output_chars,
            "executor_root": str(self.executor.root),
            "env_allowlist": list(ENV_ALLOWLIST),
            "pricing_source": PRICING_SOURCE,
            "contract": protocol.CONTRACT_ID,
        }

    def _log(self, event: str, payload: dict[str, Any]) -> None:
        if self.ledger is not None:
            self.ledger.append(event, payload)

    def _finish(self, *, status: str, final_answer: str | None, error: str | None, state: _RunState,
                total_ms: float, run_id: str | None) -> AgentResult:
        calls = state.calls
        total_tokens = _total(calls, lambda c: c.total_tokens)
        total_cost = _total(calls, lambda c: c.cost_usd)
        partitions: dict[str, int | None] = {name: None for name in protocol.PARTITIONS}
        partitions["execution"] = total_tokens
        return AgentResult(
            status=status,
            final_answer=final_answer,
            steps=_steps(calls),
            total_prompt_tokens=_total(calls, lambda c: c.prompt_tokens),
            total_completion_tokens=_total(calls, lambda c: c.completion_tokens),
            total_tokens=total_tokens,
            total_cost_usd=total_cost,
            cost_basis=protocol.run_cost_basis([
                c.cost_basis if c.attempt_usage_complete else protocol.COST_BASIS_UNKNOWN for c in calls
            ]),
            zero_priced_calls=sum(1 for c in calls if c.cost_basis == protocol.COST_BASIS_ZERO),
            chars_saved_by_masking=state.chars_saved,
            messages=state.messages,
            error=error,
            provider_calls_failed=sum(1 for c in calls if c.outcome == "provider_error"),
            provider_attempts=sum(len(c.attempts) for c in calls),
            provider_attempts_failed=_failed_attempts(calls),
            total_cached_input_tokens=_total(calls, lambda c: c.cached_input_tokens),
            total_reasoning_tokens=_total(calls, lambda c: c.reasoning_tokens),
            total_wall_clock_ms=total_ms,
            llm_wall_clock_ms=sum(c.wall_clock_ms for c in calls if c.wall_clock_ms is not None),
            tool_wall_clock_ms=state.tool_ms,
            calls=list(calls),
            tool_events=list(state.tool_events),
            partitions=partitions,
            core_tokens_partial=total_tokens,
            outcome=None,
            run_id=run_id,
            unmeasured=_unmeasured_for(calls, total_tokens, run_id),
        )


# --- funções auxiliares -----------------------------------------------------


def _validate_limits(max_steps: int, max_tokens: int | None, max_cost_usd: float | None) -> None:
    if max_steps < 1:
        raise ValueError("max_steps must be >= 1")
    if max_tokens is not None and max_tokens < 1:
        raise ValueError("max_tokens must be >= 1 when set")
    if max_cost_usd is not None and max_cost_usd <= 0:
        raise ValueError("max_cost_usd must be > 0 when set")


def _unmeasured_for(calls: list[CallRecord], total_tokens: int | None, run_id: str | None) -> dict[str, str]:
    unmeasured = dict(protocol.UNMEASURED_REASONS)
    if run_id is None:
        unmeasured["run_identity"] = protocol.UNMEASURED_REASONS["run_identity"]
    else:
        unmeasured.pop("run_identity", None)  # medido pelo chamador: não é "não medido"
    incomplete = "incomplete: at least one call has unknown usage (failed attempt or provider error)"
    empty = "no calls were made"
    for key, value in (
        ("total_tokens", total_tokens),
        ("total_prompt_tokens", _total(calls, lambda c: c.prompt_tokens)),
        ("total_completion_tokens", _total(calls, lambda c: c.completion_tokens)),
        ("total_cost_usd", _total(calls, lambda c: c.cost_usd)),
        ("total_cached_input_tokens", _total(calls, lambda c: c.cached_input_tokens)),
        ("total_reasoning_tokens", _total(calls, lambda c: c.reasoning_tokens)),
        ("execution", total_tokens),
    ):
        if value is None:
            unmeasured[key] = incomplete if calls else empty
    failed = _failed_attempts(calls)
    if failed:
        unmeasured["failed_attempt_usage"] = (
            f"{failed} failed attempt(s) returned no usage; their tokens and cost are not measured"
        )
    return unmeasured


def _steps(calls: list[CallRecord]) -> int:
    return sum(1 for c in calls if c.outcome == "ok")


def _failed_attempts(calls: list[CallRecord]) -> int:
    return sum(1 for c in calls for a in c.attempts if a.outcome == "error")


def _total(calls: list[CallRecord], getter) -> int | float | None:
    """Soma só se todas as chamadas forem medidas. Qualquer insumo ausente -> ``None`` (D5)."""
    if not calls:
        return None
    values = []
    for c in calls:
        v = getter(c)
        if v is None or not c.attempt_usage_complete:
            return None
        values.append(v)
    return sum(values)


def _ok_call_record(index: int, response: LLMResponse) -> CallRecord:
    return CallRecord(
        index=index,
        outcome="ok",
        model=response.model,
        model_returned=response.model_returned,
        provider=response.provider,
        api_id=response.api_id,
        prompt_tokens=response.prompt_tokens,
        completion_tokens=response.completion_tokens,
        total_tokens=response.total_tokens,
        cached_input_tokens=response.cached_input_tokens,
        reasoning_tokens=response.reasoning_tokens,
        cost_usd=response.cost_usd,
        cost_basis=response.cost_basis,
        pricing_source=response.pricing_source,
        wall_clock_ms=response.wall_clock_ms,
        attempts=response.attempts,
        attempt_usage_complete=not any(a.outcome == "error" for a in response.attempts),
    )


def _failed_call_record(index: int, llm: Any, exc: LLMCallFailed) -> CallRecord:
    return CallRecord(
        index=index,
        outcome="provider_error",
        model=getattr(llm, "default_model", None),
        model_returned=None,
        provider=None,
        api_id=None,
        prompt_tokens=None,
        completion_tokens=None,
        total_tokens=None,
        cached_input_tokens=None,
        reasoning_tokens=None,
        cost_usd=None,
        cost_basis=protocol.COST_BASIS_UNKNOWN,
        pricing_source=None,
        wall_clock_ms=exc.wall_clock_ms,
        attempts=exc.attempts,
        attempt_usage_complete=False,
        error=str(exc),
    )


def _submit_answer(call: ToolCall) -> tuple[str | None, str | None]:
    try:
        args = call.parsed_arguments
    except (ValueError, json.JSONDecodeError) as exc:
        return None, f"error: invalid JSON arguments for submit: {exc}"
    answer = args.get("answer")
    if not isinstance(answer, str):
        return None, "error: submit requires a string argument 'answer'"
    return answer, None


def _submit_event(call_index: int, event_no: int, call: ToolCall, observation: str, *,
                  format_error: bool, ok: bool) -> ToolEvent:
    """Evento de ``submit``, que não passa pelo executor. Sem tempo de execução de ferramenta."""
    return ToolEvent(
        index=event_no,
        call_index=call_index,
        call_id=call.id,
        name=call.name,
        ok=ok,
        format_error=format_error,
        observation=observation,
        raw_output=observation,
        raw_sha256=hashlib.sha256(observation.encode("utf-8")).hexdigest(),
        truncated=False,
        wall_clock_ms=None,
    )


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


def _format_observation(result: ToolResult, *, raw: bool) -> str:
    body = result.raw_output if raw else result.output
    if result.ok:
        return body
    head = f"error: {result.error}" if result.error else "error"
    return f"{head}\n{body}".rstrip() if body else head
