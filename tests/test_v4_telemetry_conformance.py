"""Conformidade de telemetria do contrato p1-run-accounting-v4 (GAP-1 a GAP-5; regras a-d). Offline, sem rede.

Cobre: digest de argumentos no ToolEvent e reconstrução do doom loop a partir do ledger; telemetry_complete
ortogonal ao oráculo; ledger_sha256 exato; dataset_sha256 nos resumos; schema dv-laddering-summary-v1.
Executar: python -m pytest tests/test_v4_telemetry_conformance.py -v
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from support import ROOT, ScriptedLLM, call, make_response, submit  # noqa: F401 (ROOT configura sys.path)
from test_batch_run import SOLVED_REF
from dv_harness import batch, protocol
from dv_harness import ledger as ledger_mod
from dv_harness.agent_loop import DoomLoopTracker, tool_arguments_sha256
from dv_harness.laddering import LADDERING_SCHEMA, LadderController, LadderPolicy, StepResult
from dv_harness.ledger import RunLedger
from dv_harness.task_integrity import dataset_fingerprint, validate_batch_integrity
from dv_harness.variants import REFERENCE_PROVENANCE

sys.path.insert(0, str(ROOT / "scripts"))
import dv_ladder_run as ladder_cli  # noqa: E402

P1 = ROOT / "experiments" / "p1"
GREET = "f1-greet"

# Mapa de fechamento dos gaps do contrato v4. Quem altera o contrato precisa atualizar este mapa.
GAP_MAP = {
    "GAP-1": "fechado: ToolEvent.arguments_sha256; ledger.tool_signatures e max_repeat_streak",
    "GAP-2": "fechado: telemetry_complete e cost_accounting_status (run, lote, degrau, laddering)",
    "GAP-3": "fechado: dataset_sha256 em batch-summary, laddering-summary e laddering-batch-summary",
    "GAP-4": "fechado: ledger_sha256 em result.json, batch-summary, degraus do laddering",
    "GAP-5": "fechado para o resumo por tarefa (dv-laddering-summary-v1); nome do lote sem fonte normativa",
    "GAP-6": "ABERTO: probe de capacidade exige chamadas reais ao provedor (proibido nesta etapa)",
    "GAP-7": "ABERTO: bindings de modelo e global_max_cost_usd dependem de decisão explícita do usuário",
    "GAP-8": "ABERTO: código de análise estatística não implementado nem congelado",
}


def _config(**over):
    base = dict(model="mock", request_timeout_s=30.0, max_steps=8, max_cost_usd=0.50, fail_fast=False,
                suite_or_manifest=batch.SUITE_SCREENING_N10, manifest_sha256="0" * 64, git_head="a" * 40,
                api_key_env="GEMINI_API_KEY")
    base.update(over)
    return batch.BatchConfig(**base)


def _task():
    return next(t for t in batch.builtin_suite() if t.task_id == GREET)


def _solve(task, *, prompt=10, completion=5, cost=0.01):
    fix = SOLVED_REF[task.task_id]["greet.py"]
    return [make_response(calls=[call("w1", "write_file", {"path": "greet.py", "content": fix})],
                          prompt=prompt, completion=completion, cost=cost),
            make_response(calls=[submit("s1", "fixed")], prompt=prompt, completion=completion, cost=cost)]


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fake_call(**over):
    base = dict(prompt_tokens=1, completion_tokens=1, total_tokens=2, cost_usd=0.1, attempt_usage_complete=True)
    base.update(over)
    return SimpleNamespace(**base)


# --- (a) digest de argumentos e reconstrução do doom loop -----------------------------------------


def test_arguments_digest_is_key_order_independent_and_content_sensitive():
    assert tool_arguments_sha256('{"path":"a","start_line":1}') == \
        tool_arguments_sha256('{"start_line":1,"path":"a"}')
    assert tool_arguments_sha256('{"path":"a"}') != tool_arguments_sha256('{"path":"b"}')
    assert tool_arguments_sha256("") == tool_arguments_sha256("{}")
    assert len(tool_arguments_sha256("not json")) == 64  # argumento inválido não levanta exceção


def test_doom_streak_reconstructed_from_ledger_matches_the_in_memory_tracker(tmp_path):
    task = _task()
    seq = [("read_file", {"path": "greet.py"})] * 3
    seq += [("read_file", {"path": "greet.py", "start_line": 1}), ("read_file", {"start_line": 1, "path": "greet.py"})]
    seq += [("read_file", {"path": "missing.txt"})]
    seq += [("read_file", {"path": "greet.py"})] * 2
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "greet.py").write_text(task.files["greet.py"], encoding="utf-8")
    script = [make_response(calls=[call(f"r{i}", name, args)]) for i, (name, args) in enumerate(seq)]
    script.append(make_response(calls=[submit("s", "done")]))
    ledger_path = tmp_path / "ledger.jsonl"
    with RunLedger(ledger_path) as ledger:
        batch.run_agent_loop(task, workspace, ScriptedLLM(script), _config(harness_variant="b0_plus", max_steps=20),
                             max_cost_usd=10.0, ledger=ledger, bash_logs=None)

    signatures = ledger_mod.tool_signatures(ledger_path)
    assert len(signatures) == len(seq) + 1  # uma entrada por ferramenta, submit incluído
    assert all(len(digest) == 64 for _, digest in signatures)

    tracker = DoomLoopTracker(threshold=2)
    peak = 0
    for name, args in seq:
        tracker.observe(name, json.dumps(args))
        peak = max(peak, tracker.streak)
    assert ledger_mod.max_repeat_streak(signatures[:len(seq)]) == peak == 3  # 3 idênticos; chave reordenada conta

    for line in ledger_path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("event") == "tool_finished":
            assert "arguments" not in event["tool_event"]  # só o digest vai para o ledger


def test_max_repeat_streak_edge_cases():
    assert ledger_mod.max_repeat_streak([]) == 0
    assert ledger_mod.max_repeat_streak([("a", "x"), ("a", "y")]) == 1
    assert ledger_mod.max_repeat_streak([("a", "x"), ("a", "x"), ("a", "x"), ("b", "x")]) == 3


# --- (b) telemetry_complete e oracle_pass são ortogonais ------------------------------------------


@pytest.mark.parametrize("calls, expected", [
    ([_fake_call()], True),
    ([_fake_call(), _fake_call(cost_usd=0.0)], True),  # zero de tabela é medido, não desconhecido
    ([], False),  # nada medido
    ([_fake_call(cost_usd=None)], False),  # preço desconhecido
    ([_fake_call(total_tokens=None)], False),
    ([_fake_call(prompt_tokens=None)], False),
    ([_fake_call(completion_tokens=None)], False),
    ([_fake_call(attempt_usage_complete=False)], False),  # tentativa sem uso medido
])
def test_telemetry_complete_rule(calls, expected):
    assert protocol.telemetry_complete(calls) is expected


def test_cost_accounting_status_maps_completeness_and_never_drops_a_run():
    assert protocol.cost_accounting_status(True) == "MEASURED"
    assert protocol.cost_accounting_status(False) == "INCONCLUSIVE"


def test_telemetry_failure_keeps_oracle_pass_true_and_marks_cost_inconclusive(tmp_path):
    task = _task()
    llm = ScriptedLLM(_solve(task, prompt=None, completion=None, cost=0.01))  # total desconhecido, custo conhecido
    report = batch.run_one(task, tmp_path / "run", llm, _config(), max_cost_usd=0.5)
    assert report["verifier_verdict"] == "PASS" and report["oracle_pass"] is True  # oráculo não é alterado
    assert report["status"] == "completed"
    assert report["telemetry_complete"] is False
    assert report["cost_accounting_status"] == "INCONCLUSIVE"


def test_fully_measured_run_is_measured_and_result_carries_ledger_hash(tmp_path):
    task = _task()
    run_dir = tmp_path / "run"
    report = batch.run_one(task, run_dir, ScriptedLLM(_solve(task)), _config(), max_cost_usd=0.5)
    assert report["telemetry_complete"] is True and report["cost_accounting_status"] == "MEASURED"
    assert report["oracle_pass"] is True and report["outcome"] == "SOLVED"
    assert report["ledger_sha256"] == _sha(run_dir / "ledger.jsonl")  # bytes exatos (GAP-4)
    assert all(len(e["arguments_sha256"]) == 64 for e in report["tool_events"])  # GAP-1 no result.json


def test_harness_error_report_is_inconclusive_and_has_the_required_fields(tmp_path):
    task = _task()
    report = batch.harness_error_report(task, tmp_path / "run", RuntimeError("boom"), _config(), 0.5)
    assert report["telemetry_complete"] is False and report["cost_accounting_status"] == "INCONCLUSIVE"
    assert report["oracle_pass"] is None and report["outcome"] == "INCONCLUSIVE"
    assert report["ledger_sha256"] is None and "ledger_sha256" in report["unmeasured"]
    assert report["reference_provenance"] == REFERENCE_PROVENANCE


# --- (c) dataset_sha256 e ledger_sha256 nos resumos -----------------------------------------------


def test_batch_summary_carries_dataset_sha256_matching_the_validator(tmp_path):
    task = _task()
    outdir = tmp_path / "out"
    summary = batch.run_batch([task], outdir, config=_config(),
                              make_llm=lambda t: ScriptedLLM(_solve(t)), secret=None)
    expected = validate_batch_integrity([task], scratch_root=tmp_path / "v").dataset_sha256
    assert summary["dataset_sha256"] == dataset_fingerprint([task]) == expected
    assert summary["integrity_validated"] is False  # padrão: o lote não afirma validação que não ocorreu
    assert summary["ledger_sha256"] == _sha(outdir / "batch-ledger.jsonl")
    on_disk = json.loads((outdir / "batch-summary.json").read_text(encoding="utf-8"))
    assert on_disk["dataset_sha256"] == summary["dataset_sha256"]
    assert on_disk["runs"][0]["ledger_sha256"] == _sha(outdir / "runs" / GREET / "ledger.jsonl")


def test_dataset_fingerprint_is_order_independent_and_task_sensitive():
    a, b = batch.builtin_suite()[0], batch.builtin_suite()[1]
    assert dataset_fingerprint([a, b]) == dataset_fingerprint([b, a])
    changed = batch.BatchTask(task_id=a.task_id, family=a.family, statement=a.statement + "!", files=a.files,
                              oracle=a.oracle)
    assert dataset_fingerprint([changed, b]) != dataset_fingerprint([a, b])


def test_run_batch_flag_is_recorded_when_the_gate_ran(tmp_path):
    task = _task()
    summary = batch.run_batch([task], tmp_path / "out", config=_config(),
                              make_llm=lambda t: ScriptedLLM(_solve(t)), secret=None, integrity_validated=True)
    assert summary["integrity_validated"] is True


# --- (c/d) laddering: schema, soma estrita, ledger por degrau, dataset ----------------------------


def _policy(global_max=1.0):
    return LadderPolicy.from_dict({"policy_id": "pol-t", "global_max_cost_usd": global_max, "rungs": [
        {"rung_id": "r0", "model": "m0", "harness_variant": "b0_plus", "max_steps": 4},
        {"rung_id": "r1", "model": "m1", "harness_variant": "b0_plus", "max_steps": 4}]})


def _wrong(cost):  # declara sucesso sem corrigir: o oráculo reprova
    return [make_response(calls=[submit("s1", "done")], cost=cost)]


def test_laddering_task_summary_schema_sums_all_rungs_and_pins_ledgers(tmp_path):
    task = _task()
    policy = _policy()
    script = {"r0": _wrong(0.20), "r1": _solve(task, cost=0.05)}
    dataset = dataset_fingerprint([task])
    payload = ladder_cli._run_task(
        task, policy, lambda rung: ScriptedLLM(script[rung.rung_id]), _config(harness_variant="b0_plus"),
        tmp_path / "out", dataset_sha256=dataset, integrity_validated=True)

    assert payload["schema"] == LADDERING_SCHEMA == "dv-laddering-summary-v1"
    assert payload["final_outcome"] == "SOLVED" and payload["resolved_at_rung"] == "r1"
    costs = [t["cost_lower_bound_usd"] for t in payload["rung_traces"]]
    assert payload["total_cost_lower_bound_usd"] == pytest.approx(sum(costs))  # r0 reprovado entra na soma
    # r0: 1 chamada a 0.20. r1: 2 chamadas (escrita + submissão) a 0.05 cada.
    assert costs[0] == pytest.approx(0.20) and costs[1] == pytest.approx(0.10)
    assert payload["ceiling_respected"] is True
    assert payload["telemetry_complete"] is True and payload["cost_accounting_status"] == "MEASURED"
    assert payload["dataset_sha256"] == dataset and payload["integrity_validated"] is True
    assert payload["harness_variant"] == "b0_plus"
    assert payload["reference_provenance"] == REFERENCE_PROVENANCE
    work = tmp_path / "out" / task.task_id / "work" / "rungs"
    assert set(payload["ledger_sha256_by_rung"]) == {"r0", "r1"}
    assert payload["ledger_sha256_by_rung"]["r0"] == _sha(work / "rung-00-r0" / "ledger.jsonl")
    assert payload["ledger_sha256_by_rung"]["r1"] == _sha(work / "rung-01-r1" / "ledger.jsonl")

    batch_summary = ladder_cli._aggregate([payload], policy, "p" * 64, dataset_sha256=dataset,
                                          integrity_validated=True)
    assert batch_summary["dataset_sha256"] == dataset
    assert batch_summary["ceiling_violations"] == 0
    assert batch_summary["telemetry_complete_tasks"] == 1
    assert batch_summary["ledger_sha256_by_task"][task.task_id] == payload["ledger_sha256_by_rung"]


def test_laddering_unknown_step_cost_is_inconclusive_and_ceiling_is_not_declared(tmp_path):
    policy = _policy()
    snapshot = tmp_path / "snap"
    snapshot.mkdir()
    (snapshot / "f.txt").write_text("x\n", encoding="utf-8")

    def step(rung, workspace, bash_logs, cap):
        return StepResult(status="completed", cost_lower_bound_usd=None, tokens_lower_bound=None,
                          wall_clock_ms=1, telemetry_complete=False, ledger_sha256=None)

    result = LadderController(policy, step_runner=step, oracle_verifier=lambda ws: True,
                              snapshot_dir=snapshot, work_root=tmp_path / "work").run("t")
    assert result.final_outcome == "SOLVED"  # oráculo não é alterado pela telemetria
    assert result.telemetry_complete is False
    assert result.cost_accounting_status == "INCONCLUSIVE"
    assert result.ceiling_respected is None  # sem custo medido, o teto não pode ser declarado respeitado
    assert result.ledger_sha256_by_rung == {"r0": None}


# --- (d) GAP-1 a GAP-8 e imutabilidade do contrato ------------------------------------------------


def test_gap_map_covers_every_gap_of_contract_v4_and_leaves_open_items_explicit():
    contract = json.loads((P1 / "run-accounting-contract-v4.json").read_text(encoding="utf-8"))
    assert {g["id"] for g in contract["implementation_gaps"]} == set(GAP_MAP)
    open_gaps = sorted(k for k, v in GAP_MAP.items() if v.startswith("ABERTO"))
    assert open_gaps == ["GAP-6", "GAP-7", "GAP-8"]


def test_contract_v4_protocol_and_policy_are_not_edited_by_this_change():
    # Os pins SHA-256 vivem em tests/test_governance_contracts.py; este teste só confirma que o arquivo existe.
    assert (P1 / "run-accounting-contract-v4.json").is_file()
    assert (P1 / "wp08-b0-plus-laddering-ablation-v1.json").is_file()
