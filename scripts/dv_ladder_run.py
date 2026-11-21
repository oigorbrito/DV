"""Naïve Laddering guiado por oráculo externo, em lote (controlador em ``src/dv_harness/laddering.py``).

Uso, fora do repositório, com diretório de saída inexistente ou vazio:
    python scripts/dv_ladder_run.py --policy <policy.json> --tasks-dir <dir-com-manifestos> --output-dir <dir-limpo>

Cada ``*.json`` em ``--tasks-dir`` é um manifesto ``dv-batch-manifest-v1`` (uma ou mais tarefas). Para cada
tarefa, o snapshot e o oráculo ficam fora do workspace; o controlador roda os degraus da política e grava
``<output-dir>/<task_id>/laddering-summary.json``. O agregado do lote vai para
``<output-dir>/laddering-batch-summary.json``.

Gate (saída 2, nada executado): política inválida, ``--tasks-dir`` sem manifestos, ``--output-dir`` não vazio,
nome de variável de chave inválido, variável ausente ou vazia (o valor nunca é impresso), ou
``--request-timeout-s`` não positivo.
Saída: 0 concluído.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

KEY_VAR = "ANTHROPIC_API_KEY"  # padrão igual ao das outras CLIs; --api-key-env troca
REQUEST_TIMEOUT_S = 30.0
KEY_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--policy", type=Path, required=True, help="arquivo JSON de LadderPolicy")
    parser.add_argument("--tasks-dir", type=Path, required=True, help="diretório com manifestos *.json")
    parser.add_argument("--output-dir", type=Path, required=True, help="diretório de saída, limpo ou inexistente")
    parser.add_argument("--api-key-env", default=KEY_VAR, metavar="NAME",
                        help=f"nome da variável com a chave; padrão: {KEY_VAR}")
    parser.add_argument("--request-timeout-s", type=float, default=REQUEST_TIMEOUT_S, metavar="S",
                        help=f"timeout por requisição HTTP; padrão: {REQUEST_TIMEOUT_S}")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="executa só as N primeiras tarefas")
    parser.add_argument("--validate-tasks", action=argparse.BooleanOptionalAction, default=True,
                        help="padrão ligado: valida a integridade das tarefas antes de qualquer chamada de modelo")
    return parser


def _gate(args: argparse.Namespace, policy_error: type[Exception]) -> tuple[Any, list[Path], str | None]:
    """Valida tudo antes de qualquer execução. Devolve ``(policy, manifestos, erro)``. Nunca ecoa chaves."""
    from dv_harness.laddering import LadderPolicy

    if not KEY_NAME_RE.fullmatch(args.api_key_env):
        return None, [], "--api-key-env must be a variable NAME matching [A-Z_][A-Z0-9_]*"
    if not (args.request_timeout_s > 0) or math.isnan(args.request_timeout_s):
        return None, [], "--request-timeout-s must be > 0"
    if args.limit is not None and args.limit < 1:
        return None, [], "--limit must be >= 1"
    try:
        policy = LadderPolicy.from_json_file(args.policy)
    except policy_error as exc:
        return None, [], f"invalid policy: {exc}"
    if not args.tasks_dir.is_dir():
        return None, [], f"--tasks-dir is not a directory: {args.tasks_dir}"
    manifests = sorted(args.tasks_dir.glob("*.json"))
    if not manifests:
        return None, [], f"--tasks-dir has no *.json manifests: {args.tasks_dir}"
    if args.output_dir.exists() and any(args.output_dir.iterdir()):
        return None, [], f"--output-dir {args.output_dir} is not empty; use a clean directory"
    if not os.environ.get(args.api_key_env):
        return None, [], f"{args.api_key_env} is not set (or empty) in this environment"
    return policy, manifests, None


def _aggregate(results: list[Any], policy: Any, policy_sha256: str) -> dict[str, Any]:
    outcomes = Counter(result.final_outcome for result in results)
    resolved = Counter(result.resolved_at_rung for result in results if result.resolved_at_rung)
    costs = [r.total_cost_lower_bound_usd for r in results if r.total_cost_lower_bound_usd is not None]
    tokens = [r.total_tokens_lower_bound for r in results if r.total_tokens_lower_bound is not None]
    return {
        "schema": "dv-laddering-batch-v1",
        "policy_id": policy.policy_id,
        "policy_sha256": policy_sha256,
        "tasks": len(results),
        "outcome_counts": {k: outcomes.get(k, 0) for k in ("SOLVED", "FAILURE", "INCONCLUSIVE")},
        "resolved_at_rung_counts": dict(sorted(resolved.items())),
        "escalation_total": sum(r.escalation_count for r in results),
        "escalated_tasks": sum(1 for r in results if r.escalation_count > 0),
        # Limites inferiores: soma só o que foi medido. None quando nenhum degrau mediu o valor.
        "total_cost_lower_bound_usd": sum(costs) if costs else None,
        "total_tokens_lower_bound": sum(tokens) if tokens else None,
        "total_wall_clock_ms": sum(r.total_wall_clock_ms for r in results),
        "tasks_detail": [
            {"task_id": r.task_id, "final_outcome": r.final_outcome, "resolved_at_rung": r.resolved_at_rung,
             "escalation_count": r.escalation_count}
            for r in results
        ],
    }


def _run_task(task: Any, policy: Any, make_llm: Any, config: Any, output_dir: Path) -> Any:
    from dv_harness import batch
    from dv_harness.laddering import LadderController

    task_dir = output_dir / task.task_id
    work_root = task_dir / "work"
    snapshot = work_root / "snapshot"
    snapshot.mkdir(parents=True)
    for name, content in task.files.items():
        target = snapshot / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    oracle_path = work_root / "oracle" / batch.ORACLE_FILE  # fora do workspace: o agente não o alcança
    oracle_path.parent.mkdir(parents=True)
    oracle_path.write_bytes(task.oracle.encode("utf-8"))
    controller = LadderController(
        policy,
        step_runner=batch.ladder_step_runner(task, make_llm, config),
        oracle_verifier=lambda workspace: batch.verify(oracle_path, workspace) == "PASS",
        snapshot_dir=snapshot,
        work_root=work_root,
    )
    result = controller.run(task.task_id)
    (task_dir / "laddering-summary.json").write_text(
        json.dumps(result.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    from dv_harness import batch
    from dv_harness.laddering import LadderPolicyError
    from dv_harness.llm_client import LLMClient, RetryPolicy

    args = _parser().parse_args(argv)
    policy, manifests, error = _gate(args, LadderPolicyError)
    if error:
        print(f"blocked: {error}. Nothing was run.")
        return 2
    tasks: list[Any] = []
    try:
        for manifest in manifests:
            _suite, loaded, _sha = batch.load_manifest(manifest)
            tasks.extend(loaded)
    except (batch.ManifestError, ValueError, OSError) as exc:
        print(f"blocked: {exc}. Nothing was run.")
        return 2
    if args.limit is not None:
        tasks = tasks[:args.limit]

    if args.validate_tasks:  # gate pré-lote: nenhuma chamada de modelo se houver violação
        from dv_harness.task_integrity import integrity_gate, integrity_table

        report = integrity_gate(tasks)
        if not report.passed:
            print(integrity_table(report))
            print("blocked: task integrity gate failed. Nothing was run.")
            return 2

    policy_sha256 = hashlib.sha256(args.policy.read_bytes()).hexdigest()

    def make_llm(rung: Any) -> Any:
        params = {"reasoning_effort": rung.reasoning_effort} if rung.reasoning_effort else {}
        return LLMClient(default_model=rung.model, retry=RetryPolicy(max_attempts=3, base_delay_s=2.0),
                         request_timeout_s=args.request_timeout_s, **params)

    # A configuração-base só carrega o que o adaptador não sobrescreve por degrau (timeouts, variante e teto).
    config = batch.BatchConfig(
        model=policy.rungs[0].model, request_timeout_s=args.request_timeout_s, max_steps=8,
        max_cost_usd=policy.global_max_cost_usd, fail_fast=False, suite_or_manifest="laddering",
        manifest_sha256=policy_sha256, git_head=None, api_key_env=args.api_key_env, harness_variant="b0_plus",
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results = [_run_task(task, policy, make_llm, config, args.output_dir) for task in tasks]
    summary = _aggregate(results, policy, policy_sha256)
    (args.output_dir / "laddering-batch-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("tasks", "outcome_counts", "resolved_at_rung_counts",
                                              "escalation_total", "total_cost_lower_bound_usd")},
                     indent=2, ensure_ascii=False))
    print(f"summary: {args.output_dir / 'laddering-batch-summary.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
