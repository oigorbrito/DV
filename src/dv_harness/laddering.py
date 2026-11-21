"""Naïve Laddering guiado por oráculo externo, sobre o substrato ``b0_plus`` (controlador puro).

Um degrau é uma tentativa completa da tarefa com um modelo e um esforço de raciocínio (``LadderRung``).
O controlador:

1. começa sempre pelo primeiro degrau (sem pré-roteamento);
2. prepara um workspace limpo a partir do snapshot da tarefa antes de cada degrau, e um diretório de saída
   completa isolado, fora do workspace;
3. limita o custo global: o teto de cada degrau é ``min(global * cost_share_cap, saldo restante)``;
4. decide parar ou escalar apenas pelo oráculo externo (``oracle_verifier``), nunca pelo que o agente declarou;
5. soma o custo e os tokens (limite inferior) de todos os degraus executados, inclusive os reprovados.

Não altera os contratos ``v1``-``v3``. ``state_carryover`` é fixo em ``clean_workspace`` nesta versão.
O executor de degrau é injetado: em produção é o adaptador de ``batch.py`` sobre o ``AgentLoop``; nos testes,
um mock determinístico.
"""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from dv_harness.variants import check_variant

LADDERING_SCHEMA = "dv-laddering-v1"
OUTCOME_SOLVED = "SOLVED"
OUTCOME_FAILURE = "FAILURE"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"
DEFAULT_ESCALATE_ON = ("failure", "max_steps_reached", "error_limit_reached", "provider_error")
NO_VERDICT_STATUSES = ("provider_error", "budget_unverifiable")  # sem veredito: INCONCLUSIVE no último degrau
CLEAN_WORKSPACE = "clean_workspace"
RUNG_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_RUNG_KEYS = frozenset({"rung_id", "model", "reasoning_effort", "harness_variant", "max_steps",
                        "cost_share_cap", "escalate_on_statuses"})
_POLICY_KEYS = frozenset({"policy_id", "global_max_cost_usd", "rungs", "state_carryover"})


class LadderPolicyError(ValueError):
    """Política de laddering inválida. Levantada antes de qualquer chamada ao provedor."""


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


@dataclass(frozen=True)
class LadderRung:
    rung_id: str
    model: str
    reasoning_effort: str | None = None
    harness_variant: str = "b0_plus"
    max_steps: int = 8
    cost_share_cap: float = 1.0
    escalate_on_statuses: tuple[str, ...] = DEFAULT_ESCALATE_ON

    def __post_init__(self) -> None:
        if not isinstance(self.rung_id, str) or not RUNG_ID_RE.fullmatch(self.rung_id):
            raise LadderPolicyError("rung_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
        if not isinstance(self.model, str) or not self.model.strip():
            raise LadderPolicyError(f"{self.rung_id}: model must be a non-empty string")
        if self.reasoning_effort is not None and (
                not isinstance(self.reasoning_effort, str) or not self.reasoning_effort.strip()):
            raise LadderPolicyError(f"{self.rung_id}: reasoning_effort must be null or a non-empty string")
        try:
            check_variant(self.harness_variant)
        except ValueError as exc:
            raise LadderPolicyError(f"{self.rung_id}: {exc}") from exc
        if isinstance(self.max_steps, bool) or not isinstance(self.max_steps, int) or self.max_steps < 1:
            raise LadderPolicyError(f"{self.rung_id}: max_steps must be an integer >= 1")
        if not _is_number(self.cost_share_cap) or not 0 < self.cost_share_cap <= 1:
            raise LadderPolicyError(f"{self.rung_id}: cost_share_cap must be a number in (0, 1]")
        statuses = tuple(self.escalate_on_statuses)
        if not all(isinstance(status, str) for status in statuses):
            raise LadderPolicyError(f"{self.rung_id}: escalate_on_statuses must be strings")
        object.__setattr__(self, "escalate_on_statuses", statuses)


@dataclass(frozen=True)
class LadderPolicy:
    policy_id: str
    global_max_cost_usd: float
    rungs: tuple[LadderRung, ...]
    state_carryover: str = CLEAN_WORKSPACE

    def __post_init__(self) -> None:
        if not isinstance(self.policy_id, str) or not self.policy_id.strip():
            raise LadderPolicyError("policy_id must be a non-empty string")
        if not _is_number(self.global_max_cost_usd) or self.global_max_cost_usd <= 0:
            raise LadderPolicyError("global_max_cost_usd must be a number > 0")
        rungs = tuple(self.rungs)
        if not rungs:
            raise LadderPolicyError("rungs must not be empty")
        if not all(isinstance(rung, LadderRung) for rung in rungs):
            raise LadderPolicyError("rungs must be LadderRung instances")
        ids = [rung.rung_id for rung in rungs]
        if len(set(ids)) != len(ids):
            raise LadderPolicyError("rung_id values must be unique")
        if self.state_carryover != CLEAN_WORKSPACE:
            raise LadderPolicyError(f"state_carryover must be {CLEAN_WORKSPACE!r} in this version")
        object.__setattr__(self, "rungs", rungs)

    @classmethod
    def from_dict(cls, data: Any) -> LadderPolicy:
        if not isinstance(data, Mapping):
            raise LadderPolicyError("policy must be a JSON object")
        unknown = set(data) - _POLICY_KEYS
        if unknown:
            raise LadderPolicyError(f"unknown policy keys: {sorted(unknown)}")
        raw_rungs = data.get("rungs")
        if not isinstance(raw_rungs, list):
            raise LadderPolicyError("rungs must be a list")
        rungs = []
        for entry in raw_rungs:
            if not isinstance(entry, Mapping):
                raise LadderPolicyError("each rung must be a JSON object")
            unknown_rung = set(entry) - _RUNG_KEYS
            if unknown_rung:
                raise LadderPolicyError(f"unknown rung keys: {sorted(unknown_rung)}")
            kwargs = dict(entry)
            if "escalate_on_statuses" in kwargs:
                kwargs["escalate_on_statuses"] = tuple(kwargs["escalate_on_statuses"])
            try:
                rungs.append(LadderRung(**kwargs))
            except TypeError as exc:  # campo obrigatório ausente
                raise LadderPolicyError(f"invalid rung: {exc}") from exc
        return cls(
            policy_id=data.get("policy_id"),  # type: ignore[arg-type]
            global_max_cost_usd=data.get("global_max_cost_usd"),  # type: ignore[arg-type]
            rungs=tuple(rungs),
            state_carryover=data.get("state_carryover", CLEAN_WORKSPACE),
        )

    @classmethod
    def from_json_file(cls, path: Path) -> LadderPolicy:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LadderPolicyError(f"policy file is not valid UTF-8 JSON: {exc}") from exc
        return cls.from_dict(data)


@dataclass(frozen=True)
class StepResult:
    """Resultado de um degrau, devolvido pelo executor. Campos ``None`` significam não medido."""

    status: str
    cost_lower_bound_usd: float | None = None
    tokens_lower_bound: int | None = None
    wall_clock_ms: int = 0


@dataclass(frozen=True)
class RungTrace:
    rung_id: str
    model: str
    reasoning_effort: str | None
    status: str
    rung_status: str
    oracle_pass: bool
    cap_usd: float
    cost_lower_bound_usd: float | None
    tokens_lower_bound: int | None
    wall_clock_ms: int


@dataclass(frozen=True)
class LadderingResult:
    task_id: str
    policy_id: str
    final_outcome: str
    resolved_at_rung: str | None
    escalation_count: int
    total_cost_lower_bound_usd: float | None
    total_tokens_lower_bound: int | None
    total_wall_clock_ms: int
    rung_traces: list[RungTrace]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema"] = LADDERING_SCHEMA
        return data


StepRunner = Callable[[LadderRung, Path, Path, float], StepResult]
OracleVerifier = Callable[[Path], bool]


class LadderController:
    """Executa uma política de laddering sobre uma tarefa. Não guarda estado entre tarefas."""

    def __init__(self, policy: LadderPolicy, *, step_runner: StepRunner, oracle_verifier: OracleVerifier,
                 snapshot_dir: Path, work_root: Path) -> None:
        self.policy = policy
        self.step_runner = step_runner
        self.oracle_verifier = oracle_verifier
        self.snapshot_dir = Path(snapshot_dir)
        self.work_root = Path(work_root)
        if not self.snapshot_dir.is_dir():
            raise LadderPolicyError(f"snapshot_dir is not a directory: {self.snapshot_dir}")
        self.work_root.mkdir(parents=True, exist_ok=True)

    def run(self, task_id: str) -> LadderingResult:
        policy = self.policy
        traces: list[RungTrace] = []
        spent: float = 0.0
        cost_total: float | None = None
        tokens_total: int | None = None
        wall_total = 0
        escalations = 0
        outcome = OUTCOME_FAILURE
        resolved: str | None = None
        for index, rung in enumerate(policy.rungs):
            remaining = policy.global_max_cost_usd - spent  # Invariante 3: saldo global
            if remaining <= 0:
                outcome = OUTCOME_FAILURE  # o último veredito do oráculo já foi reprovação
                break
            if index > 0:
                escalations += 1
            cap = min(policy.global_max_cost_usd * rung.cost_share_cap, remaining)
            workspace = self._clean_workspace()  # Invariante 2: nunca reaproveita patch de degrau reprovado
            bash_logs = self._bash_logs(index, rung)
            step = self.step_runner(rung, workspace, bash_logs, cap)
            oracle_pass = bool(self.oracle_verifier(workspace))  # Invariante 4: veredito externo
            # Agente que declarou sucesso e foi reprovado pelo oráculo é um falso positivo: "failure".
            rung_status = "failure" if step.status == "completed" and not oracle_pass else step.status
            traces.append(RungTrace(
                rung_id=rung.rung_id, model=rung.model, reasoning_effort=rung.reasoning_effort,
                status=step.status, rung_status=rung_status, oracle_pass=oracle_pass, cap_usd=cap,
                cost_lower_bound_usd=step.cost_lower_bound_usd, tokens_lower_bound=step.tokens_lower_bound,
                wall_clock_ms=step.wall_clock_ms,
            ))
            if step.cost_lower_bound_usd is not None:  # Invariante 6: soma todos os degraus executados
                spent += step.cost_lower_bound_usd
                cost_total = (cost_total or 0.0) + step.cost_lower_bound_usd
            if step.tokens_lower_bound is not None:
                tokens_total = (tokens_total or 0) + step.tokens_lower_bound
            wall_total += step.wall_clock_ms
            if oracle_pass:  # Invariante 4: parada imediata pelo oráculo
                outcome, resolved = OUTCOME_SOLVED, rung.rung_id
                break
            if index == len(policy.rungs) - 1:  # Invariante 5: sem degrau superior
                outcome = OUTCOME_INCONCLUSIVE if rung_status in NO_VERDICT_STATUSES else OUTCOME_FAILURE
                break
            if rung_status not in rung.escalate_on_statuses:  # Invariante 5: status não escalável
                outcome = OUTCOME_FAILURE
                break
        return LadderingResult(
            task_id=task_id, policy_id=policy.policy_id, final_outcome=outcome, resolved_at_rung=resolved,
            escalation_count=escalations, total_cost_lower_bound_usd=cost_total,
            total_tokens_lower_bound=tokens_total, total_wall_clock_ms=wall_total, rung_traces=traces,
        )

    def _clean_workspace(self) -> Path:
        workspace = self.work_root / "workspace"
        if workspace.exists():
            shutil.rmtree(workspace)  # o workspace do degrau anterior sai inteiro
        shutil.copytree(self.snapshot_dir, workspace)
        return workspace

    def _bash_logs(self, index: int, rung: LadderRung) -> Path:
        path = self.work_root / "rungs" / f"rung-{index:02d}-{rung.rung_id}" / "bash_logs"
        path.mkdir(parents=True, exist_ok=True)
        return path
