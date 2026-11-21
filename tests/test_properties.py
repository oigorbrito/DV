"""Testes de propriedade (hypothesis) para as invariantes I1 a I6 do protocolo v2.

Cada exemplo gerado roda o sistema real (LLM roteirizado, ToolExecutor real, ContextManager real,
ledger real). Os diretórios temporários são criados dentro de cada exemplo.

Executar: python -m pytest tests/test_properties.py -v
"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from support import ScriptedLLM, call, make_response, provider_failure, submit, tool_contents  # noqa: F401
from dv_harness import protocol as p
from dv_harness.agent_loop import AgentLoop
from dv_harness.context_manager import DEFAULT_PLACEHOLDER, ContextManager
from dv_harness.ledger import RunLedger, reconcile
from dv_harness.llm_client import AttemptRecord
from dv_harness.tool_executor import ToolExecutor, ToolResult

PROP = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
SMALL = settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])

_usage = st.one_of(st.none(), st.integers(min_value=0, max_value=500))
_cost = st.one_of(st.none(), st.floats(min_value=0.0, max_value=3.0, allow_nan=False, allow_infinity=False))


def _error_attempts(n: int) -> tuple[AttemptRecord, ...]:
    return tuple(
        AttemptRecord(index=i + 1, outcome="error", wall_clock_ms=1.0, error_type="RuntimeError", retryable=True,
                      backoff_s=1.0)
        for i in range(n)
    )


# --- I1: conservação da contabilidade, inclusive em falha de provedor -----------------


@PROP
@given(
    steps=st.lists(
        st.tuples(_usage, _usage, _cost, st.integers(min_value=0, max_value=2)),
        min_size=1, max_size=5,
    ),
    fail_after=st.one_of(st.none(), st.integers(min_value=0, max_value=5)),
    fail_attempts=st.integers(min_value=1, max_value=3),
)
def test_I1_conservation_of_accounting(steps, fail_after, fail_attempts):
    script = []
    for prompt, completion, cost, n_err in steps:
        attempts = _error_attempts(n_err) + (AttemptRecord(index=n_err + 1, outcome="ok", wall_clock_ms=1.0),)
        script.append(make_response(calls=[call(f"c{len(script)}", "run_bash", {"command": "echo x"})],
                                    prompt=prompt, completion=completion, cost=cost, attempts=attempts))
    if fail_after is not None and fail_after < len(script):
        script.insert(fail_after, provider_failure("boom", attempts=fail_attempts))
    else:
        script.append(make_response(calls=[submit("end", "done")]))

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp) / "ws"
        ws.mkdir()
        ledger_path = Path(tmp) / "ledger.jsonl"
        with RunLedger(ledger_path) as ledger:
            loop = AgentLoop(ScriptedLLM(script), ToolExecutor(ws), ContextManager(10), ledger=ledger)
            result = loop.run("t", "s", max_steps=50)
        summary = reconcile(ledger_path)

    # (a) toda chamada lógica é registrada uma vez, como ok ou provider_error
    assert len(result.calls) == result.steps + result.provider_calls_failed
    # (c/d) índices contíguos
    assert [c.index for c in result.calls] == list(range(1, len(result.calls) + 1))
    # (b) totais: soma só se todas as chamadas tiverem uso completo; senão None
    def expected(getter):
        values = []
        for c in result.calls:
            v = getter(c)
            if v is None or not c.attempt_usage_complete:
                return None
            values.append(v)
        return sum(values)

    assert result.total_prompt_tokens == expected(lambda c: c.prompt_tokens)
    assert result.total_completion_tokens == expected(lambda c: c.completion_tokens)
    assert result.total_tokens == expected(lambda c: c.total_tokens)
    assert (result.total_cost_usd is None) == (expected(lambda c: c.cost_usd) is None)
    # (d) ledger concilia: nenhuma chamada em voo, e o número de registros finalizados bate
    assert summary["in_flight_call_indices"] == []
    assert summary["calls_finished"] == len(result.calls)


# --- I2: imutabilidade do log bruto frente ao masking ---------------------------------

_BODY = st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=0, max_size=1500)


@PROP
@given(bodies=st.lists(_BODY, min_size=1, max_size=5), keep=st.integers(min_value=0, max_value=3))
def test_I2_log_is_raw_and_masking_only_affects_payload(bodies, keep):
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        seen_payloads: list[list[dict]] = []

        def make_step(i, body):
            def step(messages):
                seen_payloads.append([dict(m) for m in messages])
                # (d) o LLM tenta corromper o payload. O log não pode mudar.
                for m in messages:
                    if m.get("role") == "tool":
                        m["content"] = "TAMPERED"
                return make_response(calls=[call(f"w{i}", "write_file", {"path": f"f{i}.txt", "content": body})])
            return step

        script = [make_step(i, b) for i, b in enumerate(bodies)]
        script.append(make_response(calls=[submit("end", "done")]))
        llm = ScriptedLLM(script)
        loop = AgentLoop(llm, ToolExecutor(ws), ContextManager(keep_recent_observations=keep))
        result = loop.run("t", "s", max_steps=50)

    # (a/b) cada conteúdo de ferramenta no log é exatamente a observação registrada e a saída bruta
    by_id = {e.call_id: e for e in result.tool_events}
    for m in result.messages:
        if m["role"] == "tool":
            assert m["content"] != "TAMPERED"
            assert m["content"] == by_id[m["tool_call_id"]].observation
    for e in result.tool_events:
        assert e.raw_sha256 == hashlib.sha256(e.raw_output.encode("utf-8")).hexdigest()
    # (c) o payload enviado difere do log só no conteúdo de observações mascaradas
    for payload in seen_payloads:
        _assert_payload_is_masked_view_of_log(payload, result.messages)


def _assert_payload_is_masked_view_of_log(payload: list[dict], log: list[dict]) -> None:
    for pm, lm in zip(payload, log[: len(payload)], strict=True):
        assert pm["role"] == lm["role"]
        assert pm.get("tool_call_id") == lm.get("tool_call_id")
        if pm.get("content") != lm.get("content"):
            assert pm["role"] == "tool"
            assert pm["content"] == DEFAULT_PLACEHOLDER  # só observações mascaradas diferem


# --- I3: confinamento do ToolExecutor -----------------------------------------------

_PATHS = st.sampled_from([
    "../outside/secret.txt", "link_out/secret.txt", "link_out", "../", "/", "", "a\x00b",
    "sub/../../outside/x.txt", "inside.txt", "sub/new.txt", "./inside.txt", "a" * 3000, "\n",
    "~", "é/../..", "loop_link", "loop_link/x",
])
_OPS = st.sampled_from(["read", "write", "edit", "cwd"])


@SMALL
@given(path=_PATHS, op=_OPS, content=st.text(max_size=40))
def test_I3_no_exception_no_escape_no_outside_effect(path, op, content):
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        ws = base / "ws"
        outside = base / "outside"
        ws.mkdir()
        outside.mkdir()
        (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
        (ws / "inside.txt").write_text("in\n", encoding="utf-8")
        (ws / "link_out").symlink_to(outside, target_is_directory=True)
        (ws / "loop_link").symlink_to(ws / "loop_link")
        before = sorted((str(q.relative_to(base)), q.read_bytes()) for q in base.rglob("*") if q.is_file())

        ex = ToolExecutor(ws)
        if op == "read":
            r = ex.read_file(path)
        elif op == "write":
            r = ex.write_file(path, content)
        elif op == "edit":
            r = ex.edit_file(path, "in", "out")
        else:
            r = ex.run_bash("true", cwd=path)

        after = sorted((str(q.relative_to(base)), q.read_bytes()) for q in base.rglob("*") if q.is_file())
        outside_before = [x for x in before if x[0].startswith("outside")]
        outside_after = [x for x in after if x[0].startswith("outside")]

        assert outside_before == outside_after, "file outside the workspace changed"
        assert isinstance(r, ToolResult)  # nenhuma exceção escapou
        if r.ok and op in ("write", "edit") and path not in ("", "/", "../", "\n", "~"):
            target = (ws / path).resolve()
            assert target.is_relative_to(ws.resolve())


# --- I4: totalidade dos estados --------------------------------------------------------

_TURN = st.sampled_from(["text", "submit", "read", "unknown", "bad_json", "fail", "cost_breach", "empty_text"])


@PROP
@given(
    turns=st.lists(_TURN, min_size=1, max_size=8),
    max_steps=st.integers(min_value=1, max_value=6),
    error_limit=st.integers(min_value=1, max_value=3),
    max_cost=st.one_of(st.none(), st.floats(min_value=0.05, max_value=2.0, allow_nan=False, allow_infinity=False)),
)
def test_I4_every_run_ends_in_exactly_one_known_state(turns, max_steps, error_limit, max_cost):
    script = []
    for i, kind in enumerate(turns):
        cid = f"t{i}"
        if kind == "text":
            script.append(make_response(content="thinking", cost=0.1))
        elif kind == "empty_text":
            script.append(make_response(content="", cost=0.1))
        elif kind == "submit":
            script.append(make_response(calls=[submit(cid, f"answer {i}")], cost=0.1))
        elif kind == "read":
            script.append(make_response(calls=[call(cid, "read_file", {"path": "missing.txt"})], cost=0.1))
        elif kind == "unknown":
            script.append(make_response(calls=[call(cid, "no_such_tool", {})], cost=0.1))
        elif kind == "bad_json":
            script.append(make_response(calls=[call(cid, "read_file", "{oops")], cost=0.1))
        elif kind == "fail":
            script.append(provider_failure("down", attempts=2))
        else:
            script.append(make_response(content="expensive", cost=5.0))

    with tempfile.TemporaryDirectory() as tmp:
        loop = AgentLoop(ScriptedLLM(script), ToolExecutor(Path(tmp)), ContextManager(10), error_limit=error_limit)
        result = loop.run("t", "s", max_steps=max_steps, max_cost_usd=max_cost)

    assert result.status in p.EXECUTOR_STATUSES
    assert result.final_answer != ""
    assert (result.error is not None) == (result.status == p.STATUS_PROVIDER_ERROR)
    if result.status == p.STATUS_COMPLETED:
        assert result.final_answer is not None
    assert result.steps <= max_steps or result.status == p.STATUS_COMPLETED
    assert result.outcome is None


# --- I5: nenhum efeito colateral após quebra de orçamento --------------------------------


@SMALL
@given(prefix=st.integers(min_value=0, max_value=2), budget=st.integers(min_value=1, max_value=50))
def test_I5_no_tool_executed_on_budget_breach_turn(prefix, budget):
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        executor = ToolExecutor(ws)
        counts = {"n": 0}
        for name in ("read_file", "write_file", "edit_file", "run_bash"):
            original = getattr(executor, name)

            def counted(*a, _orig=original, **k):
                counts["n"] += 1
                return _orig(*a, **k)

            setattr(executor, name, counted)

        script = [make_response(calls=[call(f"p{i}", "read_file", {"path": "x"})], prompt=0, completion=0, cost=0.0)
                  for i in range(prefix)]
        script.append(make_response(calls=[call("w", "write_file", {"path": "breach.txt", "content": "x"})],
                                    prompt=budget, completion=0, cost=0.0))
        script.append(make_response(calls=[submit("s", "never")]))
        result = AgentLoop(ScriptedLLM(script), executor, ContextManager(10)).run("t", "s", max_tokens=budget,
                                                                                  max_steps=20)
        breach_happened = result.status == p.STATUS_BUDGET
        assert breach_happened
        assert not (ws / "breach.txt").exists()
        # Exatamente as ferramentas executadas antes do turno de estouro
        assert counts["n"] == prefix


# --- I6: nenhum sentinela; ausência vira None com motivo ------------------------------


@PROP
@given(prompt=_usage, completion=_usage, cost=_cost)
def test_I6_absent_values_are_none_with_reason(prompt, completion, cost):
    with tempfile.TemporaryDirectory() as tmp:
        llm = ScriptedLLM([make_response(calls=[submit("s", "x")], prompt=prompt, completion=completion, cost=cost)])
        result = AgentLoop(llm, ToolExecutor(Path(tmp)), ContextManager(10)).run("t", "s")
    total_known = prompt is not None and completion is not None
    if total_known:
        assert result.total_tokens == prompt + completion
    else:
        assert result.total_tokens is None
        assert "total_tokens" in result.unmeasured
    assert result.total_tokens != 0 or (prompt == 0 and completion == 0)
    assert result.partitions["execution"] == result.total_tokens
    for name, value in result.partitions.items():
        if value is None:
            assert name in result.unmeasured
    if cost is None:
        assert result.total_cost_usd is None
        assert result.cost_basis == "unknown"
    else:
        assert result.total_cost_usd == cost


# --- I1b: cota inferior de tokens e contagem de tentativas falhas --------------------------


@PROP
@given(
    steps=st.lists(
        st.tuples(_usage, _usage, st.integers(min_value=0, max_value=3)),
        min_size=1, max_size=5,
    ),
    fail_after=st.one_of(st.none(), st.integers(min_value=0, max_value=5)),
)
def test_I1b_lower_bound_and_failed_attempts_match_independent_recount(steps, fail_after):
    script = []
    for prompt, completion, n_err in steps:
        attempts = _error_attempts(n_err) + (AttemptRecord(index=n_err + 1, outcome="ok", wall_clock_ms=1.0),)
        script.append(make_response(calls=[call(f"c{len(script)}", "run_bash", {"command": "echo x"})],
                                    prompt=prompt, completion=completion, cost=0.0, attempts=attempts))
    if fail_after is not None and fail_after < len(script):
        script.insert(fail_after, provider_failure("boom", attempts=2))
    else:
        script.append(make_response(calls=[submit("end", "done")]))
    with tempfile.TemporaryDirectory() as tmp:
        result = AgentLoop(ScriptedLLM(script), ToolExecutor(Path(tmp)), ContextManager(10)).run(
            "t", "s", max_steps=50)

    # Recontagem independente a partir das chamadas registradas
    known = [c.total_tokens for c in result.calls if c.total_tokens is not None]
    expected_bound = sum(known) if known else None
    assert result.tokens_lower_bound == expected_bound
    # Cota inferior nunca passa do total medido quando ele existe
    if result.total_tokens is not None:
        assert result.tokens_lower_bound == result.total_tokens
    # Tentativas falhas: recontagem direta das tentativas
    assert result.failed_attempt_count == sum(1 for c in result.calls for a in c.attempts if a.outcome == "error")
    # Quando há tentativa falha sem uso, o total não pode ser medido
    if result.failed_attempt_count or result.provider_calls_failed:
        assert result.total_tokens is None


# --- ajuste 1 (propriedade): só PROTOCOL_ERROR consecutivo atinge error_limit ---------


def _has_protocol_run(kinds: list[str], length: int) -> bool:
    run = 0
    for kind in kinds:
        run = run + 1 if kind == "P" else 0
        if run >= length:
            return True
    return False


@PROP
@given(
    kinds=st.lists(st.sampled_from(["P", "D", "O"]), min_size=1, max_size=8),
    limit=st.integers(min_value=1, max_value=4),
)
def test_only_consecutive_protocol_turns_reach_error_limit(kinds, limit):
    # P = ferramenta inexistente (PROTOCOL_ERROR); D = read_file ausente (DOMAIN_FAILURE); O = ok.
    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "a.txt").write_text("x\n", encoding="utf-8", newline="\n")
        script = []
        for i, kind in enumerate(kinds):
            if kind == "P":
                c = call(f"c{i}", "no_such_tool", {})
            elif kind == "D":
                c = call(f"c{i}", "read_file", {"path": "missing.txt"})
            else:
                c = call(f"c{i}", "read_file", {"path": "a.txt"})
            script.append(make_response(calls=[c]))
        script.append(make_response(calls=[submit("end", "done")]))
        loop = AgentLoop(ScriptedLLM(script), ToolExecutor(ws), ContextManager(10), error_limit=limit)
        result = loop.run("t", "s", max_steps=100)

    if _has_protocol_run(kinds, limit):
        assert result.status == p.STATUS_ERROR_LIMIT
    else:
        assert result.status == p.STATUS_COMPLETED
        assert result.protocol_errors == kinds.count("P")
        assert result.domain_failures == kinds.count("D")


# --- ajuste 2 (propriedade): baldes de tokens são soma só se todos medem ---------------


@PROP
@given(
    turns=st.lists(
        st.tuples(
            st.one_of(st.none(), st.integers(min_value=0, max_value=300)),  # cache_write
            st.one_of(st.none(), st.integers(min_value=0, max_value=300)),  # cache_read
            _usage,  # prompt
            st.integers(min_value=0, max_value=300),  # completion
        ),
        min_size=1, max_size=4,
    ),
)
def test_bucket_totals_are_sum_or_none_and_never_change_total_tokens(turns):
    import dataclasses

    script = []
    for i, (write, read, prompt, completion) in enumerate(turns):
        base = make_response(calls=[call(f"c{i}", "read_file", {"path": "missing.txt"})],
                             prompt=prompt, completion=completion)
        script.append(dataclasses.replace(base, cache_write_tokens=write, cache_read_tokens=read,
                                          cached_input_tokens=read))
    script.append(make_response(calls=[submit("end", "done")], prompt=1, completion=1))

    with tempfile.TemporaryDirectory() as tmp:
        loop = AgentLoop(ScriptedLLM(script), ToolExecutor(Path(tmp)), ContextManager(10))
        result = loop.run("t", "s", max_steps=100)

    writes = [w for w, _, _, _ in turns] + [None]  # a última chamada (submit) não expõe write
    reads = [r for _, r, _, _ in turns] + [None]
    if any(w is None for w in writes):
        assert result.total_cache_write_tokens is None
    if any(r is None for r in reads):
        assert result.total_cached_input_tokens is None
    # total_tokens segue só prompt + completion; baldes não entram nele (sem dupla contagem).
    if any(pr is None for _, _, pr, _ in turns):
        expected_total = None  # prompt não medido em uma chamada: total não medido (D5)
    else:
        expected_total = sum(pr + co for _, _, pr, co in turns) + 2
    assert result.total_tokens == expected_total
