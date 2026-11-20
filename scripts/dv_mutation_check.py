"""Verificação por mutação do harness dv_harness (protocolo p1 v2).

Para cada mutante: copia o repositório para um diretório de trabalho descartável, aplica
uma substituição textual exata (que deve ocorrer exatamente uma vez) em um arquivo de
``src/``, e roda os testes relevantes. Um mutante é **morto** se algum teste falhar.
Um mutante **sobrevive** se todos os testes passarem: isso revela lacuna de teste.

Antes de qualquer mutante, a linha de base (sem mutação) precisa passar. Senão o script aborta.

Uso (a partir da raiz do repositório):
    python scripts/dv_mutation_check.py --workdir /caminho/descartavel --json resultado.json

Não altera o repositório. Os mutantes vivem só na cópia.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IGNORE = shutil.ignore_patterns(".git", ".venv", "__pycache__", ".pytest_cache", "*.pyc")


@dataclass(frozen=True)
class Mutant:
    mid: str
    file: str  # relativo à raiz do repositório
    old: str
    new: str
    what: str  # comportamento que o mutante quebra
    tests: tuple[str, ...]  # arquivos de teste a rodar


SRC = "src/dv_harness"
T_LOOP = ("tests/test_agent_loop.py", "tests/test_properties.py")
T_LLM = ("tests/test_llm_client.py", "tests/test_properties.py")
T_TOOLS = ("tests/test_tool_executor.py", "tests/test_properties.py")
T_PROTO = ("tests/test_protocol.py",)
T_LEDGER = ("tests/test_ledger.py", "tests/test_agent_loop.py")

MUTANTS: tuple[Mutant, ...] = (
    # --- precedência de estados (D1) -----------------------------------------------
    Mutant("M01", f"{SRC}/agent_loop.py",
           "    if max_tokens is not None and sum(known_tokens) >= max_tokens:\n        return protocol.STATUS_BUDGET",
           "    if False:\n        return protocol.STATUS_BUDGET",
           "max_tokens breach ignored", T_LOOP),
    Mutant("M02", f"{SRC}/agent_loop.py",
           "    if max_cost_usd is not None and sum(known_costs) >= max_cost_usd:\n        return protocol.STATUS_BUDGET",
           "    if False:\n        return protocol.STATUS_BUDGET",
           "known cost breach ignored", T_LOOP),
    Mutant("M03", f"{SRC}/agent_loop.py",
           "    if max_cost_usd is not None and unknown_cost:\n        return protocol.STATUS_UNVERIFIABLE",
           "    if False:\n        return protocol.STATUS_UNVERIFIABLE",
           "unverifiable budget ignored", T_LOOP),
    Mutant("M04", f"{SRC}/agent_loop.py",
           "        if state.consecutive_errors >= self.error_limit:",
           "        if state.consecutive_errors > self.error_limit:",
           "error limit off by one", T_LOOP),
    Mutant("M05", f"{SRC}/agent_loop.py",
           "        state.consecutive_errors = state.consecutive_errors + 1 if turn_format_error else 0",
           "        state.consecutive_errors = state.consecutive_errors + 1 if turn_format_error else state.consecutive_errors",
           "error counter never resets", T_LOOP),
    Mutant("M06", f"{SRC}/agent_loop.py",
           "        if not response.tool_calls:\n            state.messages.append({\"role\": \"user\", \"content\": NO_TOOL_CALL_MESSAGE})\n            turn_format_error = True",
           "        if not response.tool_calls:\n            state.messages.append({\"role\": \"user\", \"content\": NO_TOOL_CALL_MESSAGE})",
           "text-only reply treated as clean turn (not format error)", T_LOOP),
    Mutant("M07", f"{SRC}/agent_loop.py",
           "        if _steps(state.calls) >= limits.max_steps:",
           "        if len(state.calls) >= limits.max_steps:",
           "max_steps counts failed calls", T_LOOP),
    Mutant("M08", f"{SRC}/agent_loop.py",
           "            return budget, state.last_text, None\n\n        submitted, turn_format_error",
           "            pass\n\n        submitted, turn_format_error",
           "budget breach does not stop the run", T_LOOP),
    Mutant("M09", f"{SRC}/agent_loop.py",
           "        if budget is not None:\n            for call in response.tool_calls:",
           "        if budget is not None and False:\n            for call in response.tool_calls:",
           "budget breach does not stop tools (I5 witness)", T_LOOP),
    # --- provedor, tentativas e retentativas (A4) ----------------------------------
    Mutant("M10", f"{SRC}/agent_loop.py",
           "        attempts=exc.attempts,\n        attempt_usage_complete=False,",
           "        attempts=(),\n        attempt_usage_complete=False,",
           "failed attempts not recorded in CallRecord", T_LOOP),
    Mutant("M11", f"{SRC}/llm_client.py",
           "        return not isinstance(exc, self.abort_on)",
           "        return False",
           "no retries at all", T_LLM),
    Mutant("M12", f"{SRC}/llm_client.py",
           "        return not isinstance(exc, self.abort_on)",
           "        return True",
           "abort list bypassed (aborts are retried)", T_LLM),
    Mutant("M13", f"{SRC}/llm_client.py",
           "        return min(self.max_delay_s, self.base_delay_s * (self.multiplier ** (failed_attempt - 1)))",
           "        return min(self.max_delay_s, self.base_delay_s)",
           "backoff not exponential", T_LLM),
    Mutant("M14", f"{SRC}/llm_client.py",
           "        prompt_tokens = _int_or_none(getattr(usage, \"prompt_tokens\", None))",
           "        prompt_tokens = int(getattr(usage, \"prompt_tokens\", 0) or 0)",
           "sentinel 0 for missing usage (I6)", T_LLM),
    Mutant("M15", f"{SRC}/agent_loop.py",
           "        if v is None or not c.attempt_usage_complete:\n            return None",
           "        if v is None:\n            return None",
           "totals ignore incomplete attempt usage (D5)", T_LOOP),
    # --- ferramentas, ambiente, auditoria, linhas, CRLF, caminhos -------------------
    Mutant("M16", f"{SRC}/tool_executor.py",
           "    env = {k: host[k] for k in ENV_ALLOWLIST if k in host}",
           "    env = dict(host)",
           "environment inherited without allowlist (B6)", T_TOOLS),
    Mutant("M17", f"{SRC}/tool_executor.py",
           "            raw_output=combined,\n        )",
           "            raw_output=out,\n        )",
           "raw audit output is the truncated text (A6)", T_TOOLS),
    Mutant("M18", f"{SRC}/tool_executor.py",
           "    parts = text.split(\"\\n\")",
           "    parts = text.splitlines()",
           "line split uses splitlines (B7)", T_TOOLS),
    Mutant("M19", f"{SRC}/tool_executor.py",
           "    pure_crlf = crlf > 0 and text.count(\"\\n\") == crlf",
           "    pure_crlf = False",
           "CRLF normalization disabled (B8)", T_TOOLS),
    Mutant("M20", f"{SRC}/tool_executor.py",
           "    if \"\\x00\" in path:",
           "    if False:",
           "NUL byte not caught early (B9)", T_TOOLS),
    Mutant("M21", f"{SRC}/tool_executor.py",
           "        try:\n            os.stat(candidate)\n        except FileNotFoundError:\n            pass\n        except OSError as exc:\n            raise PathError(f\"path is not usable: {path}: {exc.strerror or exc}\") from exc",
           "        pass",
           "ENAMETOOLONG escapes as exception (I3)", T_TOOLS),
    Mutant("M22", f"{SRC}/tool_executor.py",
           "        if candidate != self.root and not candidate.is_relative_to(self.root):",
           "        if False:",
           "workspace confinement disabled (I3)", T_TOOLS),
    # --- ledger, protocolo, contrato ------------------------------------------------
    Mutant("M23", f"{SRC}/ledger.py",
           "        os.fsync(self._fh.fileno())",
           "        pass",
           "ledger not durable per event (I1/D7)", T_LEDGER),
    Mutant("M24", f"{SRC}/ledger.py",
           "    in_flight = sorted(started - set(finished))",
           "    in_flight = []",
           "reconcile hides in-flight calls (I1)", T_LEDGER),
    Mutant("M25", f"{SRC}/protocol.py",
           "    return COST_BASIS_ZERO if cost_usd == 0.0 else COST_BASIS_PRICED",
           "    return COST_BASIS_PRICED",
           "zero table price reported as priced (FREE != ZERO)", T_PROTO),
    Mutant("M26", f"{SRC}/protocol.py",
           "    return OUTCOME_SOLVED if verdict == VERDICT_PASS else OUTCOME_FAILURE",
           "    return OUTCOME_FAILURE if verdict == VERDICT_PASS else OUTCOME_SOLVED",
           "PASS and FAIL swapped in outcome mapping", T_PROTO),
    Mutant("M27", f"{SRC}/protocol.py",
           "    if status in (STATUS_UNVERIFIABLE, STATUS_PROVIDER_ERROR):",
           "    if status in (STATUS_PROVIDER_ERROR,):",
           "budget_unverifiable not fixed to INCONCLUSIVE (D2)", T_PROTO),
)


def _run_pytest(cwd: Path, tests: tuple[str, ...], timeout: int) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider"]
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, tail[-1] if tail else ""


def _copy_repo(dest: Path) -> Path:
    target = dest / "repo"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(REPO, target, ignore=IGNORE)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", type=Path, default=None, help="diretório de trabalho descartável")
    parser.add_argument("--json", type=Path, default=None, help="grava o resultado em JSON")
    parser.add_argument("--timeout", type=int, default=600, help="segundos por execução de teste")
    parser.add_argument("--only", nargs="*", default=None, help="ids de mutantes a rodar (ex.: M01 M10)")
    args = parser.parse_args()

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="dv-mutation-"))
    workdir.mkdir(parents=True, exist_ok=True)
    repo = _copy_repo(workdir)

    code, last = _run_pytest(repo, ("tests",), args.timeout)
    print(f"baseline: exit={code} {last}")
    if code != 0:
        print("baseline failed; aborting. Mutants are meaningless on a red baseline.")
        return 2

    selected = [m for m in MUTANTS if not args.only or m.mid in args.only]
    results = []
    for m in selected:
        path = repo / m.file
        original = path.read_text(encoding="utf-8")
        count = original.count(m.old)
        if count != 1:
            results.append({**asdict(m), "status": "INVALID", "detail": f"pattern occurs {count} times"})
            print(f"{m.mid} INVALID: pattern occurs {count} times in {m.file}")
            continue
        path.write_text(original.replace(m.old, m.new, 1), encoding="utf-8")
        started = time.monotonic()
        try:
            code, last = _run_pytest(repo, m.tests, args.timeout)
        finally:
            path.write_text(original, encoding="utf-8")
        elapsed = time.monotonic() - started
        status = "KILLED" if code != 0 else "SURVIVED"
        results.append({**asdict(m), "status": status, "exit": code, "detail": last, "seconds": round(elapsed, 1)})
        print(f"{m.mid} {status:8s} {m.what}  [{last}]")

    killed = sum(1 for r in results if r["status"] == "KILLED")
    survived = [r for r in results if r["status"] == "SURVIVED"]
    invalid = [r for r in results if r["status"] == "INVALID"]
    print(f"\nmutants: {len(results)}  killed: {killed}  survived: {len(survived)}  invalid: {len(invalid)}")
    for r in survived:
        print(f"  SURVIVED {r['mid']}: {r['what']}")
    if args.json:
        args.json.write_text(json.dumps({"baseline_exit": code, "results": results}, indent=2, ensure_ascii=False),
                             encoding="utf-8")
    return 0 if not survived and not invalid else 1


if __name__ == "__main__":
    sys.exit(main())
