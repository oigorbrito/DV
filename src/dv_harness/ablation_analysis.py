"""Análise pareada e bootstrap do protocolo wp08 (fecha GAP-8). Função pura, só stdlib, determinística.

Fontes normativas (somente leitura, não editadas aqui):
- experiments/p1/wp08-b0-plus-laddering-ablation-v1.json: métricas, bootstrap, H1, H2, regra de INCONCLUSIVE.
- experiments/p1/run-accounting-contract-v4.json: regras a-d (telemetria, teto, rastreabilidade).

Entrada: um diretório por braço, com subpastas ``rep-NN``. Cada subpasta contém o resumo do lote do braço:
``batch-summary.json`` (A/B/C) ou ``laddering-batch-summary.json`` (D). Cada subpasta é uma repetição completa
(todas as tarefas, uma vez por braço), e as repetições são pareadas entre braços pelo nome da pasta.

Saída: validações, telemetria por braço, ICs e vereditos H1/H2, com ``analysis_code_sha256`` deste arquivo.

Decisões de interpretação, todas registradas em ``notes`` na saída:
- ``pass_rate`` é ``SOLVED / (SOLVED + FAILURE)`` por run, conforme wp08. ``INCONCLUSIVE`` fica fora do
  denominador. ``oracle_pass`` é reportado como diagnóstico, não como critério.
- ``cost_per_solved_usd`` usa somente runs ``MEASURED``. Runs ``INCONCLUSIVE`` de custo são contados à parte,
  nunca imputados como zero e nunca descartados em silêncio.
- A não inferioridade de H2 usa o limite inferior do IC95 (``>= -0,02``), como no wp08.
- ``INCONCLUSIVE`` de H1/H2 só ocorre quando o critério depende de custo e o custo não é comparável (braço com
  mais de 10% de custo ``INCONCLUSIVE``, ou ``cost_per_solved`` indefinido), ou quando a métrica é indefinida.
  Um critério já reprovado sem custo gera ``REJECT``.
- Teto global: para A/B/C o teto é o do lote (``max_cost_usd_batch``); para D, o teto é por tarefa (cada tarefa
  tem seu ``global_max_cost_usd``). Esse desenho vem do wp08 e é reportado como nota.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROTOCOL_ID = "wp08-b0-plus-laddering-ablation-v1"
ANALYSIS_SCHEMA = "dv-ablation-analysis-v1"
CONTRACT_ID = "p1-run-accounting-v4"
DEFAULT_SEED = 20261120
DEFAULT_RESAMPLES = 10000
CONFIDENCE_LOWER_Q = 0.025  # IC95 = [p2,5; p97,5]
CONFIDENCE_UPPER_Q = 0.975
NON_INFERIORITY_MARGIN = 0.02  # H2: IC95 inferior de (pass[D] - pass[C]) >= -0,02
COST_REGRESSION_TOLERANCE = 0.10  # H1: IC95 superior de dCPS <= 0,10 x CPS(A)
INCONCLUSIVE_COST_LIMIT = 0.10  # braço com mais de 10% de custo INCONCLUSIVE: comparação de custo INCONCLUSIVE
MIN_REPETITIONS = 3

ARM_A, ARM_B, ARM_C, ARM_D = "A_B0", "B_B0_PLUS", "C_SOLO_R1", "D_DV_LADDERING"
ARM_IDS = (ARM_A, ARM_B, ARM_C, ARM_D)
ARM_VARIANT = {ARM_A: "b0", ARM_B: "b0_plus", ARM_C: "b0_plus", ARM_D: "b0_plus"}
BATCH_ARTIFACT = "batch-summary.json"
LADDER_ARTIFACT = "laddering-batch-summary.json"
ARM_ARTIFACT = {ARM_A: BATCH_ARTIFACT, ARM_B: BATCH_ARTIFACT, ARM_C: BATCH_ARTIFACT, ARM_D: LADDER_ARTIFACT}
BATCH_SCHEMA = "dv-batch-summary-v1"
LADDER_SCHEMA = "dv-laddering-batch-v1"
MEASURED = "MEASURED"
INCONCLUSIVE_COST = "INCONCLUSIVE"
OUTCOME_SOLVED = "SOLVED"
OUTCOME_FAILURE = "FAILURE"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"
VERDICT_PROMOTE_H1 = "PROMOTE_B0_PLUS"
VERDICT_REJECT_H1 = "REJECT_B0_PLUS"
VERDICT_PROMOTE_H2 = "PROMOTE_DV_LADDERING"
VERDICT_REJECT_H2 = "REJECT_DV_LADDERING"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"
_REP_RE = re.compile(r"rep-[0-9]{2,}")


class AblationInputError(ValueError):
    """Entrada inválida ou incompatível. Levantada antes de qualquer cálculo; nada é gravado."""


# --- registros normalizados ------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    """Um run de um braço, já normalizado a partir do resumo. ``None`` = não medido (nunca zero)."""

    arm: str
    task_id: str
    rep: str
    status: str
    outcome: str
    oracle_pass: bool | None
    telemetry_complete: bool
    cost_accounting_status: str
    cost_lower_bound_usd: float | None
    steps: int | None = None
    edit_calls: int | None = None
    edit_failures: int | None = None
    doom_loop_streak_max: int | None = None
    ceiling_respected: bool | None = None

    def __post_init__(self) -> None:
        if self.outcome not in (OUTCOME_SOLVED, OUTCOME_FAILURE, OUTCOME_INCONCLUSIVE):
            raise AblationInputError(f"{self.arm}/{self.task_id}/{self.rep}: unknown outcome {self.outcome!r}")
        if self.cost_accounting_status not in (MEASURED, INCONCLUSIVE_COST):
            raise AblationInputError(f"{self.arm}/{self.task_id}/{self.rep}: unknown cost_accounting_status")
        if self.telemetry_complete != (self.cost_accounting_status == MEASURED):
            raise AblationInputError(f"{self.arm}/{self.task_id}/{self.rep}: telemetry_complete disagrees with "
                                     "cost_accounting_status")
        if self.cost_accounting_status == MEASURED and self.cost_lower_bound_usd is None:
            raise AblationInputError(f"{self.arm}/{self.task_id}/{self.rep}: MEASURED run without a cost value")


@dataclass(frozen=True)
class ArmBatch:
    """Todas as repetições de um braço, com metadados do lote que precisam ser idênticos entre braços."""

    arm: str
    records: tuple[RunRecord, ...]
    dataset_sha256: str
    contract_id: str
    harness_variant: str
    model: str | None
    global_max_cost_usd: float
    input_sha256: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskArmStats:
    """Agregado de um (tarefa, braço) sobre as repetições. Cada métrica é ``None`` quando indefinida."""

    pass_rate: float | None
    oracle_pass_rate: float | None
    edit_failure_rate: float | None
    doom_loop_streak_max: float | None
    steps: float | None
    cost_sum: float  # soma somente de runs MEASURED
    solved_measured: int  # SOLVED entre runs MEASURED (denominador de cost_per_solved)


# --- utilidades numéricas (ordem de soma fixa: resultado reproduzível) --------------------------


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _quantile(sorted_values: Sequence[float], q: float) -> float:
    """Quantil tipo 7 (interpolação linear). Determinístico e documentado."""
    pos = q * (len(sorted_values) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (pos - lo)


def _ratio(cost: float, solved: int) -> float | None:
    return cost / solved if solved > 0 else None


def _key(data: Mapping[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise AblationInputError(f"{where}: artifact lacks field {key!r}; regenerate it with the current harness")
    return data[key]


# --- agregação por (tarefa, braço) -------------------------------------------------------------


def _group_by_task(arm: ArmBatch) -> dict[str, list[RunRecord]]:
    grouped: dict[str, list[RunRecord]] = defaultdict(list)
    for record in arm.records:
        grouped[record.task_id].append(record)
    for runs in grouped.values():
        runs.sort(key=lambda r: r.rep)
    return grouped


def _task_arm_stats(runs: Sequence[RunRecord]) -> TaskArmStats:
    verified = [r for r in runs if r.outcome in (OUTCOME_SOLVED, OUTCOME_FAILURE)]  # INCONCLUSIVE fora
    oracle = [r for r in runs if r.oracle_pass is not None]
    edit = [r.edit_failures / r.edit_calls for r in runs if r.edit_calls]  # sem chamada de edit: indefinido
    doom = [float(r.doom_loop_streak_max) for r in runs if r.doom_loop_streak_max is not None]
    steps = [float(r.steps) for r in runs if r.steps is not None and r.status != "harness_error"]
    measured = [r for r in runs if r.cost_accounting_status == MEASURED and r.cost_lower_bound_usd is not None]
    return TaskArmStats(
        pass_rate=_mean([1.0 if r.outcome == OUTCOME_SOLVED else 0.0 for r in verified]),
        oracle_pass_rate=_mean([1.0 if r.oracle_pass else 0.0 for r in oracle]),
        edit_failure_rate=_mean(edit),
        doom_loop_streak_max=_mean(doom),
        steps=_mean(steps),
        cost_sum=sum(r.cost_lower_bound_usd for r in measured),
        solved_measured=sum(1 for r in measured if r.outcome == OUTCOME_SOLVED),
    )


def _arm_cost_per_solved(stats: Sequence[TaskArmStats]) -> float | None:
    return _ratio(sum(s.cost_sum for s in stats), sum(s.solved_measured for s in stats))


def _telemetry(arm: ArmBatch) -> dict[str, Any]:
    recs = arm.records
    total = len(recs)
    inconclusive = sum(1 for r in recs if r.cost_accounting_status == INCONCLUSIVE_COST)
    return {
        "n_runs": total,
        "measured_cost_runs": total - inconclusive,
        "inconclusive_cost_runs": inconclusive,
        "inconclusive_cost_fraction": inconclusive / total if total else None,
        "inconclusive_cost_by_outcome": {
            k: sum(1 for r in recs if r.cost_accounting_status == INCONCLUSIVE_COST and r.outcome == k)
            for k in (OUTCOME_SOLVED, OUTCOME_FAILURE, OUTCOME_INCONCLUSIVE)
        },
        "outcome_counts": {k: sum(1 for r in recs if r.outcome == k)
                           for k in (OUTCOME_SOLVED, OUTCOME_FAILURE, OUTCOME_INCONCLUSIVE)},
        "oracle_pass_true": sum(1 for r in recs if r.oracle_pass is True),
        "oracle_pass_without_solved_outcome": sum(1 for r in recs if r.oracle_pass is True
                                                  and r.outcome != OUTCOME_SOLVED),
        "ceiling_violations": sum(1 for r in recs if r.ceiling_respected is False),
        "ceiling_unverifiable": sum(1 for r in recs if r.ceiling_respected is None),
    }


def _cost_gate(arm: str, telemetry: Mapping[str, Any], cps: float | None) -> tuple[bool, str | None]:
    """Comparação de custo de um braço é válida? Usa aritmética inteira para o limite de 10%."""
    inc, total = telemetry["inconclusive_cost_runs"], telemetry["n_runs"]
    if inc * 10 > total:  # fração > 0,10
        return False, f"{arm}: {inc} of {total} runs have INCONCLUSIVE cost (> 10%): cost comparison is INCONCLUSIVE"
    if cps is None:
        return False, f"{arm}: no MEASURED SOLVED run, cost_per_solved is undefined"
    return True, None


# --- validação de pareamento e integridade -----------------------------------------------------


def _check_bootstrap_params(seed: Any, resamples: Any) -> None:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise AblationInputError("seed must be an integer")
    if isinstance(resamples, bool) or not isinstance(resamples, int) or resamples < 1:
        raise AblationInputError("resamples must be an integer >= 1")


def _check_identity(arms: Mapping[str, ArmBatch]) -> None:
    """Mesmo lote, mesmo contrato, mesma variante por braço, mesmo teto e mesmo modelo de Rung_0."""
    missing = [a for a in ARM_IDS if a not in arms]
    if missing:
        raise AblationInputError(f"missing arm(s): {missing}")
    extra = [a for a in arms if a not in ARM_IDS]
    if extra:
        raise AblationInputError(f"unknown arm(s): {extra}")
    datasets = {a: arms[a].dataset_sha256 for a in ARM_IDS}
    if len(set(datasets.values())) != 1:
        detail = ", ".join(f"{a}={h}" for a, h in datasets.items())
        raise AblationInputError(f"mixed batches: dataset_sha256 differs across arms ({detail})")
    for a in ARM_IDS:
        if arms[a].contract_id != CONTRACT_ID:
            raise AblationInputError(f"{a}: contract_id is {arms[a].contract_id!r}, expected {CONTRACT_ID!r}")
        if arms[a].harness_variant != ARM_VARIANT[a]:
            raise AblationInputError(f"{a}: harness_variant is {arms[a].harness_variant!r}, "
                                     f"expected {ARM_VARIANT[a]!r}")
    if len({arms[a].global_max_cost_usd for a in ARM_IDS}) != 1:
        raise AblationInputError("global_max_cost_usd differs across arms (wp08 pc4 requires one value)")
    if arms[ARM_A].model != arms[ARM_B].model:
        raise AblationInputError("A_B0 and B_B0_PLUS must use the same economic model (Rung_0)")


def _check_pairing(arms: Mapping[str, ArmBatch]) -> int:
    """Cada braço tem todas as tarefas, uma vez por repetição, com as mesmas repetições. Devolve N."""
    reps = {a: sorted({r.rep for r in arms[a].records}) for a in ARM_IDS}
    if len({tuple(v) for v in reps.values()}) != 1:
        raise AblationInputError("arms have different repetition sets; pairing requires the same rep-NN folders")
    n = len(reps[ARM_A])
    if n < MIN_REPETITIONS:
        raise AblationInputError(f"{n} repetition(s) per arm; wp08 requires at least {MIN_REPETITIONS}")
    tasks = {a: sorted({r.task_id for r in arms[a].records}) for a in ARM_IDS}
    if len({tuple(v) for v in tasks.values()}) != 1:
        raise AblationInputError("arms have different task sets; pairing requires the same tasks")
    for a in ARM_IDS:
        seen = {(r.task_id, r.rep) for r in arms[a].records}
        if len(seen) != len(arms[a].records):
            raise AblationInputError(f"{a}: duplicate run for the same task and repetition")
        expected = len(tasks[a]) * n
        if len(seen) != expected:
            raise AblationInputError(f"{a}: {len(seen)} runs, expected {expected} (every task once per repetition)")
    return n


def validate_arms(arms: Mapping[str, ArmBatch]) -> int:
    """Rejeita lotes misturados e pareamento incompleto. Devolve o número de repetições N."""
    _check_identity(arms)
    return _check_pairing(arms)


# --- bootstrap pareado por tarefa ---------------------------------------------------------------


def _summary(point: float | None, draws: list[float], n_units: int, resamples: int) -> dict[str, Any]:
    if point is None or not draws:
        return {"point": point, "ci95": None, "n_units": n_units, "n_defined_resamples": len(draws),
                "n_resamples": resamples, "reason": "undefined: no defined paired value"}
    ordered = sorted(draws)
    return {"point": point, "ci95": [_quantile(ordered, CONFIDENCE_LOWER_Q), _quantile(ordered, CONFIDENCE_UPPER_Q)],
            "n_units": n_units, "n_defined_resamples": len(draws), "n_resamples": resamples, "reason": None}


def _paired_bootstrap(mean_diffs: Mapping[str, list[float | None]],
                      ratio_diffs: Mapping[str, tuple[list[float], list[int], list[float], list[int]]],
                      *, seed: int, resamples: int) -> dict[str, dict[str, Any]]:
    """Reamostra tarefas com reposição. A mesma reamostragem alimenta todas as métricas (pareamento preservado)."""
    n_tasks = len(next(iter(mean_diffs.values()))) if mean_diffs else len(next(iter(ratio_diffs.values()))[0])
    rng = random.Random(seed)
    out_mean: dict[str, list[float]] = {k: [] for k in mean_diffs}
    out_ratio: dict[str, list[float]] = {k: [] for k in ratio_diffs}
    for _ in range(resamples):
        idx = [rng.randrange(n_tasks) for _ in range(n_tasks)]
        for name, values in mean_diffs.items():
            picked = [values[i] for i in idx if values[i] is not None]
            if picked:
                out_mean[name].append(sum(picked) / len(picked))
        for name, (cx, sx, cy, sy) in ratio_diffs.items():
            x = _ratio(sum(cx[i] for i in idx), sum(sx[i] for i in idx))
            y = _ratio(sum(cy[i] for i in idx), sum(sy[i] for i in idx))
            if x is not None and y is not None:
                out_ratio[name].append(x - y)
    results: dict[str, dict[str, Any]] = {}
    for name, values in mean_diffs.items():
        defined = [v for v in values if v is not None]
        point = sum(defined) / len(defined) if defined else None
        results[name] = _summary(point, out_mean[name], len(defined), resamples)
    for name, (cx, sx, cy, sy) in ratio_diffs.items():
        x = _ratio(sum(cx), sum(sx))
        y = _ratio(sum(cy), sum(sy))
        point = x - y if x is not None and y is not None else None
        results[name] = _summary(point, out_ratio[name], n_tasks, resamples)
    return results


# --- decisões H1 e H2 (wp08) -------------------------------------------------------------------


def decide_h1(metrics: Mapping[str, Mapping[str, Any]], cost_ok: Mapping[str, bool],
              cost_reasons: Sequence[str], cps_a: float | None) -> dict[str, Any]:
    pass_m = metrics["pass_rate_diff_B_minus_A"]
    edit_m = metrics["edit_failure_rate_diff_B_minus_A"]
    cps_m = metrics["cost_per_solved_diff_B_minus_A"]
    if pass_m["ci95"] is None:
        return {"verdict": VERDICT_INCONCLUSIVE, "reasons": [f"pass_rate difference undefined: {pass_m['reason']}"],
                "conditions": {}}
    p1 = pass_m["ci95"][0] > 0
    p2 = edit_m["ci95"] is not None and edit_m["ci95"][1] < 0 and pass_m["ci95"][0] >= 0
    conditions: dict[str, Any] = {"pass_ci_lower_gt_0": p1, "edit_ci_upper_lt_0_and_pass_ci_lower_ge_0": p2}
    if not (p1 or p2):
        return {"verdict": VERDICT_REJECT_H1, "reasons": ["no promotion condition holds (wp08 H1)"],
                "conditions": conditions}
    if not (cost_ok[ARM_A] and cost_ok[ARM_B]):
        return {"verdict": VERDICT_INCONCLUSIVE, "reasons": list(cost_reasons), "conditions": conditions}
    bound = COST_REGRESSION_TOLERANCE * cps_a if cps_a is not None else None
    if cps_m["ci95"] is None or bound is None:
        return {"verdict": VERDICT_INCONCLUSIVE, "reasons": ["cost_per_solved difference undefined"],
                "conditions": conditions}
    no_regression = cps_m["ci95"][1] <= bound
    conditions["cost_no_regression"] = no_regression
    if no_regression:
        return {"verdict": VERDICT_PROMOTE_H1, "reasons": [], "conditions": conditions}
    return {"verdict": VERDICT_REJECT_H1, "reasons": ["cost regression above 10% of CPS(A)"], "conditions": conditions}


def decide_h2(metrics: Mapping[str, Mapping[str, Any]], cost_ok: Mapping[str, bool],
              cost_reasons: Sequence[str], ceiling_violations_d: int, ceiling_unverifiable_d: int) -> dict[str, Any]:
    pass_m = metrics["pass_rate_diff_D_minus_C"]
    cps_m = metrics["cost_per_solved_diff_D_minus_C"]
    if ceiling_violations_d > 0:  # wp08: zero violações de teto é condição de promoção; violação reprova
        return {"verdict": VERDICT_REJECT_H2, "reasons": [f"{ceiling_violations_d} global ceiling violation(s) in D"],
                "conditions": {"ceiling_violations_d": ceiling_violations_d}}
    if pass_m["ci95"] is None:
        return {"verdict": VERDICT_INCONCLUSIVE, "reasons": [f"pass_rate difference undefined: {pass_m['reason']}"],
                "conditions": {}}
    non_inferior = pass_m["ci95"][0] >= -NON_INFERIORITY_MARGIN
    conditions: dict[str, Any] = {"pass_ci_lower_ge_minus_margin": non_inferior,
                                  "margin": NON_INFERIORITY_MARGIN}
    if not non_inferior:
        return {"verdict": VERDICT_REJECT_H2, "reasons": ["non-inferiority margin not met (wp08 H2)"],
                "conditions": conditions}
    if ceiling_unverifiable_d > 0:
        return {"verdict": VERDICT_INCONCLUSIVE, "reasons": [f"{ceiling_unverifiable_d} D task(s) with unverifiable "
                                                              "ceiling"], "conditions": conditions}
    if not (cost_ok[ARM_C] and cost_ok[ARM_D]):
        return {"verdict": VERDICT_INCONCLUSIVE, "reasons": list(cost_reasons), "conditions": conditions}
    if cps_m["ci95"] is None:
        return {"verdict": VERDICT_INCONCLUSIVE, "reasons": ["cost_per_solved difference undefined"],
                "conditions": conditions}
    cheaper = cps_m["ci95"][1] < 0
    conditions["cost_per_solved_ci_upper_lt_0"] = cheaper
    if cheaper:
        return {"verdict": VERDICT_PROMOTE_H2, "reasons": [], "conditions": conditions}
    return {"verdict": VERDICT_REJECT_H2, "reasons": ["no significant cost_per_solved reduction"],
            "conditions": conditions}


# --- análise completa ---------------------------------------------------------------------------


def _arm_tables(arms: Mapping[str, ArmBatch], tasks: Sequence[str]):
    grouped = {a: _group_by_task(arms[a]) for a in ARM_IDS}
    stats = {a: {t: _task_arm_stats(grouped[a][t]) for t in tasks} for a in ARM_IDS}
    per_arm = {a: [stats[a][t] for t in tasks] for a in ARM_IDS}
    telemetry = {a: _telemetry(arms[a]) for a in ARM_IDS}
    cps = {a: _arm_cost_per_solved(per_arm[a]) for a in ARM_IDS}
    return stats, per_arm, telemetry, cps


def _metric_inputs(stats: Mapping[str, Mapping[str, TaskArmStats]], tasks: Sequence[str]):
    """Diferenças pareadas por tarefa (mesma ordem de ``tasks`` em todas as métricas)."""

    def diff(x: str, y: str, key: str) -> list[float | None]:
        out: list[float | None] = []
        for t in tasks:
            a, b = getattr(stats[x][t], key), getattr(stats[y][t], key)
            out.append(a - b if a is not None and b is not None else None)
        return out

    def ratio_arrays(x: str, y: str) -> tuple[list[float], list[int], list[float], list[int]]:
        return ([stats[x][t].cost_sum for t in tasks], [stats[x][t].solved_measured for t in tasks],
                [stats[y][t].cost_sum for t in tasks], [stats[y][t].solved_measured for t in tasks])

    mean_diffs = {
        "pass_rate_diff_B_minus_A": diff(ARM_B, ARM_A, "pass_rate"),
        "edit_failure_rate_diff_B_minus_A": diff(ARM_B, ARM_A, "edit_failure_rate"),
        "doom_loop_streak_max_diff_B_minus_A": diff(ARM_B, ARM_A, "doom_loop_streak_max"),
        "steps_diff_B_minus_A": diff(ARM_B, ARM_A, "steps"),
        "oracle_pass_rate_diff_B_minus_A": diff(ARM_B, ARM_A, "oracle_pass_rate"),
        "pass_rate_diff_D_minus_C": diff(ARM_D, ARM_C, "pass_rate"),
        "oracle_pass_rate_diff_D_minus_C": diff(ARM_D, ARM_C, "oracle_pass_rate"),
    }
    ratio_diffs = {
        "cost_per_solved_diff_B_minus_A": ratio_arrays(ARM_B, ARM_A),
        "cost_per_solved_diff_D_minus_C": ratio_arrays(ARM_D, ARM_C),
    }
    return mean_diffs, ratio_diffs


def _analysis_notes(n_reps: int, telemetry: Mapping[str, Mapping[str, Any]]) -> list[str]:
    notes = [
        "pass_rate usa outcome (wp08); oracle_pass é diagnóstico. Decisões não usam oracle_pass.",
        "cost_per_solved usa somente runs MEASURED; INCONCLUSIVE de custo são reportados em telemetry.",
        "H1 usa pass_rate e edit_failure_rate como critérios; doom_loop_streak_max e steps são secundários (wp08).",
        "Teto: A/B/C por lote (max_cost_usd_batch); D por tarefa (global_max_cost_usd por tarefa). Desenho do wp08.",
    ]
    if n_reps % 4 != 0:
        notes.append(f"N={n_reps} não é múltiplo de 4: equilíbrio posicional completo não atingido (wp08).")
    for a in ARM_IDS:
        mismatch = telemetry[a]["oracle_pass_without_solved_outcome"]
        if mismatch:
            notes.append(f"{a}: {mismatch} run(s) com oracle_pass=True e outcome != SOLVED (ex.: status "
                         "budget_unverifiable); outcome prevalece nas decisões.")
    return notes


def analyze(arms: Mapping[str, ArmBatch], *, seed: int = DEFAULT_SEED,
            resamples: int = DEFAULT_RESAMPLES) -> dict[str, Any]:
    _check_bootstrap_params(seed, resamples)
    n_reps = validate_arms(arms)
    tasks = sorted({r.task_id for r in arms[ARM_A].records})
    stats, per_arm, telemetry, cps = _arm_tables(arms, tasks)

    cost_ok: dict[str, bool] = {}
    cost_reasons: list[str] = []
    for a in ARM_IDS:
        ok, reason = _cost_gate(a, telemetry[a], cps[a])
        cost_ok[a] = ok
        if reason:
            cost_reasons.append(reason)

    mean_diffs, ratio_diffs = _metric_inputs(stats, tasks)
    metrics = _paired_bootstrap(mean_diffs, ratio_diffs, seed=seed, resamples=resamples)
    h1 = decide_h1(metrics, cost_ok, cost_reasons, cps[ARM_A])
    h2 = decide_h2(metrics, cost_ok, cost_reasons, telemetry[ARM_D]["ceiling_violations"],
                   telemetry[ARM_D]["ceiling_unverifiable"])
    return {
        "schema": ANALYSIS_SCHEMA,
        "protocol_id": PROTOCOL_ID,
        "contract_id": CONTRACT_ID,
        "dataset_sha256": arms[ARM_A].dataset_sha256,
        "global_max_cost_usd": arms[ARM_A].global_max_cost_usd,
        "repetitions": n_reps,
        "tasks": tasks,
        "seed": seed,
        "resamples": resamples,
        "confidence": 0.95,
        "analysis_code_sha256": analysis_code_sha256(),
        "arms": {a: {
            "model": arms[a].model,
            "harness_variant": arms[a].harness_variant,
            "input_sha256": dict(sorted(arms[a].input_sha256.items())),
            "cost_per_solved_usd_point": cps[a],
            "pass_rate_mean_over_tasks": _mean([s.pass_rate for s in per_arm[a] if s.pass_rate is not None]),
            "cost_gate_ok": cost_ok[a],
        } for a in ARM_IDS},
        "telemetry": telemetry,
        "metrics": metrics,
        "H1_b0_plus_vs_b0": h1,
        "H2_dv_laddering_vs_solo_r1": h2,
        "notes": _analysis_notes(n_reps, telemetry),
    }


# --- ingestão de artefatos ------------------------------------------------------------------------


def _batch_records(arm: str, rep: str, data: Mapping[str, Any]) -> tuple[list[RunRecord], dict[str, Any]]:
    where = f"{arm}/{rep}"
    if data.get("schema") != BATCH_SCHEMA:
        raise AblationInputError(f"{where}: schema is {data.get('schema')!r}, expected {BATCH_SCHEMA!r}")
    if data.get("aborted") is not None or data.get("executed") != data.get("total_tasks"):
        raise AblationInputError(f"{where}: incomplete batch (aborted={data.get('aborted')!r}, "
                                 f"executed={data.get('executed')}/{data.get('total_tasks')}); pairing needs every task")
    global_max = float(_key(data, "max_cost_usd_batch", where))
    records: list[RunRecord] = []
    running, unknown = 0.0, False
    for entry in _key(data, "runs", where):
        cost = _key(entry, "total_cost_usd", where)
        if cost is None:
            unknown = True
        else:
            running += cost
        # Teto do lote: soma cumulativa dos limites inferiores. Custo desconhecido torna a verificação indefinida.
        ceiling = None if unknown else running <= global_max
        records.append(RunRecord(
            arm=arm, task_id=_key(entry, "task_id", where), rep=rep, status=_key(entry, "status", where),
            outcome=_key(entry, "outcome", where), oracle_pass=_key(entry, "oracle_pass", where),
            telemetry_complete=_key(entry, "telemetry_complete", where),
            cost_accounting_status=_key(entry, "cost_accounting_status", where),
            cost_lower_bound_usd=cost, steps=_key(entry, "steps", where),
            edit_calls=_key(entry, "edit_file_calls", where), edit_failures=_key(entry, "edit_file_failures", where),
            doom_loop_streak_max=_key(entry, "doom_loop_streak_max", where), ceiling_respected=ceiling))
    meta = {"dataset_sha256": _key(data, "dataset_sha256", where), "contract_id": _key(data, "contract_id", where),
            "harness_variant": _key(data, "harness_variant", where), "model": _key(data, "model_requested", where),
            "global_max_cost_usd": global_max}
    return records, meta


def _ladder_records(arm: str, rep: str, data: Mapping[str, Any]) -> tuple[list[RunRecord], dict[str, Any]]:
    where = f"{arm}/{rep}"
    if data.get("schema") != LADDER_SCHEMA:
        raise AblationInputError(f"{where}: schema is {data.get('schema')!r}, expected {LADDER_SCHEMA!r}")
    detail = _key(data, "tasks_detail", where)
    if _key(data, "tasks", where) != len(detail):
        raise AblationInputError(f"{where}: tasks_detail is incomplete")
    records = [RunRecord(
        arm=arm, task_id=_key(e, "task_id", where), rep=rep, status="laddering",
        outcome=_key(e, "final_outcome", where), oracle_pass=None,
        telemetry_complete=_key(e, "telemetry_complete", where),
        cost_accounting_status=_key(e, "cost_accounting_status", where),
        cost_lower_bound_usd=_key(e, "total_cost_lower_bound_usd", where),
        ceiling_respected=_key(e, "ceiling_respected", where)) for e in detail]
    meta = {"dataset_sha256": _key(data, "dataset_sha256", where), "contract_id": _key(data, "contract_id", where),
            "harness_variant": _key(data, "harness_variant", where), "model": None,
            "global_max_cost_usd": float(_key(data, "global_max_cost_usd", where))}
    return records, meta


def _single(arm: str, metas: Sequence[Mapping[str, Any]], key: str) -> Any:
    values = {json.dumps(m[key]) for m in metas}
    if len(values) != 1:
        raise AblationInputError(f"{arm}: {key} differs between repetitions (mixed batches)")
    return metas[0][key]


def load_arm(arm: str, arm_dir: Path) -> ArmBatch:
    """Lê as repetições ``rep-NN`` de um braço. Nada é escrito."""
    if arm not in ARM_IDS:
        raise AblationInputError(f"unknown arm {arm!r}")
    arm_dir = Path(arm_dir)
    rep_dirs = sorted(p for p in arm_dir.iterdir() if p.is_dir() and _REP_RE.fullmatch(p.name)) \
        if arm_dir.is_dir() else []
    if not rep_dirs:
        raise AblationInputError(f"{arm}: no rep-NN directories in {arm_dir}")
    records: list[RunRecord] = []
    metas: list[dict[str, Any]] = []
    input_sha: dict[str, str] = {}
    for rep_dir in rep_dirs:
        path = rep_dir / ARM_ARTIFACT[arm]
        if not path.is_file():
            raise AblationInputError(f"{arm}/{rep_dir.name}: missing {ARM_ARTIFACT[arm]}")
        raw = path.read_bytes()
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AblationInputError(f"{arm}/{rep_dir.name}: invalid JSON artifact: {exc}") from exc
        if arm == ARM_D:
            recs, meta = _ladder_records(arm, rep_dir.name, data)
        else:
            recs, meta = _batch_records(arm, rep_dir.name, data)
        records.extend(recs)
        metas.append(meta)
        input_sha[rep_dir.name] = hashlib.sha256(raw).hexdigest()
    return ArmBatch(
        arm=arm, records=tuple(records), dataset_sha256=_single(arm, metas, "dataset_sha256"),
        contract_id=_single(arm, metas, "contract_id"), harness_variant=_single(arm, metas, "harness_variant"),
        model=_single(arm, metas, "model"), global_max_cost_usd=float(_single(arm, metas, "global_max_cost_usd")),
        input_sha256=input_sha)


def analysis_code_sha256(path: Path | None = None) -> str:
    """SHA-256 deste arquivo com quebras de linha normalizadas para LF (igual no Windows e no WSL)."""
    source = Path(path) if path is not None else Path(__file__)
    return hashlib.sha256(source.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
