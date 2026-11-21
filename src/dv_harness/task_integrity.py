"""Validador determinístico de integridade de tarefas, oráculos e higiene de ambiente.

Gates inspirados nos controles auditados em ``kevin`` (verificação de gold antes de qualquer run) e
``codepro`` (quarentena de patch de referência), e nas regras de higiene de benchmark de Sun et al. (2026).
A citação não foi verificada nesta sessão: as regras abaixo são as do pedido de implementação, não uma
transcrição do artigo.

Para cada tarefa, tudo acontece em um diretório temporário próprio (``scratch_dir``). O dataset original
nunca é modificado. Verificações:

- a) higiene: sem ``.git`` na árvore exposta ao agente; sem marcadores de gabarito (``gold.patch``,
  ``solution.patch``, ``reference_patch``) em nomes ou conteúdos;
- b) isolamento do oráculo: o oráculo fica fora do workspace exposto, e nenhum arquivo dele referencia o
  caminho ou o texto do oráculo;
- c) gate pré-patch: no estado inicial o oráculo DEVE reprovar;
- d) gate pós-patch de referência: quando a tarefa declara ``reference_files``, o oráculo DEVE aprovar;
- e) impressões digitais SHA-256 determinísticas (caminho relativo ordenado por texto, bytes exatos).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dv_harness.batch import FILE_PATH_RE, ORACLE_FILE, BatchTask, verify

FAILING_SEVERITIES = ("critical", "error")
SOLUTION_MARKERS = ("gold.patch", "solution.patch", "reference_patch")
MIN_ORACLE_NEEDLE = 20  # trechos menores que isso geram falso positivo demais para procurar no workspace


@dataclass(frozen=True)
class TaskIntegrityViolation:
    code: str
    severity: str  # critical | error | info
    detail: str


@dataclass(frozen=True)
class TaskIntegrityReport:
    task_id: str
    passed: bool
    manifest_sha256: str
    workspace_sha256: str
    oracle_sha256: str
    violations: list[TaskIntegrityViolation] = field(default_factory=list)


@dataclass(frozen=True)
class BatchIntegrityReport:
    passed: bool
    dataset_sha256: str
    task_reports: list[TaskIntegrityReport]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _passed(violations: Sequence[TaskIntegrityViolation]) -> bool:
    return not any(v.severity in FAILING_SEVERITIES for v in violations)


def task_definition_sha256(task: BatchTask) -> str:
    """Hash da definição da tarefa (independe de formatação do arquivo de manifesto e do SO)."""
    canon = {
        "task_id": task.task_id, "family": task.family, "statement": task.statement, "files": task.files,
        "oracle": task.oracle, "reference_files": task.reference_files, "verifier_ref": task.verifier_ref,
        "repository": task.repository, "base_sha": task.base_sha,
    }
    return _sha256(json.dumps(canon, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _files_in(root: Path) -> list[tuple[str, Path]]:
    """Arquivos de ``root`` como (caminho relativo POSIX, caminho), ordenados pelo texto do caminho."""
    entries = [(path.relative_to(root).as_posix(), path) for path in root.rglob("*") if path.is_file()]
    return sorted(entries, key=lambda entry: entry[0])


def _tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for rel, path in _files_in(root):
        data = path.read_bytes()  # bytes exatos: CRLF e LF são preservados
        digest.update(rel.encode("utf-8") + b"\x00" + str(len(data)).encode("ascii") + b"\x00" + data)
    return digest.hexdigest()


def _inside(base: Path, target: Path) -> bool:
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError:
        return False
    return True


def _has_git_segment(rel: str) -> bool:
    return ".git" in rel.split("/")


def _git_and_solution_checks(snapshot: Path) -> list[TaskIntegrityViolation]:
    violations: list[TaskIntegrityViolation] = []
    for path in sorted(snapshot.rglob("*"), key=lambda p: p.relative_to(snapshot).as_posix()):
        rel = path.relative_to(snapshot).as_posix()
        if _has_git_segment(rel):
            violations.append(TaskIntegrityViolation(
                "GIT_LEAK_DETECTED", "critical", f"'.git' entry exposed to the agent: {rel}"))
        if any(marker in rel for marker in SOLUTION_MARKERS):
            violations.append(TaskIntegrityViolation(
                "SOLUTION_LEAK_DETECTED", "critical", f"solution-like file name exposed: {rel}"))
    for rel, path in _files_in(snapshot):
        text = path.read_bytes().decode("utf-8", errors="replace")
        for marker in SOLUTION_MARKERS:
            if marker in text:
                violations.append(TaskIntegrityViolation(
                    "SOLUTION_LEAK_DETECTED", "critical", f"'{marker}' appears inside exposed file {rel}"))
                break
    return violations


def _oracle_isolation_checks(snapshot: Path, oracle_path: Path, oracle_text: str) -> list[TaskIntegrityViolation]:
    violations: list[TaskIntegrityViolation] = []
    if _inside(snapshot, oracle_path):
        violations.append(TaskIntegrityViolation(
            "ORACLE_PATH_LEAK", "critical", "oracle file resides inside the exposed workspace"))
    needles = [str(oracle_path), oracle_path.parent.as_posix()]
    if len(oracle_text.strip()) >= MIN_ORACLE_NEEDLE:
        needles.append(oracle_text)
    for rel, path in _files_in(snapshot):
        text = path.read_bytes().decode("utf-8", errors="replace")
        if any(needle in text for needle in needles):
            violations.append(TaskIntegrityViolation(
                "ORACLE_PATH_LEAK", "critical", f"exposed file references the oracle: {rel}"))
    return violations


def _gold_checks(task: BatchTask, snapshot: Path, oracle_path: Path,
                 scratch_dir: Path) -> list[TaskIntegrityViolation]:
    if not task.reference_files:
        return [TaskIntegrityViolation("GOLD_PATCH_ABSENT", "info",
                                       "no reference patch declared: post-patch gate not applicable")]
    gold = scratch_dir / "gold"
    shutil.copytree(snapshot, gold)
    for name, content in task.reference_files.items():
        target = gold / name
        if not FILE_PATH_RE.fullmatch(name) or not _inside(gold, target):
            return [TaskIntegrityViolation("GOLD_PATCH_INVALID", "critical",
                                           f"reference file path is not a safe relative path: {name!r}")]
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        except OSError as exc:
            return [TaskIntegrityViolation("GOLD_PATCH_INVALID", "critical",
                                           f"reference patch could not be applied to {name}: {exc}")]
    verdict = verify(oracle_path, gold)
    if verdict != "PASS":
        return [TaskIntegrityViolation("GOLD_PATCH_FAILED", "critical",
                                       f"oracle did not approve the reference patch (verdict: {verdict})")]
    return []


def validate_task_integrity(task: BatchTask, *, scratch_dir: Path) -> TaskIntegrityReport:
    """Valida uma tarefa em ``scratch_dir`` (criado pelo chamador, exclusivo desta tarefa)."""
    scratch_dir = Path(scratch_dir)
    scratch_dir.mkdir(parents=True, exist_ok=False)
    snapshot = scratch_dir / "snapshot"
    for name, content in task.files.items():
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    oracle_path = scratch_dir / "oracle" / ORACLE_FILE  # fora do snapshot, como no lote real
    oracle_path.parent.mkdir(parents=True)
    oracle_path.write_bytes(task.oracle.encode("utf-8"))

    violations: list[TaskIntegrityViolation] = []
    violations += _git_and_solution_checks(snapshot)  # a
    violations += _oracle_isolation_checks(snapshot, oracle_path, task.oracle)  # b

    pre = scratch_dir / "pre"  # c: estado inicial, sem nenhuma alteração do agente
    shutil.copytree(snapshot, pre)
    pre_verdict = verify(oracle_path, pre)
    if pre_verdict == "PASS":
        violations.append(TaskIntegrityViolation(
            "PRE_PATCH_ORACLE_PASSED", "critical", "oracle approves the initial state: trivial or broken oracle"))
    elif pre_verdict is None:
        violations.append(TaskIntegrityViolation("ORACLE_TIMEOUT", "error", "oracle did not finish (pre-patch)"))

    violations += _gold_checks(task, snapshot, oracle_path, scratch_dir)  # d

    return TaskIntegrityReport(
        task_id=task.task_id,
        passed=_passed(violations),
        manifest_sha256=task_definition_sha256(task),
        workspace_sha256=_tree_sha256(snapshot),
        oracle_sha256=_sha256(task.oracle.encode("utf-8")),
        violations=violations,
    )


def _dataset_sha256(pairs: Iterable[tuple[str, str]]) -> str:
    """SHA-256 do dataset: pares (task_id, sha da definição), ordenados. Independe da ordem de entrada."""
    body = "\n".join(f"{task_id}\t{sha}" for task_id, sha in sorted(pairs))
    return _sha256(body.encode("utf-8"))


def dataset_fingerprint(tasks: Sequence[BatchTask]) -> str:
    """dataset_sha256 sem executar oráculos (v4, regra d). Mesmo valor que ``validate_batch_integrity``."""
    return _dataset_sha256((t.task_id, task_definition_sha256(t)) for t in tasks)


def validate_batch_integrity(tasks: Sequence[BatchTask], *, scratch_root: Path) -> BatchIntegrityReport:
    """Valida todas as tarefas. Cada uma tem um diretório temporário próprio dentro de ``scratch_root``."""
    if not tasks:
        raise ValueError("no tasks to validate")
    scratch_root = Path(scratch_root)
    scratch_root.mkdir(parents=True, exist_ok=True)
    reports: list[TaskIntegrityReport] = []
    seen: set[str] = set()
    for index, task in enumerate(tasks):
        report = validate_task_integrity(task, scratch_dir=scratch_root / f"{index:04d}-{task.task_id}")
        if task.task_id in seen:  # o segundo uso de um id é ambíguo para qualquer resultado
            extra = TaskIntegrityViolation("DUPLICATE_TASK_ID", "critical", f"task_id repeated: {task.task_id}")
            report = dataclasses.replace(report, passed=False, violations=[*report.violations, extra])
        seen.add(task.task_id)
        reports.append(report)
    dataset = _dataset_sha256((r.task_id, r.manifest_sha256) for r in reports)
    return BatchIntegrityReport(passed=all(r.passed for r in reports), dataset_sha256=dataset,
                                task_reports=reports)


def integrity_table(report: BatchIntegrityReport) -> str:
    """Tabela de verificação para o terminal: uma linha por tarefa e os códigos de violação."""
    lines = [f"{'TASK':<28} {'RESULT':<6} VIOLATIONS"]
    for task_report in report.task_reports:
        codes = ", ".join(f"{v.code}({v.severity})" for v in task_report.violations) or "-"
        result = "PASS" if task_report.passed else "FAIL"
        lines.append(f"{task_report.task_id:<28} {result:<6} {codes}")
        for violation in task_report.violations:
            if violation.severity in FAILING_SEVERITIES:
                lines.append(f"    {violation.code}: {violation.detail}")
    lines.append(f"dataset_sha256: {report.dataset_sha256}")
    lines.append(f"overall: {'PASS' if report.passed else 'FAIL'}")
    return "\n".join(lines)


def report_to_dict(report: BatchIntegrityReport) -> dict[str, Any]:
    return dataclasses.asdict(report)


def integrity_gate(tasks: Sequence[BatchTask]) -> BatchIntegrityReport:
    """Gate pré-lote: valida tudo em um diretório temporário descartável, sem tocar no diretório de saída."""
    with tempfile.TemporaryDirectory(prefix="dv-integrity-") as scratch:
        return validate_batch_integrity(tasks, scratch_root=Path(scratch))
