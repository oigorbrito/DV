"""Lote multi-tarefa sobre o ``AgentLoop``, sob o contrato ``p1-run-accounting-v3``.

Cada tarefa roda em um subdiretório próprio ``<outdir>/runs/<task_id>/`` com:

- ``workspace/``: o único diretório que o executor enxerga (raiz do ``ToolExecutor``);
- ``oracle/check.py``: o verificador da tarefa, **fora do workspace**. O agente não consegue
  editá-lo, então não consegue forjar ``PASS``. O veredito é dado pelo harness, rodando o
  oráculo com ``PYTHONPATH`` apontando para o workspace e um ambiente mínimo (sem chaves);
- ``ledger.jsonl`` (eventos ``call_started``/``call_finished`` com fsync) e ``result.json``.

Depois de cada tarefa, a varredura anti-vazamento procura o valor da chave em todo o
subdiretório da tarefa. Se achar, o lote para imediatamente e sai com código 1.

Resumo (``batch-summary.json``, ``batch-ledger.jsonl``):

- Totais são ``None`` quando qualquer parcela é desconhecida, com entrada em ``unmeasured``
  explicando o motivo. Nunca ``0`` como sentinela (invariante I6, contrato v3).
- ``pass_rate = PASS / (PASS + FAIL)``. ``solved_rate = SOLVED / N``. Sem denominador, ``None``.
- Não há score composto (``no_composite_score``).

Limitação: o lote não distingue, por si, uma falha de provedor de uma falha de rede dentro
do container. Isso fica no ``ledger`` (``provider_error``) e nas flags de infraestrutura.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dv_harness import protocol
from dv_harness.agent_loop import AgentLoop
from dv_harness.context_manager import ContextManager
from dv_harness.ledger import RunLedger, reconcile
from dv_harness.tool_executor import ToolExecutor

BATCH_SCHEMA = "dv-batch-summary-v1"
MANIFEST_SCHEMA = "dv-batch-manifest-v1"
SUITE_SCREENING_N10 = "screening-n10"
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
FILE_NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")
KEY_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
ORACLE_FILE = "check.py"
VERIFY_TIMEOUT_S = 30
ERROR_MAX_CHARS = 300
STATUS_HARNESS_ERROR = "harness_error"  # bug do harness numa tarefa; registrado, nunca silenciado

SYSTEM_PROMPT = (
    "You are a careful coding agent working in a workspace directory. You act only with the tools "
    "provided. Finish by calling submit(answer)."
)


class ManifestError(ValueError):
    """Manifesto inválido ou não executável. Levantado antes de qualquer chamada ao provedor."""


@dataclass(frozen=True)
class BatchTask:
    task_id: str
    statement: str
    files: dict[str, str]  # visíveis ao executor (dentro do workspace)
    oracle: str  # verificador: escrito FORA do workspace


@dataclass(frozen=True)
class BatchConfig:
    model: str
    request_timeout_s: float
    max_steps: int
    max_cost_usd: float | None
    continue_on_error: bool
    suite_id: str
    manifest_sha256: str
    git_head: str | None
    api_key_env: str

    def describe(self) -> dict[str, Any]:
        return {
            "contract_id": protocol.CONTRACT_ID,
            "model_requested": self.model,
            "request_timeout_s": self.request_timeout_s,
            "max_steps": self.max_steps,
            "max_cost_usd": self.max_cost_usd,
            "continue_on_error": self.continue_on_error,
            "suite_id": self.suite_id,
            "manifest_sha256": self.manifest_sha256,
            "git_head": self.git_head,
            "api_key_env": self.api_key_env,  # o NOME da variável, nunca o valor
        }


# --- suíte embutida: 10 tarefas determinísticas (screening-n10) -------------------------
# Cada tupla: (task_id, módulo, função, corpo com bug, asserts do oráculo).
_SUITE_SPECS: tuple[tuple[str, str, str, str, tuple[str, ...]], ...] = (
    ("sw01-add", "calc", "add", "def add(a, b):\n    return a - b\n",
     ("add(2, 3) == 5", "add(-1, 1) == 0")),
    ("sw02-sub", "calc", "sub", "def sub(a, b):\n    return a + b\n",
     ("sub(5, 3) == 2", "sub(0, 4) == -4")),
    ("sw03-mul", "calc", "mul", "def mul(a, b):\n    return a + b\n",
     ("mul(3, 4) == 12", "mul(-2, 5) == -10")),
    ("sw04-is-even", "parity", "is_even", "def is_even(n):\n    return n % 2 == 1\n",
     ("is_even(4) is True", "is_even(7) is False", "is_even(0) is True")),
    ("sw05-max2", "order", "max2", "def max2(a, b):\n    return a if a < b else b\n",
     ("max2(2, 9) == 9", "max2(-3, -1) == -1")),
    ("sw06-count-a", "text", "count_a", "def count_a(s):\n    return sum(1 for c in s if c == 'b')\n",
     ("count_a('banana') == 3", "count_a('xyz') == 0")),
    ("sw07-reverse", "text", "reverse", "def reverse(s):\n    return s\n",
     ("reverse('abc') == 'cba'", "reverse('') == ''")),
    ("sw08-fizzbuzz", "games", "fizzbuzz",
     "def fizzbuzz(n):\n    if n % 3 == 0:\n        return 'Fizz'\n    if n % 5 == 0:\n        return 'Buzz'\n"
     "    return str(n)\n",
     ("fizzbuzz(15) == 'FizzBuzz'", "fizzbuzz(9) == 'Fizz'", "fizzbuzz(10) == 'Buzz'", "fizzbuzz(7) == '7'")),
    ("sw09-abs", "numbers", "absval", "def absval(x):\n    return x if x > 0 else x\n",
     ("absval(-4) == 4", "absval(3) == 3", "absval(0) == 0")),
    ("sw10-last", "lists", "last", "def last(xs):\n    return xs[0]\n",
     ("last([1, 2, 3]) == 3", "last([9]) == 9")),
)


def _statement(module: str, fn: str) -> str:
    return (
        f"The file {module}.py contains a function {fn} with a bug. Fix it so that the hidden verification "
        "passes. Use the tools to read, edit and run. When the fix is done, call submit with a one-sentence "
        "answer describing the change."
    )


def _oracle(module: str, fn: str, asserts: Sequence[str]) -> str:
    body = "".join(f"assert {a}\n" for a in asserts)
    return f"from {module} import {fn}\n\n{body}print('OK')\n"


def builtin_suite(suite_id: str = SUITE_SCREENING_N10) -> list[BatchTask]:
    if suite_id != SUITE_SCREENING_N10:
        raise ManifestError(f"unknown suite: {suite_id!r}")
    return [
        BatchTask(task_id=tid, statement=_statement(mod, fn), files={f"{mod}.py": bug},
                  oracle=_oracle(mod, fn, asserts))
        for tid, mod, fn, bug, asserts in _SUITE_SPECS
    ]


def suite_sha256(tasks: Sequence[BatchTask]) -> str:
    canon = json.dumps([[t.task_id, t.statement, t.files, t.oracle] for t in tasks],
                       ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# --- manifesto ------------------------------------------------------------------------


def load_manifest(path: Path) -> tuple[str, list[BatchTask], str]:
    """Lê um manifesto ``dv-batch-manifest-v1``. Devolve ``(suite_id, tarefas, sha256_do_arquivo)``."""
    raw = path.read_bytes()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManifestError(f"manifest is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest must be a JSON object")
    schema = str(data.get("schema_id", ""))
    if "candidates" in data or schema.startswith("ndv-p1-s2-candidate-wave"):
        raise ManifestError(
            "S2 candidate wave is discovery-only (admitted_count=0, treatment_execution NOT_EXECUTED, "
            "verifier UNRESOLVED): it has no executable workspace or oracle and cannot be run"
        )
    if schema != MANIFEST_SCHEMA:
        raise ManifestError(f"schema_id must be {MANIFEST_SCHEMA!r}")
    suite_id = data.get("suite_id")
    if not isinstance(suite_id, str) or not suite_id:
        raise ManifestError("suite_id must be a non-empty string")
    entries = data.get("tasks")
    if not isinstance(entries, list) or not entries:
        raise ManifestError("tasks must be a non-empty list")
    tasks: list[BatchTask] = []
    seen: set[str] = set()
    for entry in entries:
        tasks.append(_task_from_entry(entry, seen))
    return suite_id, tasks, hashlib.sha256(raw).hexdigest()


def _task_from_entry(entry: Any, seen: set[str]) -> BatchTask:
    if not isinstance(entry, dict):
        raise ManifestError("each task must be a JSON object")
    tid = entry.get("task_id")
    if not isinstance(tid, str) or not TASK_ID_RE.fullmatch(tid):
        raise ManifestError("task_id must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
    if tid in seen:
        raise ManifestError(f"duplicate task_id: {tid}")
    seen.add(tid)
    statement = entry.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise ManifestError(f"{tid}: statement must be a non-empty string")
    files = entry.get("files")
    if not isinstance(files, dict) or not files:
        raise ManifestError(f"{tid}: files must be a non-empty object")
    for name, content in files.items():
        if not isinstance(name, str) or not FILE_NAME_RE.fullmatch(name) or not isinstance(content, str):
            raise ManifestError(f"{tid}: invalid workspace file entry")
    oracle = entry.get("oracle")
    if not isinstance(oracle, str) or not oracle.strip():
        raise ManifestError(f"{tid}: oracle must be a non-empty string")
    return BatchTask(task_id=tid, statement=statement, files=dict(files), oracle=oracle)


# --- verificação independente ---------------------------------------------------------


def verify(oracle_path: Path, workspace: Path) -> str | None:
    """Roda o oráculo fora do loop. ``PASS``/``FAIL``, ou ``None`` se não concluiu (timeout)."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(workspace), "PYTHONDONTWRITEBYTECODE": "1"}
    if "SYSTEMROOT" in os.environ:  # Windows precisa disto para o interpretador iniciar
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    try:
        proc = subprocess.run([sys.executable, str(oracle_path)], cwd=workspace, env=env,
                              capture_output=True, text=True, timeout=VERIFY_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return None
    return protocol.VERDICT_PASS if proc.returncode == 0 else protocol.VERDICT_FAIL


# --- varredura anti-vazamento ---------------------------------------------------------


def scan_for_secret(root: Path, secret: bytes) -> list[str]:
    """Caminhos (relativos a ``root``) de arquivos que contêm o valor do segredo. Nunca ecoa o valor."""
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and secret in path.read_bytes():
            hits.append(path.relative_to(root).as_posix())
    return hits


# --- uma tarefa -----------------------------------------------------------------------


def _truncate(text: str) -> str:
    return text if len(text) <= ERROR_MAX_CHARS else text[:ERROR_MAX_CHARS] + "..."


def run_one(task: BatchTask, run_dir: Path, llm: Any, config: BatchConfig) -> dict[str, Any]:
    """Executa uma tarefa em ``run_dir`` e devolve o relatório (também gravado em ``result.json``)."""
    workspace = run_dir / "workspace"
    oracle_dir = run_dir / "oracle"
    workspace.mkdir(parents=True)
    oracle_dir.mkdir()
    for name, content in task.files.items():
        (workspace / name).write_text(content, encoding="utf-8")
    oracle_path = oracle_dir / ORACLE_FILE
    oracle_path.write_text(task.oracle, encoding="utf-8")

    with RunLedger(run_dir / "ledger.jsonl") as ledger:
        loop = AgentLoop(llm, ToolExecutor(workspace), ContextManager(keep_recent_observations=10), ledger=ledger)
        result = loop.run(task.statement, SYSTEM_PROMPT, max_steps=config.max_steps,
                          max_cost_usd=config.max_cost_usd, run_id=task.task_id)

    verdict = verify(oracle_path, workspace)
    outcome = protocol.outcome_for(result.status, verdict)
    oracle_sha = hashlib.sha256(task.oracle.encode("utf-8")).hexdigest()
    report: dict[str, Any] = {
        "task_id": task.task_id,
        "contract_id": protocol.CONTRACT_ID,
        "model_requested": config.model,
        "status": result.status,
        "verifier_verdict": verdict,
        "outcome": outcome,
        "oracle_sha256": oracle_sha,
        "final_answer": result.final_answer,
        "steps": result.steps,
        "total_tokens": result.total_tokens,
        "tokens_lower_bound": result.tokens_lower_bound,
        "total_prompt_tokens": result.total_prompt_tokens,
        "total_completion_tokens": result.total_completion_tokens,
        "total_cache_read_tokens": result.total_cache_read_tokens,
        "total_cache_write_tokens": result.total_cache_write_tokens,
        "total_reasoning_tokens": result.total_reasoning_tokens,
        "total_output_tokens": result.total_output_tokens,
        "total_cost_usd": result.total_cost_usd,
        "cost_basis": result.cost_basis,
        "failed_attempt_count": result.failed_attempt_count,
        "total_wall_clock_ms": result.total_wall_clock_ms,
        "llm_wall_clock_ms": result.llm_wall_clock_ms,
        "tool_wall_clock_ms": result.tool_wall_clock_ms,
        "protocol_errors": result.protocol_errors,
        "domain_failures": result.domain_failures,
        "cmd_timeouts": result.cmd_timeouts,
        "calls": [
            {"index": c.index, "outcome": c.outcome, "model_returned": c.model_returned, "provider": c.provider,
             "api_id": c.api_id, "total_tokens": c.total_tokens, "cost_usd": c.cost_usd, "cost_basis": c.cost_basis,
             "wall_clock_ms": c.wall_clock_ms, "attempts": len(c.attempts)}
            for c in result.calls
        ],
        "tool_events": [
            {"index": e.index, "call_index": e.call_index, "name": e.name, "ok": e.ok, "format_error": e.format_error,
             "raw_sha256": e.raw_sha256, "raw_len": len(e.raw_output), "truncated": e.truncated,
             "wall_clock_ms": e.wall_clock_ms}
            for e in result.tool_events
        ],
        "unmeasured": dict(result.unmeasured),
    }
    (run_dir / "result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def harness_error_report(task: BatchTask, run_dir: Path, exc: BaseException) -> dict[str, Any]:
    """Relatório de uma tarefa que quebrou por bug do harness. Campos não medidos ficam ``None`` com motivo."""
    reason = "run aborted by a harness error before completion (see error)"
    in_flight: list[int] = []
    ledger_path = run_dir / "ledger.jsonl"
    if ledger_path.exists():
        try:
            in_flight = reconcile(ledger_path)["in_flight_call_indices"]
        except ValueError:
            in_flight = []
    report: dict[str, Any] = {
        "task_id": task.task_id,
        "contract_id": protocol.CONTRACT_ID,
        "status": STATUS_HARNESS_ERROR,
        "verifier_verdict": None,
        "outcome": protocol.OUTCOME_INCONCLUSIVE,
        "steps": 0,
        "total_tokens": None,
        "tokens_lower_bound": None,
        "total_cost_usd": None,
        "cost_basis": protocol.COST_BASIS_UNKNOWN,
        "failed_attempt_count": None,
        "total_wall_clock_ms": None,
        "error": f"{type(exc).__name__}: {_truncate(str(exc))}",
        "calls": [],
        "tool_events": [],
        "unmeasured": {
            "total_tokens": reason,
            "tokens_lower_bound": reason,
            "total_cost_usd": reason,
            "failed_attempt_count": reason,
            "total_wall_clock_ms": reason,
            "verifier_verdict": "verification not reached",
            **({"in_flight_calls": f"calls {in_flight} started and not finished: consumption unknown"}
               if in_flight else {}),
        },
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def _brief(report: dict[str, Any], leak_clean: bool) -> dict[str, Any]:
    return {
        "task_id": report["task_id"],
        "status": report["status"],
        "verifier_verdict": report["verifier_verdict"],
        "outcome": report["outcome"],
        "steps": report["steps"],
        "total_tokens": report["total_tokens"],
        "tokens_lower_bound": report["tokens_lower_bound"],
        "cost_usd": report["total_cost_usd"],
        "cost_basis": report["cost_basis"],
        "wall_clock_ms": report["total_wall_clock_ms"],
        "failed_attempt_count": report["failed_attempt_count"],
        "leak_clean": leak_clean,
        "result_json": f"runs/{report['task_id']}/result.json",
    }


# --- resumo consolidado ---------------------------------------------------------------

NULLABLE_SUMMARY_FIELDS = (
    "total_tokens", "tokens_lower_bound", "total_cost_usd", "total_wall_clock_ms",
    "pass_rate", "solved_rate", "false_finish_rate", "core_tokens_partial_per_solved", "git_head",
)


def _sum_or_none(runs: list[dict[str, Any]], key: str, reason: str, unmeasured: dict[str, str]) -> Any:
    values = [r[key] for r in runs]
    if not values or any(v is None for v in values):
        unmeasured[key] = reason
        return None
    return sum(values)


def _rates(runs: list[dict[str, Any]], verdicts: Counter, outcomes: Counter,
           unmeasured: dict[str, str]) -> dict[str, Any]:
    n = len(runs)
    pass_n, fail_n = verdicts.get(protocol.VERDICT_PASS, 0), verdicts.get(protocol.VERDICT_FAIL, 0)
    rates: dict[str, Any] = {
        "pass_rate": pass_n / (pass_n + fail_n) if pass_n + fail_n else None,
        "solved_rate": outcomes.get(protocol.OUTCOME_SOLVED, 0) / n if n else None,
        "false_finish_rate": protocol.false_finish_rate([(r["status"], r["outcome"]) for r in runs]),
    }
    reasons = {
        "pass_rate": "no run has a verifier verdict (PASS or FAIL)",
        "solved_rate": "no runs executed",
        "false_finish_rate": "no completed run with a SOLVED or FAILURE outcome",
    }
    for key, reason in reasons.items():
        if rates[key] is None:
            unmeasured[key] = reason
    return rates


def summarize(runs: list[dict[str, Any]], *, planned: int, config: BatchConfig, aborted: str | None) -> dict[str, Any]:
    n = len(runs)
    outcomes = Counter(r["outcome"] for r in runs)
    verdicts = Counter((r["verifier_verdict"] or "NO_VERDICT") for r in runs)
    statuses = Counter(r["status"] for r in runs)
    unmeasured: dict[str, str] = {}

    total_tokens = _sum_or_none(runs, "total_tokens",
                                "one or more runs have incomplete token usage (contract v3, D5)", unmeasured)
    lb_values = [r["tokens_lower_bound"] for r in runs if r["tokens_lower_bound"] is not None]
    tokens_lb = sum(lb_values) if lb_values else None
    if tokens_lb is None:
        unmeasured["tokens_lower_bound"] = "no run returned measured usage"
    total_cost = _sum_or_none(runs, "total_cost_usd",
                              "one or more calls have unknown price (cost_basis unknown)", unmeasured)
    total_wall = _sum_or_none(runs, "total_wall_clock_ms",
                              "one or more runs did not measure wall clock (harness error)", unmeasured)
    rates = _rates(runs, verdicts, outcomes, unmeasured)
    solved = outcomes.get(protocol.OUTCOME_SOLVED, 0)
    per_solved = total_tokens / solved if total_tokens is not None and solved > 0 else None
    if per_solved is None:
        unmeasured["core_tokens_partial_per_solved"] = (
            "requires all runs with measured usage and at least one SOLVED task")
    if config.git_head is None:
        unmeasured["git_head"] = "not provided (--git-head) and git unavailable in this environment"

    summary: dict[str, Any] = {
        "schema": BATCH_SCHEMA,
        "contract_id": protocol.CONTRACT_ID,
        "metric": "CORE_TOKENS_PARTIAL",
        "no_composite_score": True,
        "git_head": config.git_head,
        "model_requested": config.model,
        "request_timeout_s": config.request_timeout_s,
        "max_steps": config.max_steps,
        "max_cost_usd": config.max_cost_usd,
        "continue_on_error": config.continue_on_error,
        "suite_id": config.suite_id,
        "manifest_sha256": config.manifest_sha256,
        "N": planned,
        "executed": n,
        "aborted": aborted,
        "outcome_counts": {k: outcomes.get(k, 0) for k in (protocol.OUTCOME_SOLVED, protocol.OUTCOME_FAILURE,
                                                           protocol.OUTCOME_INCONCLUSIVE)},
        "verifier_verdict_counts": {k: verdicts.get(k, 0) for k in (protocol.VERDICT_PASS, protocol.VERDICT_FAIL,
                                                                    "NO_VERDICT")},
        "status_counts": dict(sorted(statuses.items())),
        "total_steps": sum(r["steps"] for r in runs),
        "total_tokens": total_tokens,
        "tokens_lower_bound": tokens_lb,
        "total_cost_usd": total_cost,
        "cost_basis": protocol.run_cost_basis([r["cost_basis"] for r in runs]),
        "total_wall_clock_ms": total_wall,
        **rates,
        "core_tokens_partial_per_solved": per_solved,
        "runs": [_brief(r, r.get("leak_clean", False)) for r in runs],
        "unmeasured": unmeasured,
    }
    for field_name in NULLABLE_SUMMARY_FIELDS:
        if summary.get(field_name) is None and field_name not in unmeasured:
            raise AssertionError(f"null summary field without unmeasured reason: {field_name}")
    return summary


# --- lote -----------------------------------------------------------------------------

LlmFactory = Callable[[BatchTask], Any]


def run_batch(tasks: Sequence[BatchTask], outdir: Path, *, config: BatchConfig, make_llm: LlmFactory,
              secret: str | None) -> dict[str, Any]:
    """Executa ``tasks`` em ordem. Devolve o resumo (também gravado em ``batch-summary.json``).

    ``secret``: valor da chave (só em memória). Não é gravado. Se ``None``, a varredura é pulada e o
    chamador deve ter provado a ausência de vazamento por outro meio.
    """
    if secret is not None and not secret:
        raise ValueError("secret must be non-empty when given")
    outdir.mkdir(parents=True, exist_ok=True)
    runs_dir = outdir / "runs"
    runs_dir.mkdir(exist_ok=True)
    secret_bytes = secret.encode("utf-8") if secret else None
    runs: list[dict[str, Any]] = []
    aborted: str | None = None

    with RunLedger(outdir / "batch-ledger.jsonl") as bl:
        bl.append("batch_started", {"config": config.describe(), "task_ids": [t.task_id for t in tasks],
                                    "N": len(tasks)})
        for task in tasks:
            run_dir = runs_dir / task.task_id
            if run_dir.exists():
                raise FileExistsError(f"run directory already exists (use a clean --outdir): {task.task_id}")
            try:
                report = run_one(task, run_dir, make_llm(task), config)
            except Exception as exc:  # noqa: BLE001 - bug do harness; registrado por tarefa, não silenciado
                if not config.continue_on_error:
                    raise
                report = harness_error_report(task, run_dir, exc)
            leaks = scan_for_secret(run_dir, secret_bytes) if secret_bytes else []
            report["leak_clean"] = not leaks
            runs.append(report)
            bl.append("batch_run_finished", {"run": _brief(report, not leaks)})
            if leaks:
                aborted = "secret_leak"  # caminhos apenas; o valor nunca é impresso
                break
            if not config.continue_on_error and report["outcome"] != protocol.OUTCOME_SOLVED:
                aborted = f"stopped_on_{report['outcome']}"
                break

        summary = summarize(runs, planned=len(tasks), config=config, aborted=aborted)
        bl.append("batch_finished", {"summary": {k: summary[k] for k in (
            "N", "executed", "aborted", "outcome_counts", "verifier_verdict_counts", "total_tokens",
            "total_cost_usd", "pass_rate")}})

    batch_leaks = scan_for_secret(outdir, secret_bytes) if secret_bytes else []
    summary["leak_scan"] = {"performed": secret_bytes is not None,
                            "clean": (not batch_leaks) if secret_bytes else None,
                            "hit_paths": batch_leaks}
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if secret and secret in text:  # defesa final: o resumo nunca pode conter o segredo
        raise RuntimeError("secret found in batch summary; summary not written")
    (outdir / "batch-summary.json").write_text(text, encoding="utf-8")
    return summary


def resolve_git_head(repo: Path) -> str | None:
    """HEAD do repositório, ou ``None`` se o git não estiver disponível (ex.: dentro do container)."""
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    head = proc.stdout.strip()
    return head if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", head) else None


def select(tasks: list[BatchTask], limit: int | None) -> list[BatchTask]:
    if limit is None:
        return tasks
    if limit < 1:
        raise ValueError("--limit must be >= 1")
    return tasks[:limit]


__all__ = [
    "BATCH_SCHEMA", "BatchConfig", "BatchTask", "ManifestError", "SUITE_SCREENING_N10", "builtin_suite",
    "harness_error_report", "load_manifest", "resolve_git_head", "run_batch", "run_one", "scan_for_secret",
    "select", "summarize", "suite_sha256", "verify",
]
