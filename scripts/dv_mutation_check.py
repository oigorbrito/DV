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
T_CONT = ("tests/test_container_run.py",)
T_CANARY = ("tests/test_canary.py",)
SCRIPTS = "scripts"
T_LEDGER = ("tests/test_ledger.py", "tests/test_agent_loop.py")
T_BATCH = ("tests/test_batch_run.py",)
T_B0_PLUS = ("tests/test_b0_plus_variant.py",)
T_LADDER = ("tests/test_naive_laddering.py",)
T_INTEGRITY = ("tests/test_task_integrity.py",)

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
           "        if state.consecutive_protocol_errors >= self.error_limit:",
           "        if state.consecutive_protocol_errors > self.error_limit:",
           "error limit off by one", T_LOOP),
    Mutant("M05", f"{SRC}/agent_loop.py",
           "        else:\n            state.consecutive_protocol_errors = 0",
           "        else:\n            pass",
           "error counter never resets", T_LOOP),
    Mutant("M06", f"{SRC}/agent_loop.py",
           "            kinds.append(protocol.FAILURE_PROTOCOL)  # texto sem tool_call é erro de protocolo",
           "            pass",
           "text-only reply treated as clean turn (not format error)", T_LOOP),
    Mutant("M07", f"{SRC}/agent_loop.py",
           "        if _steps(state.calls) >= limits.max_steps:",
           "        if len(state.calls) >= limits.max_steps:",
           "max_steps counts failed calls", T_LOOP),
    Mutant("M08", f"{SRC}/agent_loop.py",
           "            return budget, state.last_text, None\n\n        submitted, kinds",
           "            pass\n\n        submitted, kinds",
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
           "        if \"\\x00\" in path:\n            raise PathError(\"invalid path: contains NUL byte\")\n        p = Path(path)",
           "        if False:\n            raise PathError(\"invalid path: contains NUL byte\")\n        p = Path(path)",
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
    # --- cota inferior de tokens e tentativas falhas (Decisão 1 refinada) ----------
    Mutant("M28", f"{SRC}/agent_loop.py",
           "            tokens_lower_bound=_lower_bound(calls),",
           "            tokens_lower_bound=_total(calls, lambda c: c.total_tokens),",
           "lower bound replaced by the complete total (None on any failure)", T_LOOP),
    Mutant("M29", f"{SRC}/agent_loop.py",
           "            failed_attempt_count=_failed_attempts(calls),",
           "            failed_attempt_count=sum(1 for c in calls if c.outcome == \"provider_error\"),",
           "failed attempt count ignores retried failures", T_LOOP),
    Mutant("M30", f"{SRC}/agent_loop.py",
           "    known = [c.total_tokens for c in calls if c.total_tokens is not None]\n    return sum(known) if known else None",
           "    known = [c.total_tokens for c in calls if c.total_tokens is not None and c.attempt_usage_complete]\n    return sum(known) if known else None",
           "lower bound drops calls that recovered from a retry", T_LOOP),
    # --- ajuste 1: PROTOCOL_ERROR x DOMAIN_FAILURE ----------------------------------
    Mutant("M31", f"{SRC}/agent_loop.py",
           "            kind=protocol.FAILURE_OK if result.ok else protocol.FAILURE_DOMAIN,",
           "            kind=protocol.FAILURE_OK if result.ok else protocol.FAILURE_PROTOCOL,",
           "bash exit != 0 classified as protocol error (counts toward error_limit)", T_LOOP),
    Mutant("M32", f"{SRC}/agent_loop.py",
           "        if protocol.FAILURE_PROTOCOL in kinds:\n            state.consecutive_protocol_errors += 1",
           "        if protocol.FAILURE_PROTOCOL in kinds or protocol.FAILURE_DOMAIN in kinds:\n            state.consecutive_protocol_errors += 1",
           "domain failure counts toward error_limit", T_LOOP),
    Mutant("M33", f"{SRC}/agent_loop.py",
           "            timed_out=result.timed_out,\n        )",
           "            timed_out=False,\n        )",
           "command timeout not flagged (cmd_timeouts lost)", T_LOOP),
    # --- ajuste 2: baldes de tokens, false_finish ------------------------------------
    Mutant("M34", f"{SRC}/agent_loop.py",
           "            total_cache_write_tokens=_total(calls, lambda c: c.cache_write_tokens),",
           "            total_cache_write_tokens=sum(c.cache_write_tokens or 0 for c in calls),",
           "cache_write total uses 0 as sentinel for unmeasured (I6)", T_LOOP),
    Mutant("M35", f"{SRC}/agent_loop.py",
           "        output_tokens=response.completion_tokens,",
           "        output_tokens=(response.completion_tokens or 0) + (response.reasoning_tokens or 0),",
           "reasoning double-counted into output_tokens", T_LOOP),
    Mutant("M36", f"{SRC}/llm_client.py",
           "        if cache_write is None:\n            cache_write = _detail(usage, \"prompt_tokens_details\", \"cache_creation_tokens\")",
           "        if cache_write is None:\n            cache_write = None",
           "cache_creation fallback removed (cache writes under the other name lost)", T_LLM),
    Mutant("M37", f"{SRC}/protocol.py",
           "    return status == STATUS_COMPLETED and outcome == OUTCOME_FAILURE",
           "    return status == STATUS_COMPLETED and outcome is not None",
           "false_finish counts verified successes as false finishes", T_PROTO),
    Mutant("M38", f"{SRC}/protocol.py",
           "    verified = [o for s, o in runs if s == STATUS_COMPLETED and o in (OUTCOME_SOLVED, OUTCOME_FAILURE)]",
           "    verified = [o for s, o in runs if s == STATUS_COMPLETED]",
           "false_finish_rate counts unverified completions in denominator", T_PROTO),
    Mutant("M39", f"{SRC}/protocol.py",
           "    if not verified:\n        return None",
           "    if not verified:\n        return 0.0",
           "false_finish_rate reports 0 instead of None without denominator", T_PROTO),
    # --- ajuste 3: wrapper de container -------------------------------------------------
    Mutant("M40", f"{SCRIPTS}/dv_container_run.py",
           "    if self.network != \"none\" and not allow_network:",
           "    if False:",
           "non-isolated network accepted by default", T_CONT),
    Mutant("M41", f"{SCRIPTS}/dv_container_run.py",
           "        \"--cap-drop\", \"ALL\",\n",
           "",
           "container keeps Linux capabilities", T_CONT),
    Mutant("M42", f"{SCRIPTS}/dv_container_run.py",
           "            _docker(runner, [\"kill\", name], timeout=30)",
           "            pass",
           "timed-out container not killed", T_CONT),
    Mutant("M43", f"{SCRIPTS}/dv_container_run.py",
           "    if cp.returncode != 0:\n        return None",
           "    if cp.returncode != 0:\n        return False",
           "failed OOM inspection reported as not OOM-killed (not measured)", T_CONT),
    Mutant("M44", f"{SCRIPTS}/dv_container_run.py",
           "    elif timed_out:\n        exit_code = EXIT_TIMEOUT",
           "    elif False:\n        exit_code = EXIT_TIMEOUT",
           "timeout reported with the killed process code, not 124", T_CONT),
    # --- ajuste 3 (fechamento): limpeza protegida e isca somente leitura ---------------
    Mutant("M45", f"{SCRIPTS}/dv_canary_check.py",
           "            if not path.is_file() or path.read_text(encoding=\"utf-8\") != ORIGINAL:",
           "            if False:",
           "read-only control runs without verifying provisioned baits", T_CANARY),
    Mutant("M46", f"{SCRIPTS}/dv_container_run.py",
           "            rm_error = f\"rm: {type(exc).__name__}: {exc}\"",
           "            pass",
           "cleanup failure swallowed (container may leak silently)", T_CONT),
    Mutant("M47", f"{SCRIPTS}/dv_container_run.py",
           "        daemon_error = True  # telemetria ou limpeza incompleta: falha de infraestrutura, não do comando",
           "        pass",
           "cleanup failure does not signal infrastructure (exit 125 lost)", T_CONT),
    Mutant("M48", f"{SCRIPTS}/dv_container_run.py",
           "    if cleanup_error is not None:\n        exit_code = EXIT_INFRA",
           "    if False:\n        exit_code = EXIT_INFRA",
           "timeout + failed kill reported as 124 instead of infrastructure 125", T_CONT),
    Mutant("M49", f"{SCRIPTS}/dv_container_run.py",
           "            kill_error = f\"kill: {type(exc).__name__}: {exc}\"",
           "            pass",
           "failed docker kill not recorded in cleanup_error", T_CONT),
    # --- ajuste 1/2 (smoke em container): montagem /app, PYTHONPATH e --pass-env -----------
    Mutant("M50", f"{SCRIPTS}/dv_container_run.py",
           'PASS_ENV_RE = re.compile(r"[A-Z_][A-Z0-9_]*")',
           'PASS_ENV_RE = re.compile(r".+")',
           "--pass-env name pattern accepts any text (lowercase, spaces, '=')", T_CONT),
    Mutant("M51", f"{SCRIPTS}/dv_container_run.py",
           '        argv += ["-e", var]',
           '        argv += ["-e", var + "=" + os.environ.get(var, "")]',
           "--pass-env emits NAME=value on the docker argv (secret on the command line)", T_CONT),
    Mutant("M52", f"{SCRIPTS}/dv_container_run.py",
           "        if not os.environ.get(name):",
           "        if False:",
           "--pass-env missing from host environment not refused before docker", T_CONT),
    Mutant("M53", f"{SCRIPTS}/dv_container_run.py",
           '        "-e", f"PYTHONPATH={APP_SRC}",\n',
           "",
           "PYTHONPATH=/app/src not set in the container", T_CONT),
    Mutant("M54", f"{SCRIPTS}/dv_container_run.py",
           ',target={APP_TARGET},readonly"',
           ',target={APP_TARGET}"',
           "repo mounted at /app read-write", T_CONT),
    Mutant("M55", f"{SCRIPTS}/dv_container_run.py",
           '    if repo is not None:\n        argv += ["--mount", _repo_mount(repo)]',
           '    if False:\n        argv += ["--mount", _repo_mount(repo)]',
           "repo not mounted at /app (smoke script absent inside the container)", T_CONT),
    Mutant("M56", f"{SCRIPTS}/dv_container_run.py",
           '    if repo is not None and any(re.search(',
           '    if False and any(re.search(',
           "second mount at /app accepted (duplicate repo mount)", T_CONT),
    Mutant("M57", f"{SCRIPTS}/dv_container_run.py",
           "        pass_env=list(pass_env),",
           '        pass_env=[os.environ.get(v, "") for v in pass_env],',
           "ledger records the secret value of --pass-env instead of the name", T_CONT),
    # --- timeout explícito por requisição (hang HTTP) ------------------------------
    Mutant("M58", f"{SRC}/llm_client.py",
           '            kwargs["timeout"] = self.request_timeout_s  # sem isto, um hang HTTP não levanta exceção',
           '            pass  # timeout not forwarded',
           "request timeout not forwarded to litellm (hang never raises)", T_LLM),
    Mutant("M59", f"{SRC}/llm_client.py",
           "        if isinstance(exc, TRANSIENT_TIMEOUT_EXCEPTIONS):\n            return True",
           "        if isinstance(exc, TRANSIENT_TIMEOUT_EXCEPTIONS):\n            return False",
           "timeouts treated as non-retryable (ledger closes on first hang)", T_LLM),
    # --- lote multi-tarefa (contrato v3: null com motivo; vazamento; oráculo fora do workspace) ---
    Mutant("M60", f"{SRC}/batch.py",
           "        unmeasured[key] = reason\n        return None\n    return sum(values)",
           "        unmeasured[key] = reason\n        return 0\n    return sum(values)",
           "batch total reports 0 instead of null when a run is unmeasured (I6)", T_BATCH),
    Mutant("M61", f"{SRC}/batch.py",
           "            if leaks:\n                aborted = \"secret_leak\"  # caminhos apenas; o valor nunca é impresso\n                break",
           "            if False:\n                aborted = \"secret_leak\"\n                break",
           "secret leak does not stop the batch", T_BATCH),
    Mutant("M62", f"{SRC}/batch.py",
           "    oracle_path = oracle_dir / ORACLE_FILE",
           "    oracle_path = workspace / ORACLE_FILE",
           "verifier written inside the agent workspace (agent can forge PASS)", T_BATCH),
    Mutant("M63", f"{SRC}/batch.py",
           "            if config.fail_fast and report[\"outcome\"] != protocol.OUTCOME_SOLVED:",
           "            if False:",
           "fail-fast ignored: batch keeps going after a non-SOLVED outcome", T_BATCH),
    Mutant("M64", f"{SRC}/batch.py",
           "            if spent >= config.max_cost_usd:",
           "            if False:",
           "batch ceiling ignored (lower bound of spent cost not enforced)", T_BATCH),
    Mutant("M65", f"{SRC}/batch.py",
           "    elif any(totals.get(b) is None for b in TELEMETRY_BUCKETS):",
           "    elif False:",
           "null telemetry buckets reported as S0_READY (telemetry limitation hidden)", T_BATCH),
    Mutant("M66", f"{SRC}/batch.py",
           "                report = harness_error_report(task, run_dir, exc, config, cap)",
           "                raise",
           "harness error propagates and kills the batch instead of being recorded per task", T_BATCH),
    # --- orçamento pelo limite inferior, pass_rate, usage_details e patch (auditoria 2026-11) ---
    Mutant("M67", f"{SRC}/agent_loop.py",
           "    unknown_cost = any(c.cost_usd is None for c in calls)",
           "    unknown_cost = any(c.cost_usd is None or not c.attempt_usage_complete for c in calls)",
           "recovered retry makes the budget unverifiable and skips the turn's tools again", T_LOOP),
    Mutant("M68", f"{SRC}/batch.py",
           "        \"pass_rate\": solved_n / (solved_n + failure_n) if solved_n + failure_n else None,",
           "        \"pass_rate\": solved_n / n if n else None,",
           "pass_rate counts INCONCLUSIVE runs in the denominator again", T_BATCH),
    Mutant("M69", f"{SRC}/llm_client.py",
           "            usage_details=_usage_details(usage),",
           "            usage_details=None,",
           "usage subfields (cache/reasoning details) not recorded for audit", T_LLM),
    Mutant("M70", f"{SRC}/batch.py",
           "        if before == after:",
           "        if False:",
           "patch stats count every file as changed (regression of the write_text CRLF bug)", T_BATCH),
    # --- variante b0_plus (opt-in) e isolamento do b0 ---
    Mutant("M71", f"{SRC}/tool_executor.py",
           "    if len(windows) > 1:",
           "    if False:",
           "edit fallback applies an ambiguous match silently instead of rejecting it", T_B0_PLUS),
    Mutant("M72", f"{SRC}/agent_loop.py",
           "        return self._streak >= self.threshold",
           "        return self._streak == self.threshold",
           "doom loop fires only once per sequence (no continuous advisory from the 4th repetition)", T_B0_PLUS),
    Mutant("M76", f"{SRC}/agent_loop.py",
           "count=state.doom.streak)",
           "count=state.doom.threshold)",
           "advisory reports the threshold instead of the current repetition count", T_B0_PLUS),
    Mutant("M77", f"{SRC}/tool_executor.py",
           "            block = _reindent_block(new, target)",
           "            block = new",
           "indentation_flexible writes new_text at the wrong indentation (no relative re-indent)", T_B0_PLUS),
    Mutant("M78", f"{SRC}/tool_executor.py",
           "    shift = target - base",
           "    shift = target",
           "re-indent shifts absolutely instead of relatively (double indentation when new_text is already aligned)", T_B0_PLUS),
    Mutant("M73", f"{SRC}/tool_executor.py",
           "        return _apply_unique_replacement(text, old_text, new_text, path)",
           "        return _apply_edit_with_fallback(text, old_text, new_text, path)[0:3:2]",
           "b0 silently gets the b0_plus fuzzy edit (default mode no longer isolated)", T_B0_PLUS),
    Mutant("M74", f"{SRC}/tool_executor.py",
           "    head = int(budget * TAIL_HEAD_RATIO)",
           "    head = budget // 2",
           "tail truncation uses 50/50 instead of 20/80 (traceback start kept, end lost)", T_B0_PLUS),
    Mutant("M75", f"{SRC}/agent_loop.py",
           "            state.doom = DoomLoopTracker(DOOM_LOOP_THRESHOLD)",
           "            state.doom = DoomLoopTracker(DOOM_LOOP_THRESHOLD)\n        else:\n            state.doom = DoomLoopTracker(DOOM_LOOP_THRESHOLD)",
           "b0 gets the doom-loop advisory (default mode no longer isolated)", T_B0_PLUS),
    # --- motor nivelado (b0_plus): no-op, unicode, threshold, prefixo, continuacao, kill limitado ---
    Mutant("M79", f"{SRC}/tool_executor.py",
           "    if updated == text:",
           "    if False:",
           "no-op replacement silently written instead of rejected", T_B0_PLUS),
    Mutant("M80", f"{SRC}/tool_executor.py",
           "    return unicodedata.normalize(\"NFKC\", line).translate(_UNICODE_MAP).rstrip()",
           "    return line.rstrip()",
           "unicode normalization (NFKC, curly quotes, dashes, NBSP) disabled", T_B0_PLUS),
    Mutant("M81", f"{SRC}/tool_executor.py",
           "BLOCK_ANCHOR_THRESHOLD = 0.65",
           "BLOCK_ANCHOR_THRESHOLD = 0.5",
           "block_anchor accepts blocks below the OpenCode similarity threshold (0.65)", T_B0_PLUS),
    Mutant("M82", f"{SRC}/tool_executor.py",
           "    if variant != VARIANT_B0_PLUS:",
           "    if False:",
           "b0 receives the enriched tool descriptions (default mode no longer isolated)", T_B0_PLUS),
    Mutant("M83", f"{SRC}/tool_executor.py",
           "    if old in body:",
           "    if True:",
           "numbered-prefix strip applied even when the file literally contains the prefix", T_B0_PLUS),
    Mutant("M84", f"{SRC}/tool_executor.py",
           "Use start_line={end + 1} to continue.]",
           "Use start_line={end} to continue.]",
           "read_file continuation hint names the last shown line instead of the next one", T_B0_PLUS),
    Mutant("M85", f"{SRC}/tool_executor.py",
           "        return proc.communicate(timeout=POST_KILL_TIMEOUT_S)",
           "        return proc.communicate()",
           "post-kill communicate unbounded (a grandchild holding the pipes hangs the call)", T_B0_PLUS),
    Mutant("M86", f"{SRC}/tool_executor.py",
           "            subprocess.run([\"taskkill\", \"/F\", \"/T\", \"/PID\", str(proc.pid)], capture_output=True,\n                           check=False, timeout=10)",
           "            pass",
           "Windows timeout no longer kills the process tree (taskkill /T removed)", T_B0_PLUS),
    Mutant("M87", f"{SRC}/tool_executor.py",
           "    if un_old == old:",
           "    if True:",
           "escape_normalized stage disabled", T_B0_PLUS),
    Mutant("M88", f"{SRC}/tool_executor.py",
           "    if trimmed == old or not trimmed:",
           "    if True:",
           "trimmed_boundary stage disabled", T_B0_PLUS),
    # --- leitura somente-leitura do spill (b0_plus), aviso de linha unica longa, isolamento do b0 ---
    Mutant("M89", f"{SRC}/tool_executor.py",
           "        spill = self._readable_spill()",
           "        spill = None",
           "b0_plus cannot read the full output directory (read-only access disabled)", T_B0_PLUS),
    Mutant("M90", f"{SRC}/tool_executor.py",
           "            target = self._resolve(path)\n        except PathError as exc:\n            return ToolResult(ok=False, error=str(exc))\n        if target.is_dir():",
           "            target = self._resolve_for_read(path)\n        except PathError as exc:\n            return ToolResult(ok=False, error=str(exc))\n        if target.is_dir():",
           "write_file can write into the full output directory (read-only breached)", T_B0_PLUS),
    Mutant("M91", f"{SRC}/tool_executor.py",
           "        except ValueError:\n            return self._resolve(path)",
           "        except ValueError:\n            return candidate",
           "path traversal out of the full output directory is not refused", T_B0_PLUS),
    Mutant("M92", f"{SRC}/tool_executor.py",
           "            if current.is_symlink():",
           "            if False:",
           "symlinks inside the full output directory are followed", T_B0_PLUS),
    Mutant("M93", f"{SRC}/tool_executor.py",
           "    if not shown and lines:",
           "    if False:",
           "single long line is cut silently and gets a generic continuation hint", T_B0_PLUS),
    Mutant("M94", f"{SRC}/tool_executor.py",
           "        if self.harness_variant != VARIANT_B0_PLUS or self.full_output_dir is None:",
           "        if self.full_output_dir is None:",
           "b0 gains read access to the full output directory (default mode no longer isolated)", T_B0_PLUS),
    # --- trecho parecido (limiar 0.6), invalidacao de leitura obsoleta, isolamento do b0 ---
    Mutant("M95", f"{SRC}/tool_executor.py",
           "CLOSEST_MATCH_THRESHOLD = 0.6",
           "CLOSEST_MATCH_THRESHOLD = 0.5",
           "closest-match hint accepts windows below the aider threshold (0.6)", T_B0_PLUS),
    Mutant("M96", f"{SRC}/tool_executor.py",
           "    first = max(0, best_start - CLOSEST_MATCH_CONTEXT)",
           "    first = best_start",
           "closest-match window drops the five lines of context before the match", T_B0_PLUS),
    Mutant("M97", f"{SRC}/tool_executor.py",
           "    if new.strip() and new in body:",
           "    if False:",
           "no note when new_text is already in the file (duplicate edit goes unnoticed)", T_B0_PLUS),
    Mutant("M98", f"{SRC}/context_manager.py",
           "for index, norm, requested in reads",
           "for index, norm, requested in []",
           "stale read_file observations are never invalidated after an edit or write", T_B0_PLUS),
    Mutant("M99", f"{SRC}/context_manager.py",
           "        stale = _stale_read_indices(messages, calls) if self.harness_variant == VARIANT_B0_PLUS else {}",
           "        stale = _stale_read_indices(messages, calls)",
           "b0 invalidates stale reads (default mode no longer isolated)", T_B0_PLUS),
    # --- naive laddering guiado por oraculo externo: parada no oraculo, orcamento global, workspace limpo ---
    Mutant("M100", f"{SRC}/laddering.py",
           "        if oracle_pass:",
           "        if False:",
           "ladder does not stop when the external oracle approves a rung (keeps escalating)", T_LADDER),
    Mutant("M101", f"{SRC}/laddering.py",
           "            if remaining <= 0:",
           "            if False:",
           "ladder escalates after the global budget is exhausted", T_LADDER),
    Mutant("M102", f"{SRC}/laddering.py",
           "            shutil.rmtree(workspace)",
           "            pass",
           "ladder reuses the previous rung's workspace instead of a clean snapshot copy", T_LADDER),
    # --- validador de integridade de tarefas: .git, oraculo pre-patch, gold pos-patch ---
    Mutant("M103", f"{SRC}/task_integrity.py",
           "    return \".git\" in rel.split(\"/\")",
           "    return False",
           "exposed .git directory is not detected as a git leak", T_INTEGRITY),
    Mutant("M104", f"{SRC}/task_integrity.py",
           "    if pre_verdict == \"PASS\":",
           "    if False:",
           "oracle that already passes on the initial state is accepted (trivial task not rejected)", T_INTEGRITY),
    Mutant("M105", f"{SRC}/task_integrity.py",
           "    if verdict != \"PASS\":",
           "    if False:",
           "reference patch that the oracle rejects is accepted (broken gold not rejected)", T_INTEGRITY),
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
