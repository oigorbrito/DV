"""Testes do Módulo 4 (AgentLoop), protocolo v2.

Integração: LLM roteirizado, ToolExecutor e ContextManager reais em tmp_path, ledger real.
Executar: python -m pytest tests/test_agent_loop.py -v
"""

from __future__ import annotations

import json

import pytest

from support import ScriptedLLM, call, make_response, provider_failure, submit, tool_contents  # noqa: F401
from dv_harness import protocol as p
from dv_harness.agent_loop import NO_TOOL_CALL_MESSAGE, AgentLoop
from dv_harness.context_manager import DEFAULT_PLACEHOLDER, ContextManager
from dv_harness.ledger import RunLedger, reconcile, read_events
from dv_harness.llm_client import AttemptRecord
from dv_harness.tool_executor import ToolExecutor


def make_loop(tmp_path, llm, *, keep_recent=10, error_limit=3, ledger=None):
    return AgentLoop(llm, ToolExecutor(tmp_path), ContextManager(keep_recent_observations=keep_recent),
                     error_limit=error_limit, ledger=ledger)


# --- conclusão por submissão explícita ----------------------------------------------


def test_explicit_submit_completes_with_answer(tmp_path):
    llm = ScriptedLLM([make_response(calls=[submit("s1", "Paris")])])
    result = make_loop(tmp_path, llm).run("Capital?", "sys")
    assert result.status == p.STATUS_COMPLETED
    assert result.final_answer == "Paris"
    assert result.steps == 1
    assert result.outcome is None  # outcome é atribuído pelo avaliador, não pelo núcleo
    assert [e.observation for e in result.tool_events] == ["submitted"]


def test_text_only_reply_is_a_format_error_not_completion(tmp_path):
    llm = ScriptedLLM([
        make_response(content="Paris"),
        make_response(calls=[submit("s1", "Paris")]),
    ])
    result = make_loop(tmp_path, llm).run("Capital?", "sys")
    assert result.status == p.STATUS_COMPLETED
    assert result.steps == 2
    assert any(m == {"role": "user", "content": NO_TOOL_CALL_MESSAGE} for m in result.messages)


def test_repeated_text_only_replies_hit_error_limit(tmp_path):
    llm = ScriptedLLM([make_response(content="still thinking")])
    result = make_loop(tmp_path, llm, error_limit=3).run("t", "s")
    assert result.status == p.STATUS_ERROR_LIMIT
    assert result.steps == 3
    assert result.final_answer == "still thinking"  # D3: último texto não vazio


def test_invalid_submit_arguments_count_as_format_error(tmp_path):
    llm = ScriptedLLM([
        make_response(calls=[call("s1", "submit", {"answer": 42})]),
        make_response(calls=[call("s2", "submit", "not-json")]),
        make_response(calls=[call("s3", "submit", {})]),
    ])
    result = make_loop(tmp_path, llm, error_limit=3).run("t", "s")
    assert result.status == p.STATUS_ERROR_LIMIT
    assert all(e.format_error for e in result.tool_events)


def test_error_counter_resets_on_clean_turn(tmp_path):
    (tmp_path / "a.txt").write_text("hi\n", encoding="utf-8")
    llm = ScriptedLLM([
        make_response(calls=[call("b1", "nope", {})]),                       # erro 1
        make_response(calls=[call("r1", "read_file", {"path": "a.txt"})]),   # limpo: zera
        make_response(calls=[call("b2", "nope", {})]),                       # erro 1
        make_response(calls=[call("b3", "nope", {})]),                       # erro 2 -> limite 2
    ])
    result = make_loop(tmp_path, llm, error_limit=2).run("t", "s")
    assert result.status == p.STATUS_ERROR_LIMIT
    assert result.steps == 4


def test_non_format_tool_result_does_not_count_toward_error_limit(tmp_path):
    # ToolResult(ok=False) esperado (arquivo ausente) é resultado normal, não erro de formato.
    llm = ScriptedLLM([
        make_response(calls=[call("r1", "read_file", {"path": "missing.txt"})]),
        make_response(calls=[call("r2", "read_file", {"path": "missing.txt"})]),
        make_response(calls=[submit("s", "done")]),
    ])
    result = make_loop(tmp_path, llm, error_limit=2).run("t", "s")
    assert result.status == p.STATUS_COMPLETED
    assert not any(e.format_error for e in result.tool_events)


def test_submit_completes_even_if_other_calls_in_turn_failed_format(tmp_path):
    llm = ScriptedLLM([make_response(calls=[call("b", "nope", {}), submit("s", "answer")])])
    result = make_loop(tmp_path, llm, error_limit=5).run("t", "s")
    assert result.status == p.STATUS_COMPLETED
    assert result.final_answer == "answer"


def test_other_tools_in_submit_turn_run_before_submit(tmp_path):
    llm = ScriptedLLM([make_response(calls=[
        call("w", "write_file", {"path": "out.txt", "content": "done\n"}),
        submit("s", "wrote it"),
    ])])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.status == p.STATUS_COMPLETED
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "done\n"


def test_second_submit_in_same_turn_is_not_executed(tmp_path):
    llm = ScriptedLLM([make_response(calls=[submit("s1", "first"), submit("s2", "second")])])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.final_answer == "first"
    obs = {e.call_id: e.observation for e in result.tool_events}
    assert obs["s2"].startswith("not executed")


# --- D1: precedência de estados -------------------------------------------------------


def test_tokens_breach_wins_over_unknown_cost(tmp_path):
    # D1: max_tokens (cota inferior medida) antes de budget_unverifiable.
    llm = ScriptedLLM([make_response(content="x", prompt=60, completion=40, cost=None)])
    result = make_loop(tmp_path, llm).run("t", "s", max_tokens=100, max_cost_usd=1.0)
    assert result.status == p.STATUS_BUDGET
    assert result.final_answer == "x"


def test_known_cost_breach_wins_over_unknown_attempts(tmp_path):
    attempts = (
        AttemptRecord(index=1, outcome="error", wall_clock_ms=1.0, error_type="RuntimeError", retryable=True),
        AttemptRecord(index=2, outcome="ok", wall_clock_ms=1.0),
    )
    llm = ScriptedLLM([make_response(content="x", cost=2.0, attempts=attempts)])
    result = make_loop(tmp_path, llm).run("t", "s", max_cost_usd=1.0)
    assert result.status == p.STATUS_BUDGET


def test_unknown_cost_with_budget_is_unverifiable_and_keeps_text(tmp_path):
    llm = ScriptedLLM([make_response(content="maybe", cost=None)])
    result = make_loop(tmp_path, llm).run("t", "s", max_cost_usd=1.0)
    assert result.status == p.STATUS_UNVERIFIABLE
    assert result.final_answer == "maybe"
    assert result.total_cost_usd is None


def test_budget_breach_on_text_turn_is_not_completion(tmp_path):
    llm = ScriptedLLM([make_response(content="late", cost=5.0)])
    result = make_loop(tmp_path, llm).run("t", "s", max_cost_usd=1.0)
    assert result.status == p.STATUS_BUDGET
    assert result.final_answer == "late"


def test_budget_breach_on_submit_turn_does_not_complete(tmp_path):
    llm = ScriptedLLM([make_response(calls=[submit("s", "final")], cost=5.0)])
    result = make_loop(tmp_path, llm).run("t", "s", max_cost_usd=1.0)
    assert result.status == p.STATUS_BUDGET
    assert result.final_answer is None  # o turno não tinha texto, e a submissão não conta
    assert [e.observation for e in result.tool_events] == []  # submit não executado


def test_budget_breach_does_not_execute_tools(tmp_path):
    llm = ScriptedLLM([make_response(
        calls=[call("w", "write_file", {"path": "never.txt", "content": "x"})],
        prompt=60, completion=40, cost=0.01,
    )])
    result = make_loop(tmp_path, llm).run("t", "s", max_tokens=100)
    assert result.status == p.STATUS_BUDGET
    assert not (tmp_path / "never.txt").exists()
    observation = next(m["content"] for m in result.messages if m.get("tool_call_id") == "w")
    assert observation.startswith("not executed: run stopped with status budget_exceeded")


def test_exact_max_steps_counts_logical_calls(tmp_path):
    llm = ScriptedLLM([make_response(calls=[call("r", "run_bash", {"command": "echo x"})], cost=0.1)])
    result = make_loop(tmp_path, llm).run("t", "s", max_steps=3)
    assert result.status == p.STATUS_MAX_STEPS
    assert result.steps == 3
    assert len(llm.calls) == 3


def test_max_steps_final_answer_is_last_text(tmp_path):
    llm = ScriptedLLM([make_response(content="partial thought", calls=[call("r", "run_bash", {"command": "echo x"})])])
    result = make_loop(tmp_path, llm).run("t", "s", max_steps=2)
    assert result.status == p.STATUS_MAX_STEPS
    assert result.final_answer == "partial thought"


def test_final_answer_is_none_not_empty_string(tmp_path):
    result = make_loop(tmp_path, ScriptedLLM([make_response(content="", calls=[submit("s", "")])])).run("t", "s")
    assert result.status == p.STATUS_COMPLETED
    assert result.final_answer == ""  # submissão explícita de string vazia é resposta medida, não ausência
    # Texto vazio sem submissão: max_steps=1 para no primeiro turno, sem texto -> None (não "").
    result2 = make_loop(tmp_path, ScriptedLLM([make_response(content="", cost=0.0)])).run("t", "s", max_steps=1)
    assert result2.status == p.STATUS_MAX_STEPS
    assert result2.final_answer is None


def test_invalid_parameters_raise_before_any_call(tmp_path):
    llm = ScriptedLLM([make_response(calls=[submit("s", "x")])])
    loop = make_loop(tmp_path, llm)
    with pytest.raises(ValueError):
        loop.run("t", "s", max_steps=0)
    with pytest.raises(ValueError):
        loop.run("t", "s", max_tokens=0)
    with pytest.raises(ValueError):
        loop.run("t", "s", max_cost_usd=0)
    assert llm.calls == []


# --- falha de provedor e tentativas (I1) ----------------------------------------------


def test_provider_error_keeps_totals_attempts_and_messages(tmp_path):
    llm = ScriptedLLM([
        make_response(calls=[call("b1", "run_bash", {"command": "echo hi"})], prompt=30, completion=20, cost=0.2),
        provider_failure("upstream 500", attempts=3),
    ])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.status == p.STATUS_PROVIDER_ERROR
    assert result.final_answer is None  # primeiro turno não tinha texto
    assert result.steps == 1
    assert result.provider_calls_failed == 1
    assert result.provider_attempts == 1 + 3
    assert result.provider_attempts_failed == 3
    failed = result.calls[1]
    assert failed.outcome == "provider_error"
    assert len(failed.attempts) == 3
    assert failed.cost_basis == "unknown"
    assert "RuntimeError" in result.error
    # Totais incluem chamada com falha como desconhecida -> None (D5), mas o primeiro passo está no registro.
    assert result.calls[0].total_tokens == 50
    assert result.total_tokens is None
    assert result.total_cost_usd is None
    assert any(m.get("tool_call_id") == "b1" for m in result.messages)
    assert "failed_attempt_usage" in result.unmeasured


def test_provider_error_on_first_call_has_no_measured_usage(tmp_path):
    result = make_loop(tmp_path, ScriptedLLM([provider_failure("down")])).run("t", "s")
    assert result.status == p.STATUS_PROVIDER_ERROR
    assert result.steps == 0
    assert result.total_tokens is None and result.total_cost_usd is None
    assert result.total_cached_input_tokens is None and result.total_reasoning_tokens is None
    assert result.cost_basis == "unknown"
    assert len(result.messages) == 2


def test_retried_success_marks_usage_incomplete(tmp_path):
    attempts = (
        AttemptRecord(index=1, outcome="error", wall_clock_ms=3.0, error_type="RateLimitError", retryable=True, backoff_s=1.0),
        AttemptRecord(index=2, outcome="ok", wall_clock_ms=2.0),
    )
    llm = ScriptedLLM([make_response(calls=[submit("s", "ok")], prompt=5, completion=5, cost=0.1, attempts=attempts)])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.status == p.STATUS_COMPLETED
    assert result.provider_attempts == 2
    assert result.provider_attempts_failed == 1
    assert result.total_tokens is None  # tentativa com erro não tem uso conhecido (D5)
    assert "failed_attempt_usage" in result.unmeasured


# --- I6: sentinelas ---------------------------------------------------------------


def test_partitions_are_explicit_and_none_have_reasons(tmp_path):
    llm = ScriptedLLM([make_response(calls=[submit("s", "x")], prompt=3, completion=4)])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert set(result.partitions) == set(p.PARTITIONS)
    assert result.partitions["execution"] == 7
    for name, value in result.partitions.items():
        if value is None:
            assert name in result.unmeasured, name
    assert result.metric == "CORE_TOKENS_PARTIAL"
    assert result.core_tokens_partial == 7


def test_no_zero_sentinel_for_unmeasured_latency_or_usage(tmp_path):
    llm = ScriptedLLM([make_response(calls=[submit("s", "x")], prompt=None, completion=None, cost=None)])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.total_tokens is None
    assert result.total_cost_usd is None
    assert result.total_wall_clock_ms is not None and result.total_wall_clock_ms >= 0
    assert result.llm_wall_clock_ms is not None


def test_run_identity_is_declared_when_absent_and_recorded_when_given(tmp_path):
    llm = ScriptedLLM([make_response(calls=[submit("s", "x")])])
    assert "run_identity" in make_loop(tmp_path, llm).run("t", "s").unmeasured
    llm2 = ScriptedLLM([make_response(calls=[submit("s", "x")])])
    r2 = make_loop(tmp_path, llm2).run("t", "s", run_id="task1|treat|bind|shape|rep0")
    assert r2.run_id == "task1|treat|bind|shape|rep0"
    assert "run_identity" not in r2.unmeasured


def test_cost_basis_zero_priced_is_counted_separately(tmp_path):
    llm = ScriptedLLM([
        make_response(calls=[call("r", "run_bash", {"command": "echo x"})], cost=0.0),
        make_response(calls=[submit("s", "x")], cost=0.0),
    ])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.cost_basis == "priced_zero_by_table"
    assert result.zero_priced_calls == 2
    assert result.total_cost_usd == 0.0


# --- auditoria bruta (A6) ----------------------------------------------------------


def test_audit_keeps_raw_output_and_model_sees_truncated(tmp_path):
    loop = AgentLoop(
        ScriptedLLM([
            make_response(calls=[call("b", "run_bash", {"command": "python3 -c 'print(\"Q\"*5000)'"})]),
            make_response(calls=[submit("s", "done")]),
        ]),
        ToolExecutor(tmp_path, max_output_chars=400),
        ContextManager(keep_recent_observations=10),
    )
    result = loop.run("t", "s")
    event = next(e for e in result.tool_events if e.call_id == "b")
    assert event.truncated is True
    assert len(event.observation) <= 400
    assert "Q" * 5000 in event.raw_output
    import hashlib
    assert event.raw_sha256 == hashlib.sha256(event.raw_output.encode()).hexdigest()
    seen_by_model = tool_contents(result.messages)[0]
    assert seen_by_model == event.observation


def test_unknown_tool_and_invalid_json_are_format_errors(tmp_path):
    llm = ScriptedLLM([
        make_response(calls=[call("u", "delete_everything", {})]),
        make_response(calls=[call("j", "read_file", "{not json")]),
        make_response(calls=[call("a", "edit_file", {"path": "x.txt"})]),
        make_response(calls=[submit("s", "recovered")]),
    ])
    result = make_loop(tmp_path, llm, error_limit=10).run("t", "s")
    assert result.status == p.STATUS_COMPLETED
    by_id = {e.call_id: e for e in result.tool_events}
    assert by_id["u"].format_error and "unknown tool 'delete_everything'" in by_id["u"].observation
    assert by_id["j"].format_error and "invalid JSON arguments for read_file" in by_id["j"].observation
    assert by_id["a"].format_error and "invalid arguments for edit_file" in by_id["a"].observation


def test_unexpected_tool_exception_is_observation_and_format_error(tmp_path):
    llm = ScriptedLLM([
        make_response(calls=[call("b", "run_bash", {"command": "echo x", "timeout": 0})]),  # ValueError interno
        make_response(calls=[submit("s", "ok")]),
    ])
    result = make_loop(tmp_path, llm, error_limit=5).run("t", "s")
    event = next(e for e in result.tool_events if e.call_id == "b")
    assert event.format_error and "run_bash failed: ValueError" in event.observation


# --- ledger e interrupção (I1, D7) -------------------------------------------------


def test_ledger_records_full_sequence_and_reconciles(tmp_path):
    path = tmp_path / "ledger.jsonl"
    workspace = tmp_path / "ws"
    workspace.mkdir()
    llm = ScriptedLLM([
        make_response(calls=[call("r", "run_bash", {"command": "echo ok"})], prompt=2, completion=2, cost=0.1),
        make_response(calls=[submit("s", "done")], prompt=3, completion=3, cost=0.1),
    ])
    with RunLedger(path) as ledger:
        result = make_loop(workspace, llm, ledger=ledger).run("t", "s", run_id="r1")
    events = [e["event"] for e in read_events(path)]
    assert events[0] == "run_started"
    assert events[-1] == "run_finished"
    assert events.count("call_started") == 2 and events.count("call_finished") == 2
    assert events.count("tool_finished") == 2
    summary = reconcile(path)
    assert summary["in_flight_call_indices"] == []
    assert summary["finished_records"][1]["total_tokens"] == 4
    assert result.status == p.STATUS_COMPLETED


def test_ledger_config_snapshot_has_no_environment_values(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-must-not-appear-999")
    path = tmp_path / "ledger.jsonl"
    with RunLedger(path) as ledger:
        make_loop(tmp_path, ScriptedLLM([make_response(calls=[submit("s", "x")])]), ledger=ledger).run("t", "s")
    text = path.read_text(encoding="utf-8")
    assert "sk-must-not-appear-999" not in text
    first = json.loads(text.splitlines()[0])
    assert first["event"] == "run_started"
    assert "env_allowlist" in first["config"]


def test_interruption_leaves_in_flight_call_and_earlier_accounting(tmp_path):
    path = tmp_path / "ledger.jsonl"
    llm = ScriptedLLM([
        make_response(calls=[call("r", "run_bash", {"command": "echo x"})], prompt=10, completion=10, cost=0.2),
        KeyboardInterrupt(),
    ])
    with RunLedger(path) as ledger, pytest.raises(KeyboardInterrupt):
        make_loop(tmp_path, llm, ledger=ledger).run("t", "s")
    summary = reconcile(path)
    assert summary["calls_started"] == 2
    assert summary["calls_finished"] == 1
    assert summary["in_flight_call_indices"] == [2]
    assert summary["finished_records"][1]["total_tokens"] == 20
    assert summary["run_finished_events"] == 0


# --- consistência do histórico ----------------------------------------------------


def test_consecutive_runs_do_not_share_state(tmp_path):
    loop = make_loop(tmp_path, ScriptedLLM([make_response(calls=[submit("s", "a")], cost=0.1)]))
    r1 = loop.run("t1", "s")
    loop.llm.script = [make_response(calls=[submit("s2", "b")], cost=0.5)]
    loop.llm.calls = []
    r2 = loop.run("t2", "s")
    assert r1.messages is not r2.messages and r1.calls is not r2.calls
    assert r1.final_answer == "a" and r2.final_answer == "b"
    assert len(r1.calls) == 1 and len(r2.calls) == 1
    assert r2.total_cost_usd == pytest.approx(0.5)


def test_masking_does_not_alter_log_and_reports_savings(tmp_path):
    llm = ScriptedLLM([
        make_response(calls=[call("b1", "run_bash", {"command": "python3 -c 'print(\"x\" * 600)'"})]),
        make_response(calls=[call("b2", "run_bash", {"command": "echo ok"})]),
        make_response(calls=[submit("s", "done")]),
    ])
    result = make_loop(tmp_path, llm, keep_recent=0).run("t", "s")
    long_obs = tool_contents(result.messages)[0]
    assert len(long_obs) > len(DEFAULT_PLACEHOLDER)
    assert result.chars_saved_by_masking == 2 * (len(long_obs) - len(DEFAULT_PLACEHOLDER))
    assert tool_contents(llm.calls[2]["messages"])[0] == DEFAULT_PLACEHOLDER


def test_unknown_token_usage_with_token_budget_is_unverifiable(tmp_path):
    # Insumo de tokens desconhecido com max_tokens definido: orçamento não verificável, não "passou".
    llm = ScriptedLLM([make_response(content="x", prompt=None, completion=5, cost=0.1)])
    result = make_loop(tmp_path, llm).run("t", "s", max_tokens=100)
    assert result.status == p.STATUS_UNVERIFIABLE
    assert result.final_answer == "x"


def test_error_limit_must_be_positive(tmp_path):
    with pytest.raises(ValueError, match="error_limit"):
        make_loop(tmp_path, ScriptedLLM([make_response(content="x")]), error_limit=0)


def test_total_of_no_calls_is_none_not_zero():
    # Guarda defensiva: sum([]) seria o sentinela 0, proibido pelo I6.
    from dv_harness.agent_loop import _total
    assert _total([], lambda c: c.total_tokens) is None
