"""Smoke test exploratório com modelo real: uma tarefa pequena e controlada.

Tarefa: ``calc.py`` tem ``add`` com bug (``a - b``). O agente deve corrigir o arquivo para que
``check.py`` passe e então chamar ``submit``. O veredito é dado por ``check.py`` rodado
**fora do loop**, e o ``outcome`` vem de ``protocol.outcome_for``. Não é a palavra do agente.

Parâmetros fixos: ``max_steps=8``, ``max_cost_usd=0.10``. Padrões: ``--model claude-haiku-5-5`` e
``--api-key-env ANTHROPIC_API_KEY``. Ambos podem ser trocados (ex.: ``--model gemini/gemini-2.5-flash``
``--api-key-env GEMINI_API_KEY``).
Esses modelos têm preço na tabela local do litellm (``litellm.model_cost``, imagem ``dv-harness:local``).
Um nome com prefixo ``anthropic/`` não tem, e o orçamento de custo ficaria ``budget_unverifiable``
desde o primeiro passo.

Pré-requisito: a variável indicada em ``--api-key-env`` no ambiente. O script **não** imprime o valor,
e recusa rodar se ela estiver ausente ou vazia (código de saída 2). O valor entra na varredura de
vazamento do ledger e do resultado. Não simula resposta de modelo.

Verificações do registro: campos de proveniência presentes ou ``None`` com motivo, ausência de
sentinelas, schema do ledger, e varredura de vazamento do valor da chave em ledger e resultado.

Uso (fora do repositório):
    python scripts/dv_smoke_run.py --outdir <dir-descartavel>
    python scripts/dv_smoke_run.py --outdir <dir> --model gemini/gemini-2.5-flash --api-key-env GEMINI_API_KEY
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

MODEL = "claude-haiku-5-5"  # padrão; --model troca
MAX_STEPS = 8
MAX_COST_USD = 0.10
KEY_VAR = "ANTHROPIC_API_KEY"  # padrão; --api-key-env troca
KEY_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
# Orçamento de tempo: 3 tentativas × 30s + backoff (2s + 4s) = 96s < --timeout-s 300 do container.
REQUEST_TIMEOUT_S = 30.0  # padrão; --request-timeout-s troca

BUGGY_CALC = "def add(a, b):\n    return a - b\n"
CHECK = "from calc import add\n\nassert add(2, 3) == 5\nassert add(-1, 1) == 0\nprint('OK')\n"
TASK = (
    "The file calc.py contains a function add(a, b) with a bug. Fix it so that check.py passes "
    "(run `python3 check.py` to see). Use the tools to read, edit and run. When the fix is done, "
    "call submit with a one-sentence answer describing the change."
)
SYSTEM = (
    "You are a careful coding agent working in a workspace directory. You act only with the tools "
    "provided. Finish by calling submit(answer)."
)


def _verify(workdir: Path) -> str:
    """Veredito independente: ``check.py`` rodado pelo próprio harness, fora do loop."""
    proc = subprocess.run([sys.executable, "check.py"], cwd=workdir, capture_output=True, text=True, timeout=30)
    return "PASS" if proc.returncode == 0 else "FAIL"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outdir", type=Path, default=None)
    parser.add_argument("--model", default=MODEL, help=f"padrão: {MODEL}")
    parser.add_argument("--api-key-env", default=KEY_VAR, metavar="NAME",
                        help=f"nome da variável com a chave; padrão: {KEY_VAR}")
    parser.add_argument("--request-timeout-s", type=float, default=REQUEST_TIMEOUT_S, metavar="S",
                        help=f"timeout por requisição HTTP; padrão: {REQUEST_TIMEOUT_S}")
    parser.add_argument("--harness-variant", default="b0", choices=["b0", "b0_plus"],
                        help="b0 (padrão) ou b0_plus (ablação opt-in)")
    args = parser.parse_args()

    # O argumento é um NOME. Um valor de chave passado por engano não pode ser ecoado nas mensagens.
    if not KEY_NAME_RE.fullmatch(args.api_key_env):
        print("blocked: --api-key-env must be a variable NAME matching [A-Z_][A-Z0-9_]*. Nothing was run.")
        return 2
    if not args.request_timeout_s > 0:
        print("blocked: --request-timeout-s must be > 0. Nothing was run.")
        return 2
    if not os.environ.get(args.api_key_env):
        print(f"blocked: {args.api_key_env} is not set (or empty) in this environment. Nothing was run.")
        print("The key must be added as an environment variable in the environment settings, then a new session started.")
        return 2

    from dv_harness.agent_loop import AgentLoop
    from dv_harness.context_manager import ContextManager
    from dv_harness.ledger import RunLedger, reconcile
    from dv_harness.llm_client import LLMClient, RetryPolicy
    from dv_harness.protocol import outcome_for
    from dv_harness.tool_executor import ToolExecutor

    outdir = args.outdir or Path(tempfile.mkdtemp(prefix="dv-smoke-"))
    outdir.mkdir(parents=True, exist_ok=True)
    workspace = outdir / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "calc.py").write_text(BUGGY_CALC, encoding="utf-8")
    (workspace / "check.py").write_text(CHECK, encoding="utf-8")
    ledger_path = outdir / "ledger.jsonl"

    llm = LLMClient(default_model=args.model, retry=RetryPolicy(max_attempts=3, base_delay_s=2.0),
                    request_timeout_s=args.request_timeout_s)
    with RunLedger(ledger_path) as ledger:
        variant = args.harness_variant
        bash_logs = outdir / "bash_logs" if variant == "b0_plus" else None  # fora do workspace
        loop = AgentLoop(llm, ToolExecutor(workspace, harness_variant=variant, full_output_dir=bash_logs),
                         ContextManager(keep_recent_observations=10, harness_variant=variant), ledger=ledger,
                         harness_variant=variant)
        result = loop.run(TASK, SYSTEM, max_steps=MAX_STEPS, max_cost_usd=MAX_COST_USD, run_id="smoke-calc-001")

    verdict = _verify(workspace)
    outcome = outcome_for(result.status, verdict)

    report = {
        "model_requested": args.model,
        "harness_variant": args.harness_variant,
        "status": result.status,
        "verifier_verdict": verdict,
        "outcome": outcome,
        "final_answer": result.final_answer,
        "steps": result.steps,
        "total_tokens": result.total_tokens,
        "tokens_lower_bound": result.tokens_lower_bound,
        "total_cost_usd": result.total_cost_usd,
        "cost_basis": result.cost_basis,
        "failed_attempt_count": result.failed_attempt_count,
        "total_wall_clock_ms": result.total_wall_clock_ms,
        "llm_wall_clock_ms": result.llm_wall_clock_ms,
        "tool_wall_clock_ms": result.tool_wall_clock_ms,
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
        "unmeasured": result.unmeasured,
    }
    result_path = outdir / "result.json"
    result_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- verificações do registro -----------------------------------------------------------
    key = os.environ[args.api_key_env]
    leaks = []
    for path in (ledger_path, result_path):
        if key and key in path.read_text(encoding="utf-8"):
            leaks.append(str(path))
    summary = reconcile(ledger_path)

    print(f"status={result.status} verdict={verdict} outcome={outcome} steps={result.steps}")
    print(f"model_returned={[c.model_returned for c in result.calls]} provider={[c.provider for c in result.calls]}")
    print(f"api_id={[c.api_id for c in result.calls]} cost_basis={result.cost_basis} cost={result.total_cost_usd}")
    print(f"ledger: calls_started={summary['calls_started']} calls_finished={summary['calls_finished']} "
          f"in_flight={summary['in_flight_call_indices']} tool_events={summary['tool_events']}")
    print(f"secret leak scan (ledger, result): {'LEAK' if leaks else 'clean'}")
    print(f"report: {result_path}")
    print(f"ledger: {ledger_path}")
    return 1 if leaks else 0


if __name__ == "__main__":
    sys.exit(main())
