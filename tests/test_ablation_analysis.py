"""Análise pareada e bootstrap do wp08 (GAP-8). Fixtures sintéticas determinísticas; zero rede.

Executar: python -m pytest tests/test_ablation_analysis.py -v
"""

from __future__ import annotations

import json
import re
import subprocess
import sys

import pytest

from support import ROOT, ScriptedLLM, make_response  # noqa: F401 (ROOT configura sys.path)
from dv_harness import ablation_analysis as aa
from dv_harness import batch
from dv_harness.task_integrity import dataset_fingerprint

sys.path.insert(0, str(ROOT / "scripts"))
import dv_analyze_ablation as cli  # noqa: E402
import dv_ladder_run as ladder_cli  # noqa: E402
from test_v4_telemetry_conformance import _config, _policy, _solve, _wrong  # noqa: E402

H = "d" * 64
REPS = ("rep-01", "rep-02", "rep-03")
SOLVED_OK = {"outcome": "SOLVED", "cost": 0.10}
FAILED = {"outcome": "FAILURE", "cost": 0.10}


def rec(arm, task, rep, outcome, cost, *, ceiling=True, mstatus=None, oracle=None, steps=5,
        edit_calls=0, edit_failures=0, doom=1):
    if mstatus is None:
        mstatus = "MEASURED" if cost is not None else "INCONCLUSIVE"
    return aa.RunRecord(
        arm=arm, task_id=task, rep=rep, status="completed", outcome=outcome,
        oracle_pass=(outcome == "SOLVED") if oracle is None else oracle,
        telemetry_complete=mstatus == "MEASURED", cost_accounting_status=mstatus, cost_lower_bound_usd=cost,
        steps=steps, edit_calls=edit_calls, edit_failures=edit_failures, doom_loop_streak_max=doom,
        ceiling_respected=ceiling)


def arm_batch(arm, cells, *, model="m0", gmax=1.0, dataset=H, contract=aa.CONTRACT_ID, variant=None):
    records = tuple(rec(arm, t, r, **kw) for (t, r), kw in sorted(cells.items()))
    return aa.ArmBatch(arm=arm, records=records, dataset_sha256=dataset, contract_id=contract,
                       harness_variant=variant or aa.ARM_VARIANT[arm], model=model,
                       global_max_cost_usd=gmax, input_sha256={})


def build(n_tasks, spec):
    """``spec[arm](i, j)`` devolve os kwargs de ``rec`` para a tarefa i e a repetição j."""
    tasks = [f"t{i:03d}" for i in range(n_tasks)]
    arms = {}
    for arm in aa.ARM_IDS:
        cells = {(t, r): spec[arm](i, j) for i, t in enumerate(tasks) for j, r in enumerate(REPS)}
        model = "m1" if arm == aa.ARM_C else "m0"
        arms[arm] = arm_batch(arm, cells, model=model, variant=None)
    return arms


def default_spec(**overrides):
    spec = {aa.ARM_A: lambda i, j: dict(FAILED), aa.ARM_B: lambda i, j: dict(SOLVED_OK),
            aa.ARM_C: lambda i, j: dict(SOLVED_OK), aa.ARM_D: lambda i, j: {"outcome": "SOLVED", "cost": 0.05}}
    spec.update(overrides)
    return spec


def _lo_hi(stat):
    return stat["ci95"][0], stat["ci95"][1]


# --- (a) determinismo ---------------------------------------------------------------------------


def test_same_inputs_same_seed_give_identical_intervals_hashes_and_verdicts():
    arms = build(12, default_spec())
    first = aa.analyze(arms)  # seed=20261120, 10.000 reamostragens (padrões)
    second = aa.analyze(arms)
    assert first == second
    assert first["seed"] == aa.DEFAULT_SEED == 20261120
    assert first["resamples"] == aa.DEFAULT_RESAMPLES == 10000
    assert first["analysis_code_sha256"] == second["analysis_code_sha256"]


def test_analysis_code_hash_is_stable_across_line_endings(tmp_path):
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    body = b"x = 1\ny = 2\n"
    lf.write_bytes(body)
    crlf.write_bytes(body.replace(b"\n", b"\r\n"))
    assert aa.analysis_code_sha256(lf) == aa.analysis_code_sha256(crlf)
    assert re.fullmatch(r"[0-9a-f]{64}", aa.analysis_code_sha256())


def test_seed_changes_draws_but_not_the_point_estimate():
    arms = build(12, default_spec())
    a = aa.analyze(arms, seed=1, resamples=500)
    b = aa.analyze(arms, seed=2, resamples=500)
    assert a["metrics"]["pass_rate_diff_B_minus_A"]["point"] == b["metrics"]["pass_rate_diff_B_minus_A"]["point"]


# --- (b) pareamento e integridade ---------------------------------------------------------------


def test_divergent_dataset_sha256_is_rejected_with_explicit_error():
    arms = build(4, default_spec())
    arms[aa.ARM_C] = arm_batch(aa.ARM_C, {(t, r): dict(SOLVED_OK) for t in ("t000", "t001", "t002", "t003")
                                          for r in REPS}, model="m1", dataset="e" * 64)
    with pytest.raises(aa.AblationInputError, match="mixed batches: dataset_sha256 differs"):
        aa.analyze(arms, resamples=50)


def test_contract_other_than_v4_is_rejected():
    arms = build(4, default_spec())
    arms[aa.ARM_D] = arm_batch(aa.ARM_D, {(t, r): {"outcome": "SOLVED", "cost": 0.05} for t in
                                          ("t000", "t001", "t002", "t003") for r in REPS},
                               dataset=H, contract="p1-run-accounting-v3", variant="b0_plus")
    with pytest.raises(aa.AblationInputError, match="contract_id"):
        aa.analyze(arms, resamples=50)


def test_missing_pair_or_too_few_repetitions_is_rejected():
    arms = build(4, default_spec())
    cells = {(r.task_id, r.rep): {"outcome": r.outcome, "cost": r.cost_lower_bound_usd}
             for r in arms[aa.ARM_B].records if not (r.task_id == "t000" and r.rep == "rep-02")}
    arms[aa.ARM_B] = arm_batch(aa.ARM_B, cells)
    with pytest.raises(aa.AblationInputError, match="runs, expected"):
        aa.analyze(arms, resamples=50)
    two = build(4, default_spec())
    two = {k: aa.ArmBatch(v.arm, tuple(r for r in v.records if r.rep != "rep-03"), v.dataset_sha256,
                          v.contract_id, v.harness_variant, v.model, v.global_max_cost_usd, {})
           for k, v in two.items()}
    with pytest.raises(aa.AblationInputError, match="at least 3"):
        aa.analyze(two, resamples=50)


def test_inconsistent_telemetry_record_is_rejected_at_ingestion():
    with pytest.raises(aa.AblationInputError, match="MEASURED run without a cost"):
        rec(aa.ARM_A, "t000", "rep-01", "SOLVED", None, mstatus="MEASURED")


# --- (c) H1: promoção e rejeição ----------------------------------------------------------------


def _h1_spec(b_fn, a_fn=None):
    return default_spec(**{aa.ARM_A: a_fn or (lambda i, j: {"outcome": "SOLVED", "cost": 0.10} if j == 0
                                             else dict(FAILED)),
                           aa.ARM_B: b_fn})


def test_h1_is_promoted_when_b0_plus_beats_b0_with_positive_ci_and_no_cost_regression():
    arms = build(12, _h1_spec(lambda i, j: dict(SOLVED_OK)))  # A resolve 1/3, B resolve 3/3, custo igual
    result = aa.analyze(arms, resamples=2000)
    lo, hi = _lo_hi(result["metrics"]["pass_rate_diff_B_minus_A"])
    assert lo > 0 and hi <= 1.0
    assert result["H1_b0_plus_vs_b0"]["verdict"] == "PROMOTE_B0_PLUS"
    assert result["H1_b0_plus_vs_b0"]["conditions"]["cost_no_regression"] is True


def test_h1_is_rejected_when_there_is_no_improvement():
    same = {aa.ARM_A: lambda i, j: {"outcome": "SOLVED", "cost": 0.10} if j == 0 else dict(FAILED)}
    arms = build(12, default_spec(**same, **{aa.ARM_B: same[aa.ARM_A]}))  # A e B idênticos
    result = aa.analyze(arms, resamples=2000)
    assert result["H1_b0_plus_vs_b0"]["verdict"] == "REJECT_B0_PLUS"


def test_h1_is_rejected_on_cost_regression_even_with_better_pass_rate():
    arms = build(12, _h1_spec(lambda i, j: {"outcome": "SOLVED", "cost": 1.0}))  # B melhor, mas 3x o custo por resolvido
    result = aa.analyze(arms, resamples=2000)
    assert result["H1_b0_plus_vs_b0"]["verdict"] == "REJECT_B0_PLUS"
    assert result["H1_b0_plus_vs_b0"]["conditions"]["cost_no_regression"] is False


def test_h1_is_inconclusive_when_more_than_ten_percent_of_b_cost_runs_are_inconclusive():
    def b(i, j):
        if j == 2:  # 1 de 3 repetições (33%) com custo INCONCLUSIVE, outcome SOLVED
            return {"outcome": "SOLVED", "cost": None}
        return dict(SOLVED_OK)
    arms = build(12, _h1_spec(b))
    result = aa.analyze(arms, resamples=2000)
    assert result["telemetry"][aa.ARM_B]["inconclusive_cost_fraction"] > 0.10
    assert result["H1_b0_plus_vs_b0"]["verdict"] == "INCONCLUSIVE"


def test_h1_undefined_pass_rate_is_inconclusive_not_promoted():
    arms = build(4, default_spec(**{aa.ARM_B: lambda i, j: {"outcome": "INCONCLUSIVE", "cost": 0.1}}))
    result = aa.analyze(arms, resamples=50)
    assert result["H1_b0_plus_vs_b0"]["verdict"] == "INCONCLUSIVE"


# --- (d) H2: promoção e rejeição por margem, custo e teto ---------------------------------------


def _d_spec(d_fn=None, c_fn=None):
    return default_spec(**{aa.ARM_D: d_fn or (lambda i, j: {"outcome": "SOLVED", "cost": 0.05}),
                           aa.ARM_C: c_fn or (lambda i, j: dict(SOLVED_OK))})


def test_h2_is_promoted_when_d_keeps_pass_rate_reduces_cost_and_has_no_ceiling_violation():
    result = aa.analyze(build(100, default_spec()), resamples=2000)
    assert result["telemetry"][aa.ARM_D]["ceiling_violations"] == 0
    assert result["H2_dv_laddering_vs_solo_r1"]["verdict"] == "PROMOTE_DV_LADDERING"
    assert _lo_hi(result["metrics"]["pass_rate_diff_D_minus_C"])[0] >= -aa.NON_INFERIORITY_MARGIN
    assert result["metrics"]["cost_per_solved_diff_D_minus_C"]["ci95"][1] < 0


def test_h2_is_rejected_by_non_inferiority_margin():
    # 7 de 100 tarefas: D reprova 1 de 3 repetições (diferença -1/3 nessas tarefas). IC95 inferior fica entre
    # -0,05 e -0,02: a margem de 0,02 reprova e uma margem de 0,05 aprovaria.
    def d(i, j):
        if i < 7 and j == 0:
            return dict(FAILED) | {"cost": 0.05}
        return {"outcome": "SOLVED", "cost": 0.05}
    result = aa.analyze(build(100, _d_spec(d_fn=d)))
    lo, _hi = _lo_hi(result["metrics"]["pass_rate_diff_D_minus_C"])
    assert -0.05 < lo < -0.02
    assert result["H2_dv_laddering_vs_solo_r1"]["verdict"] == "REJECT_DV_LADDERING"


def test_h2_is_rejected_when_d_is_not_cheaper():
    result = aa.analyze(build(12, _d_spec(d_fn=lambda i, j: {"outcome": "SOLVED", "cost": 0.15})), resamples=2000)
    assert result["H2_dv_laddering_vs_solo_r1"]["verdict"] == "REJECT_DV_LADDERING"


def test_h2_is_rejected_by_any_global_ceiling_violation_in_d():
    def d(i, j):
        return {"outcome": "SOLVED", "cost": 0.05, "ceiling": not (i == 0 and j == 1)}
    result = aa.analyze(build(12, _d_spec(d_fn=d)), resamples=2000)
    assert result["telemetry"][aa.ARM_D]["ceiling_violations"] == 1
    assert result["H2_dv_laddering_vs_solo_r1"]["verdict"] == "REJECT_DV_LADDERING"
    assert "ceiling" in result["H2_dv_laddering_vs_solo_r1"]["reasons"][0]


def test_h2_is_inconclusive_when_a_d_ceiling_cannot_be_verified():
    def d(i, j):
        return {"outcome": "SOLVED", "cost": 0.05, "ceiling": None if (i == 0 and j == 1) else True}
    result = aa.analyze(build(12, _d_spec(d_fn=d)), resamples=2000)
    assert result["H2_dv_laddering_vs_solo_r1"]["verdict"] == "INCONCLUSIVE"


# --- (e) segregação ortogonal de INCONCLUSIVE ----------------------------------------------------


def test_inconclusive_cost_run_keeps_outcome_and_oracle_and_never_imputes_zero():
    # t000/rep-02 de B: outcome SOLVED e oráculo PASS, mas custo não medido (tokens desconhecidos).
    def b(i, j):
        if i == 0 and j == 1:
            return {"outcome": "SOLVED", "cost": 0.50, "mstatus": "INCONCLUSIVE", "oracle": True}
        return dict(SOLVED_OK)
    arms = build(10, default_spec(**{aa.ARM_B: b}))
    result = aa.analyze(arms, resamples=200)

    flagged = [r for r in arms[aa.ARM_B].records if r.cost_accounting_status == "INCONCLUSIVE"]
    assert len(flagged) == 1 and flagged[0].oracle_pass is True and flagged[0].outcome == "SOLVED"
    tel = result["telemetry"][aa.ARM_B]
    assert tel["inconclusive_cost_runs"] == 1 and tel["inconclusive_cost_by_outcome"]["SOLVED"] == 1
    assert tel["oracle_pass_true"] == tel["outcome_counts"]["SOLVED"]  # oracle_pass intacto
    # cps de B = 0,10 exatamente: o custo de 0,50 do run INCONCLUSIVE não entra no numerador nem no denominador.
    assert result["arms"][aa.ARM_B]["cost_per_solved_usd_point"] == pytest.approx(0.10)
    assert result["arms"][aa.ARM_B]["cost_gate_ok"] is True  # 1 de 30 (3,3%) não excede 10%


def test_inconclusive_cost_count_is_reported_per_arm_with_fraction():
    arms = build(10, default_spec(**{aa.ARM_C: lambda i, j: {"outcome": "SOLVED", "cost": None}}))
    result = aa.analyze(arms, resamples=50)
    assert result["telemetry"][aa.ARM_C]["inconclusive_cost_runs"] == 30
    assert result["telemetry"][aa.ARM_C]["inconclusive_cost_fraction"] == 1.0
    assert result["arms"][aa.ARM_C]["cost_gate_ok"] is False
    assert result["arms"][aa.ARM_C]["cost_per_solved_usd_point"] is None  # sem nenhum custo medido, sem zero


# --- (f) CLI e artefatos reais -------------------------------------------------------------------


def test_help_lists_the_analysis_flags(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--arms-dir", "--arm-a", "--seed", "--resamples", "--output-json", "--print-code-sha256"):
        assert flag in out


def test_print_code_sha256_prints_the_module_hash_and_exits_zero(capsys):
    assert cli.main(["--print-code-sha256"]) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == aa.analysis_code_sha256()
    assert re.fullmatch(r"[0-9a-f]{64}", printed)


def test_cli_refuses_missing_arms_and_existing_output_without_writing(tmp_path, capsys):
    out = tmp_path / "analysis.json"
    assert cli.main(["--arms-dir", str(tmp_path), "--output-json", str(out)]) == 2
    assert not out.exists()
    out.write_text("{}", encoding="utf-8")
    assert cli.main(["--arms-dir", str(tmp_path), "--output-json", str(out)]) == 2
    assert out.read_text(encoding="utf-8") == "{}"


def _write_real_arms(root):
    """Artefatos reais: run_batch (A/B/C) e _aggregate da escada (D), 3 repetições, 1 tarefa."""
    task = batch.builtin_suite()[0]
    ds = dataset_fingerprint([task])
    layout = {aa.ARM_A: ("b0", "m0", _wrong(0.10)), aa.ARM_B: ("b0_plus", "m0", _solve(task)),
              aa.ARM_C: ("b0_plus", "m1", _solve(task))}
    for rep in REPS:
        for arm, (variant, model, script) in layout.items():
            outdir = root / arm / rep
            batch.run_batch([task], outdir, config=_config(harness_variant=variant, model=model),
                            make_llm=lambda t, s=script: ScriptedLLM(list(s)), secret=None)
        policy = _policy(global_max=0.5)
        rung_script = {"r0": _wrong(0.10), "r1": _solve(task)}
        payload = ladder_cli._run_task(task, policy, lambda rung, s=rung_script: ScriptedLLM(s[rung.rung_id]),
                                       _config(harness_variant="b0_plus", max_cost_usd=0.5),
                                       root / aa.ARM_D / rep, dataset_sha256=ds, integrity_validated=True)
        summary = ladder_cli._aggregate([payload], policy, "p" * 64, dataset_sha256=ds, integrity_validated=True)
        (root / aa.ARM_D / rep / "laddering-batch-summary.json").write_text(json.dumps(summary), encoding="utf-8")
    return ds


def test_real_artifacts_load_pair_and_analyze_end_to_end(tmp_path):
    root = tmp_path / "arms"
    ds = _write_real_arms(root)
    result_path = tmp_path / "analysis.json"
    assert cli.main(["--arms-dir", str(root), "--seed", "20261120", "--resamples", "200",
                     "--output-json", str(result_path)]) == 0
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["dataset_sha256"] == ds and result["repetitions"] == 3
    assert result["arms"][aa.ARM_A]["input_sha256"].keys() == set(REPS)  # rastreio dos artefatos de entrada
    assert result["H1_b0_plus_vs_b0"]["verdict"] in {"PROMOTE_B0_PLUS", "REJECT_B0_PLUS", "INCONCLUSIVE"}
    assert result["H2_dv_laddering_vs_solo_r1"]["verdict"] in {"PROMOTE_DV_LADDERING", "REJECT_DV_LADDERING",
                                                              "INCONCLUSIVE"}
    assert result["telemetry"][aa.ARM_D]["ceiling_violations"] == 0


def test_real_arms_with_a_mismatched_batch_are_refused(tmp_path):
    root = tmp_path / "arms"
    _write_real_arms(root)
    path = root / aa.ARM_C / "rep-01" / "batch-summary.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["dataset_sha256"] = "f" * 64  # lote misturado
    path.write_text(json.dumps(data), encoding="utf-8")
    out = tmp_path / "analysis.json"
    assert cli.main(["--arms-dir", str(root), "--resamples", "50", "--output-json", str(out)]) == 2
    assert not out.exists()


def test_cli_runs_as_a_script_and_prints_the_code_hash():
    completed = subprocess.run([sys.executable, str(ROOT / "scripts" / "dv_analyze_ablation.py"),
                                "--print-code-sha256"], capture_output=True, text=True, check=False, cwd=ROOT)
    assert completed.returncode == 0
    assert completed.stdout.strip() == aa.analysis_code_sha256()
