"""Lote multi-tarefa com modelo real (contrato p1-run-accounting-v3). Ver ``src/dv_harness/batch.py``.

Padrões idênticos aos do ``dv_smoke_run.py``: ``--model claude-haiku-5-5``, ``--api-key-env ANTHROPIC_API_KEY``.
Para Gemini: ``--model gemini/gemini-3.1-flash-lite --api-key-env GEMINI_API_KEY``.

Uso (fora do repositório, em diretório limpo):
    python scripts/dv_batch_run.py --outdir <dir-limpo> --suite screening-n10
    python scripts/dv_batch_run.py --outdir <dir-limpo> --manifest <arquivo.json> --limit 3 --fail-fast

Gate (exit 2, nada executado): nome de variável inválido, variável ausente ou vazia (o valor nunca é
impresso), ``--request-timeout-s`` / ``--max-steps`` / ``--max-cost-usd`` / ``--limit`` fora do intervalo,
manifesto inválido ou não executável (ex.: candidatos S2 discovery-only), ``--outdir`` não vazio.

Saída: 0 lote concluído sem vazamento; 1 vazamento detectado (lote interrompido); 2 gate.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

MODEL = "claude-haiku-5-5"  # padrão igual ao do dv_smoke_run.py; --model troca
KEY_VAR = "ANTHROPIC_API_KEY"  # padrão; --api-key-env troca
REQUEST_TIMEOUT_S = 30.0  # 3 tentativas x 30s + backoff 2s + 4s = 96s por chamada travada
MAX_STEPS = 8
MAX_COST_USD = 0.50  # teto do LOTE (não por tarefa): cada tarefa recebe o que sobrou
KEY_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")


def _param_error(args: argparse.Namespace) -> str | None:
    """Primeiro problema de parâmetro, ou ``None``. Nunca ecoa valores de chave."""
    if not KEY_NAME_RE.fullmatch(args.api_key_env):
        return "--api-key-env must be a variable NAME matching [A-Z_][A-Z0-9_]*"
    if not (args.request_timeout_s > 0) or math.isnan(args.request_timeout_s):
        return "--request-timeout-s must be > 0"
    if args.max_steps < 1:
        return "--max-steps must be >= 1"
    if not (args.max_cost_usd > 0) or math.isnan(args.max_cost_usd):
        return "--max-cost-usd must be > 0"
    if args.limit is not None and args.limit < 1:
        return "--limit must be >= 1"
    if args.outdir.exists() and any(args.outdir.iterdir()):
        return f"--outdir {args.outdir} is not empty; use a clean directory"
    return None


def _load_tasks(args: argparse.Namespace, batch: Any) -> tuple[str, list[Any], str]:
    if args.manifest is not None:
        suite_id, tasks, manifest_sha = batch.load_manifest(args.manifest)
        label = f"manifest:{args.manifest.name}"
    else:
        suite_id = args.suite or batch.SUITE_SCREENING_N10
        tasks = batch.builtin_suite(suite_id)
        manifest_sha = batch.suite_sha256(tasks)
        label = suite_id
    return label, batch.select(tasks, args.limit), manifest_sha


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--suite", default=None, choices=["screening-n10"],
                     help="suíte embutida (padrão quando nenhum --manifest é dado)")
    src.add_argument("--manifest", type=Path, default=None, help="manifesto dv-batch-manifest-v1")
    parser.add_argument("--outdir", type=Path, required=True, help="diretório de saída, limpo ou inexistente")
    parser.add_argument("--model", default=MODEL, help=f"padrão: {MODEL}")
    parser.add_argument("--api-key-env", default=KEY_VAR, metavar="NAME",
                        help=f"nome da variável com a chave; padrão: {KEY_VAR}")
    parser.add_argument("--request-timeout-s", type=float, default=REQUEST_TIMEOUT_S, metavar="S",
                        help=f"timeout por requisição HTTP; padrão: {REQUEST_TIMEOUT_S}")
    parser.add_argument("--max-steps", type=int, default=MAX_STEPS, help=f"por tarefa; padrão: {MAX_STEPS}")
    parser.add_argument("--max-cost-usd", type=float, default=MAX_COST_USD,
                        help=f"teto de custo do LOTE (limite inferior conhecido); padrão: {MAX_COST_USD}")
    parser.add_argument("--fail-fast", action="store_true",
                        help="para o lote no primeiro outcome diferente de SOLVED (padrão: segue, sem abortar)")
    parser.add_argument("--limit", type=int, default=None, metavar="N", help="executa só as N primeiras tarefas")
    parser.add_argument("--harness-variant", default="b0", choices=["b0", "b0_plus"],
                        help="b0 (padrão, inalterado) ou b0_plus (ablação opt-in: edit fallback, cauda, doom loop)")
    parser.add_argument("--candidate-id", default=None, metavar="ID",
                        help="id do candidato no registro de triagem (opcional, somente leitura)")
    parser.add_argument("--git-head", default=None, metavar="SHA",
                        help="HEAD do repositório a registrar (padrão: git rev-parse HEAD, se disponível)")
    return parser


def main(argv: list[str] | None = None) -> int:
    from dv_harness import batch
    from dv_harness.llm_client import LLMClient, RetryPolicy

    args = _parser().parse_args(argv)

    error = _param_error(args)
    if error:
        print(f"blocked: {error}. Nothing was run.")
        return 2
    try:
        label, tasks, manifest_sha = _load_tasks(args, batch)
    except (batch.ManifestError, ValueError, OSError) as exc:
        print(f"blocked: {exc}. Nothing was run.")
        return 2
    if not os.environ.get(args.api_key_env):
        print(f"blocked: {args.api_key_env} is not set (or empty) in this environment. Nothing was run.")
        print("The key must be added as an environment variable in the environment settings, then a new session started.")
        return 2

    config = batch.BatchConfig(
        model=args.model,
        request_timeout_s=args.request_timeout_s,
        max_steps=args.max_steps,
        max_cost_usd=args.max_cost_usd,
        fail_fast=args.fail_fast,
        suite_or_manifest=label,
        manifest_sha256=manifest_sha,
        git_head=args.git_head or batch.resolve_git_head(REPO),
        api_key_env=args.api_key_env,
        candidate_id=args.candidate_id,
        harness_variant=args.harness_variant,
    )

    def make_llm(_task: batch.BatchTask) -> LLMClient:
        return LLMClient(default_model=args.model, retry=RetryPolicy(max_attempts=3, base_delay_s=2.0),
                         request_timeout_s=args.request_timeout_s)

    secret = os.environ[args.api_key_env]
    summary = batch.run_batch(tasks, args.outdir, config=config, make_llm=make_llm, secret=secret)

    leak = (not summary["leak_scan"]["clean"]) or any(not r["leak_clean"] for r in summary["runs"])
    print(json.dumps({k: summary[k] for k in (
        "total_tasks", "executed", "aborted", "outcome_counts", "verifier_verdict_counts", "family_outcome_counts",
        "pass_rate", "solved_rate", "false_finish_rate", "total_tokens", "tokens_lower_bound", "total_cost_usd",
        "cost_basis", "total_wall_clock_ms")}, indent=2, ensure_ascii=False))
    print(f"S0: {summary['s0_qualification']['status']} (treatment_eligible={summary['s0_qualification']['treatment_eligible']})")
    print(f"secret leak scan (all batch files): {'LEAK' if leak else 'clean'}")
    print(f"summary: {args.outdir / 'batch-summary.json'}")
    if leak:
        print("LEAK: a run or batch file contains the key value; batch stopped. Paths recorded in the summary.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
