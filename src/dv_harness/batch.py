"""Lote multi-tarefa sobre o ``AgentLoop``, sob o contrato ``p1-run-accounting-v4``.

Cada tarefa roda em ``<outdir>/runs/<task_id>/`` com:

- ``workspace/``: o único diretório que o executor enxerga (raiz do ``ToolExecutor``);
- ``oracle/check.py``: o verificador, **fora do workspace**. O agente não consegue editá-lo, então não
  consegue forjar ``PASS``. O harness roda o oráculo com ``PYTHONPATH`` apontando para o workspace e um
  ambiente mínimo (sem chaves de API);
- ``ledger.jsonl`` (``call_started``/``call_finished`` com fsync) e ``result.json``.

Após cada tarefa, a varredura anti-vazamento procura o valor da chave em todo o diretório da tarefa.
Achado de vazamento interrompe o lote (exit 1 na CLI). O valor nunca é gravado.

Famílias: taxonomia congelada em ``experiments/p1/s2-family-taxonomy-v1.json`` (F1 reparo localizado,
F2 reparo multi-arquivo, F3 mudança de comportamento, F4 completar testes, F5 refatoração, F6 configuração
/ integração de repositório). Os rótulos seguem essa taxonomia, não descrições alternativas.

Regras de agregação (contrato v3):

- Totais são ``None`` quando qualquer parcela é desconhecida, com motivo em ``unmeasured``. Nunca ``0``
  como sentinela (I6).
- ``pass_rate = PASS / (PASS + FAIL)``, ``solved_rate = SOLVED / N``, ``false_finish_rate`` pelo helper do
  protocolo. Sem denominador, ``None``. Não há score composto.
- Orçamento do lote: ``--max-cost-usd`` é o teto do lote. Decisões usam o limite inferior (soma dos custos
  conhecidos), como manda o contrato. Cada tarefa recebe como teto o que sobrou.

Classificação S0 (``experiments/p1/free-local-executor-screening-v1.json``): calculada a partir das
medições. Um lote **não** é evidência de qualificação: o binding não tem ``qualification_evidence_ref``,
então ``treatment_eligible`` é sempre ``False`` aqui.
"""

from __future__ import annotations

import dataclasses
import difflib
import hashlib
import json
import math
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
from dv_harness.laddering import StepResult
from dv_harness.context_manager import ContextManager
from dv_harness.ledger import RunLedger, file_sha256, reconcile
from dv_harness.tool_executor import ToolExecutor
from dv_harness.variants import DEFAULT_VARIANT, REFERENCE_PROVENANCE, VARIANT_B0_PLUS, check_variant

BATCH_SCHEMA = "dv-batch-summary-v1"
MANIFEST_SCHEMA = "dv-batch-manifest-v1"
SUITE_SCREENING_N10 = "screening-n10"
FAMILIES = ("F1", "F2", "F3", "F4", "F5", "F6")
TASK_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
FILE_PATH_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}(?:/[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}){0,5}")
KEY_NAME_RE = re.compile(r"[A-Z_][A-Z0-9_]*")
SHA40_RE = re.compile(r"[0-9a-f]{40}")
ORACLE_FILE = "check.py"
VERIFY_TIMEOUT_S = 30
ERROR_MAX_CHARS = 300
STATUS_HARNESS_ERROR = "harness_error"  # bug do harness numa tarefa: registrado, nunca silenciado
CREDENTIAL_ERRORS = frozenset({"AuthenticationError", "PermissionDeniedError"})
# Sub-baldes que o litellm/Gemini não expõe (null): sem eles o S0 é TELEMETRY_LIMITED.
TELEMETRY_BUCKETS = ("total_uncached_input_tokens", "total_cache_read_tokens", "total_reasoning_tokens")
# Campos de run que são somados no resumo; null quando a tarefa não mediu (ver _sum_or_none).
SUMMED_RUN_KEYS = (
    "total_tokens", "total_output_tokens", "total_uncached_input_tokens", "total_cache_read_tokens",
    "total_cache_write_tokens", "total_reasoning_tokens", "total_cost_usd", "total_wall_clock_ms",
    "failed_attempt_count",
)
SCOPE = {  # governança copiada por referência de experiments/p1/wp04-real-executor-smoke-v1.json#scope
    "classification": "PIPELINE_SMOKE_ONLY",
    "executor_ranking": "FORBIDDEN",
    "economic_superiority_claim": "FORBIDDEN",
    "routing_claim": "FORBIDDEN",
    "architecture_claim": "FORBIDDEN",
    "holdout_access": "FORBIDDEN",
    "source": "experiments/p1/wp04-real-executor-smoke-v1.json#scope",
}
REGISTRY_PATH = Path(__file__).resolve().parents[2] / "experiments" / "p1" / "free-local-executor-screening-v1.json"

SYSTEM_PROMPT = (
    "You are a careful coding agent working in a workspace directory. You act only with the tools "
    "provided. Finish by calling submit(answer)."
)


class ManifestError(ValueError):
    """Manifesto inválido ou não executável. Levantado antes de qualquer chamada ao provedor."""


@dataclass(frozen=True)
class BatchTask:
    task_id: str
    family: str
    statement: str
    files: dict[str, str]  # visíveis ao executor (dentro do workspace)
    oracle: str  # verificador: escrito FORA do workspace
    verifier_ref: str | None = None
    repository: str | None = None
    base_sha: str | None = None
    # Patch de referência como conteúdo pós-patch dos arquivos afetados (validado por task_integrity).
    reference_files: dict[str, str] = dataclasses.field(default_factory=dict)

    @property
    def statement_sha256(self) -> str:
        return hashlib.sha256(self.statement.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class BatchConfig:
    model: str
    request_timeout_s: float
    max_steps: int
    max_cost_usd: float  # teto do LOTE (limite inferior de custo conhecido)
    fail_fast: bool
    suite_or_manifest: str
    manifest_sha256: str
    git_head: str | None
    api_key_env: str
    candidate_id: str | None = None
    harness_variant: str = DEFAULT_VARIANT  # b0 (padrão) ou b0_plus (ablação opt-in)

    def describe(self) -> dict[str, Any]:
        return {
            "contract_id": protocol.CONTRACT_ID,
            "model_requested": self.model,
            "request_timeout_s": self.request_timeout_s,
            "max_steps": self.max_steps,
            "max_cost_usd_batch": self.max_cost_usd,
            "fail_fast": self.fail_fast,
            "suite_or_manifest": self.suite_or_manifest,
            "manifest_sha256": self.manifest_sha256,
            "git_head": self.git_head,
            "candidate_id": self.candidate_id,
            "api_key_env": self.api_key_env,  # o NOME da variável, nunca o valor
            "harness_variant": self.harness_variant,
            "reference_provenance": REFERENCE_PROVENANCE,
        }


# --- suíte embutida: screening-n10 (F1..F6, verificador independente por tarefa) ------------


def _statement(text: str) -> str:
    return (f"{text} Use the tools to read, edit and run. When the fix is done, call submit with a "
            "one-sentence answer describing the change. The verifier is hidden from you.")


def _task(task_id: str, family: str, text: str, files: dict[str, str], oracle: str) -> BatchTask:
    return BatchTask(task_id=task_id, family=family, statement=_statement(text), files=files, oracle=oracle)


_ROMAN_TABLE = ("[(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'), (100, 'C'), (90, 'XC'), (50, 'L'), "
                "(40, 'XL'), (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')]")
_ROMAN_ORACLE = f'''import os, pathlib, subprocess, sys, tempfile
WS = pathlib.Path(os.environ["PYTHONPATH"])
TEST = (WS / "tests" / "test_roman.py").read_text(encoding="utf-8")
TABLE = {_ROMAN_TABLE}
SUBTRACTIVE = {{(900, "CM"), (400, "CD"), (90, "XC"), (40, "XL"), (9, "IX"), (4, "IV")}}
def impl(table):
    return ("def to_roman(n):\\n    table = " + repr(table) + "\\n    out = ''\\n"
            "    for value, symbol in table:\\n        while n >= value:\\n            out += symbol\\n"
            "            n -= value\\n    return out\\n")
MUTANTS = {{
    "no_subtractive": [e for e in TABLE if e not in SUBTRACTIVE],
    "no_900": [e for e in TABLE if e != (900, "CM")],
    "no_thousands": [e for e in TABLE if e != (1000, "M")],
}}
def run(src):
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "roman.py").write_text(src, encoding="utf-8")
    (d / "test_roman.py").write_text(TEST, encoding="utf-8")
    env = {{"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return subprocess.run([sys.executable, str(d / "test_roman.py")], cwd=d, env=env,
                          capture_output=True, text=True, timeout=20).returncode
assert run(impl(TABLE)) == 0, "test file must pass on a correct to_roman"
for name, table in MUTANTS.items():
    assert run(impl(table)) != 0, "mutant survives: " + name
print("OK")
'''
_LEAP_ORACLE = '''import os, pathlib, subprocess, sys, tempfile
WS = pathlib.Path(os.environ["PYTHONPATH"])
TEST = (WS / "tests" / "test_leap.py").read_text(encoding="utf-8")
IMPLS = {
    "correct": "def is_leap(year):\\n    return (year % 4 == 0 and year % 100 != 0) or year % 400 == 0\\n",
    "ignores_century": "def is_leap(year):\\n    return year % 4 == 0\\n",
    "ignores_400": "def is_leap(year):\\n    return year % 4 == 0 and year % 100 != 0\\n",
}
def run(src):
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "leap.py").write_text(src, encoding="utf-8")
    (d / "test_leap.py").write_text(TEST, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    if "SYSTEMROOT" in os.environ:
        env["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    return subprocess.run([sys.executable, str(d / "test_leap.py")], cwd=d, env=env,
                          capture_output=True, text=True, timeout=20).returncode
assert run(IMPLS["correct"]) == 0, "test file must pass on a correct is_leap"
assert run(IMPLS["ignores_century"]) != 0, "mutant survives: ignores_century"
assert run(IMPLS["ignores_400"]) != 0, "mutant survives: ignores_400"
print("OK")
'''
_CLI_ORACLE = '''import os, subprocess, sys, tempfile
WS = os.environ["PYTHONPATH"]
CLI = os.path.join(WS, "cli.py")
def run(args):
    p = subprocess.run([sys.executable, CLI, *args], cwd=tempfile.gettempdir(), capture_output=True,
                       text=True, timeout=20)
    return p.returncode, p.stdout
rc, out = run([])
assert rc == 0 and out == "Hi\\nHi\\n", (rc, out)
rc, out = run(["--repeat", "1"])
assert rc == 0 and out == "Hi\\n", (rc, out)
print("OK")
'''
_CLI_SRC = '''import sys


def main(argv):
    print("Hello")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def builtin_suite(suite_id: str = SUITE_SCREENING_N10) -> list[BatchTask]:
    if suite_id != SUITE_SCREENING_N10:
        raise ManifestError(f"unknown suite: {suite_id!r}")
    return [
        _task("f1-greet", "F1", "greet.py: greet(name) must return 'Hello, <name>!'.",
              {"greet.py": 'def greet(name):\n    return "Hello " + name\n'},
              "from greet import greet\n\nassert greet('Ana') == 'Hello, Ana!'\nassert greet('') == 'Hello, !'\n"
              "print('OK')\n"),
        _task("f1-clamp", "F1", "clamp.py: clamp(x, lo, hi) must limit x to the closed interval [lo, hi].",
              {"clamp.py": "def clamp(x, lo, hi):\n    return max(lo, min(x, lo))\n"},
              "from clamp import clamp\n\nassert clamp(5, 0, 3) == 3\nassert clamp(-1, 0, 3) == 0\n"
              "assert clamp(2, 0, 3) == 2\nprint('OK')\n"),
        _task("f2-pricing", "F2",
              "shop.cart.total(items) must return the sum of the items plus SERVICE_FEE from shop/fees.py, "
              "rounded to 2 decimals. The defects are in shop/fees.py and shop/cart.py.",
              {"shop/__init__.py": "",
               "shop/fees.py": "SERVICE_FEE = 0.1\n\n\ndef apply_fee(value):\n    return value\n",
               "shop/cart.py": "from shop.fees import apply_fee\n\n\ndef total(items):\n    return sum(items)\n"},
              "from shop.cart import total\nfrom shop.fees import apply_fee\n\nassert total([10, 20]) == 33.0\n"
              "assert total([]) == 0.0\nassert apply_fee(100) == 110.0\nprint('OK')\n"),
        _task("f2-grades", "F2",
              "grades.report.letters(scores) must return one letter per score, in order, using grades.scale. "
              "A score of exactly 80 is a B. The defects are in grades/scale.py and grades/report.py.",
              {"grades/__init__.py": "",
               "grades/scale.py": ("def to_letter(score):\n    if score >= 90:\n        return 'A'\n"
                                   "    if score > 80:\n        return 'B'\n    if score >= 70:\n"
                                   "        return 'C'\n    return 'F'\n"),
               "grades/report.py": ("from grades.scale import to_letter\n\n\ndef letters(scores):\n"
                                    "    return [to_letter(s) for s in scores[1:]]\n")},
              "from grades.report import letters\nfrom grades.scale import to_letter\n\n"
              "assert to_letter(80) == 'B'\nassert to_letter(90) == 'A'\nassert to_letter(79) == 'C'\n"
              "assert letters([95, 80, 70, 10]) == ['A', 'B', 'C', 'F']\nprint('OK')\n"),
        _task("f3-percent", "F3",
              "pct.py: implement parse_percent(text). '45%' -> 0.45, '45' -> 0.45, ' 12 % ' -> 0.12. Empty input, "
              "a lone '%', or non-numeric text raises ValueError. The current function is a stub.",
              {"pct.py": "def parse_percent(text):\n    raise NotImplementedError\n"},
              "from pct import parse_percent\n\nassert parse_percent('45%') == 0.45\nassert parse_percent('45') == 0.45\n"
              "assert parse_percent(' 12 % ') == 0.12\nfor bad in ['', '%', 'abc', '1x%']:\n    try:\n"
              "        parse_percent(bad)\n    except ValueError:\n        continue\n"
              "    raise AssertionError('expected ValueError for ' + repr(bad))\nprint('OK')\n"),
        _task("f3-bytes", "F3",
              "sizes.py: implement human_size(n) for non-negative integers with base 1024. Below 1024 return "
              "'<n> B'. Otherwise one decimal in the largest unit among KB, MB, GB (1536 -> '1.5 KB', "
              "2097152 -> '2.0 MB'). The current function is a stub.",
              {"sizes.py": "def human_size(n):\n    return str(n)\n"},
              "from sizes import human_size\n\nassert human_size(0) == '0 B'\nassert human_size(512) == '512 B'\n"
              "assert human_size(1536) == '1.5 KB'\nassert human_size(2 * 1024 * 1024) == '2.0 MB'\n"
              "assert human_size(3 * 1024 ** 3) == '3.0 GB'\nprint('OK')\n"),
        _task("f4-leap", "F4",
              "tests/test_leap.py has one weak test. Complete it so it passes for a correct is_leap(year) and "
              "fails for implementations that ignore the century rule or the 400 rule.",
              {"leap.py": ("def is_leap(year):\n    return (year % 4 == 0 and year % 100 != 0) or year % 400 == 0\n"),
               "tests/test_leap.py": "from leap import is_leap\n\nassert is_leap(2024) is True\nprint('OK')\n"},
              _LEAP_ORACLE),
        _task("f4-roman", "F4",
              "tests/test_roman.py has one weak test. Complete it so it passes for a correct to_roman(n) (1..3999) "
              "and fails for implementations that drop subtractive pairs, mishandle 900, or cannot produce thousands.",
              {"roman.py": f"def to_roman(n):\n    table = {_ROMAN_TABLE}\n    out = ''\n    for value, symbol in table:\n"
                           "        while n >= value:\n            out += symbol\n            n -= value\n    return out\n",
               "tests/test_roman.py": "from roman import to_roman\n\nassert to_roman(3) == 'III'\nprint('OK')\n"},
              _ROMAN_ORACLE),
        _task("f5-money", "F5",
              "report.py: move the repeated money formatting into a helper _money(value) that both functions call "
              "(at least twice in the module). Output must remain identical.",
              {"report.py": ('def invoice_line(name, amount):\n    return f"{name}: " + f"${amount:,.2f}"\n\n\n'
                             'def receipt_total(amount, tax):\n    return "TOTAL " + f"${amount + tax:,.2f}"\n')},
              "import ast\nfrom report import invoice_line, receipt_total\n\n"
              "assert invoice_line('pen', 1234.5) == 'pen: $1,234.50'\nassert receipt_total(10, 0.5) == 'TOTAL $10.50'\n"
              "tree = ast.parse(open('report.py', encoding='utf-8').read())\n"
              "assert '_money' in [n.name for n in tree.body if isinstance(n, ast.FunctionDef)]\n"
              "calls = sum(1 for n in ast.walk(tree) if isinstance(n, ast.Call) and getattr(n.func, 'id', None) == '_money')\n"
              "assert calls >= 2, calls\nprint('OK')\n"),
        _task("f6-cli-config", "F6",
              "cli.py must read settings.json from its own directory (not the current directory) and print the "
              "greeting 'repeat' times, one per line. --repeat N overrides the config. The current cli.py ignores both.",
              {"cli.py": _CLI_SRC,
               "settings.json": json.dumps({"greeting": "Hi", "repeat": 2}) + "\n"},
              _CLI_ORACLE),
    ]


def suite_sha256(tasks: Sequence[BatchTask]) -> str:
    canon = json.dumps([[t.task_id, t.family, t.statement, t.files, t.oracle] for t in tasks],
                       ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


# --- manifesto (campos alinhados a experiments/p1/run-spec-v1.template.json#task) -----------


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
    seen: set[str] = set()
    tasks = [_task_from_entry(entry, seen) for entry in entries]
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
    family = entry.get("family")
    if family not in FAMILIES:
        raise ManifestError(f"{tid}: family must be one of {', '.join(FAMILIES)}")
    statement = entry.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise ManifestError(f"{tid}: statement must be a non-empty string")
    declared_sha = entry.get("task_statement_sha256")
    if declared_sha is not None and declared_sha != hashlib.sha256(statement.encode("utf-8")).hexdigest():
        raise ManifestError(f"{tid}: task_statement_sha256 does not match statement")
    base_sha = entry.get("base_sha")
    if base_sha is not None and not (isinstance(base_sha, str) and SHA40_RE.fullmatch(base_sha)):
        raise ManifestError(f"{tid}: base_sha must be 40 lowercase hex characters")
    files = _files_from_entry(tid, entry.get("files"))
    oracle = entry.get("oracle")
    if not isinstance(oracle, str) or not oracle.strip():
        raise ManifestError(f"{tid}: oracle must be a non-empty string")
    references = _files_from_entry(tid, entry["reference_files"]) if "reference_files" in entry else {}
    return BatchTask(task_id=tid, family=family, statement=statement, files=files, oracle=oracle,
                     reference_files=references,
                     verifier_ref=entry.get("verifier_ref") if isinstance(entry.get("verifier_ref"), str) else None,
                     repository=entry.get("repository") if isinstance(entry.get("repository"), str) else None,
                     base_sha=base_sha)


def _files_from_entry(tid: str, files: Any) -> dict[str, str]:
    if not isinstance(files, dict) or not files:
        raise ManifestError(f"{tid}: files must be a non-empty object")
    for name, content in files.items():
        if not isinstance(name, str) or not FILE_PATH_RE.fullmatch(name) or not isinstance(content, str):
            raise ManifestError(f"{tid}: invalid workspace file entry")
    return dict(files)


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


def run_agent_loop(task: BatchTask, workspace: Path, llm: Any, config: BatchConfig, *,
                   max_cost_usd: float, ledger: RunLedger, bash_logs: Path | None) -> Any:
    """Uma execução do AgentLoop sobre ``workspace``. Usada por ``run_one`` e pelo degrau do laddering."""
    variant = check_variant(config.harness_variant)
    loop = AgentLoop(llm, ToolExecutor(workspace, harness_variant=variant, full_output_dir=bash_logs),
                     ContextManager(keep_recent_observations=10, harness_variant=variant), ledger=ledger,
                     harness_variant=variant)
    return loop.run(task.statement, SYSTEM_PROMPT, max_steps=config.max_steps,
                    max_cost_usd=max_cost_usd, run_id=task.task_id)


def _definition_sha256(task: BatchTask) -> str:
    from dv_harness.task_integrity import task_definition_sha256  # import tardio: task_integrity importa batch

    return task_definition_sha256(task)


def run_one(task: BatchTask, run_dir: Path, llm: Any, config: BatchConfig, *,
            max_cost_usd: float | None = None) -> dict[str, Any]:
    """Executa uma tarefa em ``run_dir`` e devolve o relatório (também gravado em ``result.json``)."""
    cap = config.max_cost_usd if max_cost_usd is None else max_cost_usd
    workspace = run_dir / "workspace"
    oracle_dir = run_dir / "oracle"
    workspace.mkdir(parents=True)
    oracle_dir.mkdir()
    for name, content in task.files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))  # bytes exatos: write_text converteria \n em \r\n no Windows
    oracle_path = oracle_dir / ORACLE_FILE
    oracle_path.write_bytes(task.oracle.encode("utf-8"))

    with RunLedger(run_dir / "ledger.jsonl") as ledger:
        variant = check_variant(config.harness_variant)
        # Saída completa de run_bash (b0_plus) fica em run_dir/bash_logs, fora do workspace.
        bash_logs = run_dir / "bash_logs" if variant == VARIANT_B0_PLUS else None
        result = run_agent_loop(task, workspace, llm, config, max_cost_usd=cap, ledger=ledger,
                                bash_logs=bash_logs)

    verdict = verify(oracle_path, workspace)
    patch = _patch_stats(task.files, workspace)  # pós-execução: arquivos iniciais vs. finais do workspace
    report: dict[str, Any] = {
        "task_id": task.task_id,
        "family": task.family,
        "contract_id": protocol.CONTRACT_ID,
        "harness_variant": variant,
        "reference_provenance": REFERENCE_PROVENANCE,
        "task_definition_sha256": _definition_sha256(task),
        "ledger_sha256": file_sha256(run_dir / "ledger.jsonl"),  # bytes exatos, após fechar o ledger
        "model_requested": config.model,
        "task_cost_cap_usd": cap,
        "statement_sha256": task.statement_sha256,
        "oracle_sha256": hashlib.sha256(task.oracle.encode("utf-8")).hexdigest(),
        "verifier_ref": task.verifier_ref,
        "repository": task.repository,
        "base_sha": task.base_sha,
        "status": result.status,
        "verifier_verdict": verdict,
        # v4, regra a: oracle_pass e telemetry_complete são independentes. Telemetria nunca altera oracle_pass.
        "oracle_pass": None if verdict is None else verdict == protocol.VERDICT_PASS,
        "telemetry_complete": result.telemetry_complete,
        "cost_accounting_status": protocol.cost_accounting_status(result.telemetry_complete),
        "outcome": protocol.outcome_for(result.status, verdict),
        "final_answer": result.final_answer,
        "steps": result.steps,
        "total_tokens": result.total_tokens,
        "tokens_lower_bound": result.tokens_lower_bound,
        "total_prompt_tokens": result.total_prompt_tokens,
        "total_completion_tokens": result.total_completion_tokens,
        "total_output_tokens": result.total_output_tokens,
        "total_uncached_input_tokens": result.total_uncached_input_tokens,
        "total_cache_read_tokens": result.total_cache_read_tokens,
        "total_cache_write_tokens": result.total_cache_write_tokens,
        "total_reasoning_tokens": result.total_reasoning_tokens,
        "total_cost_usd": result.total_cost_usd,
        "cost_basis": result.cost_basis,
        "failed_attempt_count": result.failed_attempt_count,
        "total_wall_clock_ms": result.total_wall_clock_ms,
        "llm_wall_clock_ms": result.llm_wall_clock_ms,
        "tool_wall_clock_ms": result.tool_wall_clock_ms,
        "protocol_errors": result.protocol_errors,
        "domain_failures": result.domain_failures,
        "cmd_timeouts": result.cmd_timeouts,
        "error_types": sorted({a.error_type for c in result.calls for a in c.attempts if a.error_type}),
        "calls": [
            {"index": c.index, "outcome": c.outcome, "model_returned": c.model_returned, "provider": c.provider,
             "api_id": c.api_id, "total_tokens": c.total_tokens, "cost_usd": c.cost_usd, "cost_basis": c.cost_basis,
             "wall_clock_ms": c.wall_clock_ms, "attempts": len(c.attempts),
             "error_types": [a.error_type for a in c.attempts if a.error_type]}
            for c in result.calls
        ],
        "tool_events": [
            {"index": e.index, "call_index": e.call_index, "name": e.name, "ok": e.ok, "format_error": e.format_error,
             "raw_sha256": e.raw_sha256, "raw_len": len(e.raw_output), "truncated": e.truncated,
             "wall_clock_ms": e.wall_clock_ms, "failure_kind": e.failure_kind, "timed_out": e.timed_out,
             "arguments_sha256": e.arguments_sha256}
            for e in result.tool_events
        ],
        "patch_stats": patch,
        "unmeasured": dict(result.unmeasured),
    }
    (run_dir / "result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def harness_error_report(task: BatchTask, run_dir: Path, exc: BaseException, config: BatchConfig,
                         max_cost_usd: float) -> dict[str, Any]:
    """Relatório de tarefa interrompida por bug do harness. Todo campo não medido é ``None`` com motivo."""
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
        "family": task.family,
        "contract_id": protocol.CONTRACT_ID,
        "harness_variant": config.harness_variant,
        "reference_provenance": REFERENCE_PROVENANCE,
        "task_definition_sha256": _definition_sha256(task),
        "ledger_sha256": file_sha256(ledger_path) if ledger_path.exists() else None,
        "model_requested": config.model,
        "task_cost_cap_usd": max_cost_usd,
        "statement_sha256": task.statement_sha256,
        "verifier_ref": task.verifier_ref,
        "status": STATUS_HARNESS_ERROR,
        "verifier_verdict": None,
        "oracle_pass": None,
        "telemetry_complete": False,
        "cost_accounting_status": protocol.COST_ACCOUNTING_INCONCLUSIVE,
        "outcome": protocol.OUTCOME_INCONCLUSIVE,
        "steps": 0,
        "cost_basis": protocol.COST_BASIS_UNKNOWN,
        "error_types": [type(exc).__name__],
        "error": f"{type(exc).__name__}: {_truncate(str(exc))}",
        "calls": [],
        "tool_events": [],
    }
    for key in ("total_tokens", "tokens_lower_bound", "total_output_tokens", "total_uncached_input_tokens",
                "total_cache_read_tokens", "total_cache_write_tokens", "total_reasoning_tokens",
                "total_cost_usd", "failed_attempt_count", "total_wall_clock_ms"):
        report[key] = None
    report["unmeasured"] = {k: reason for k in report
                              if k.startswith("total_") or k in ("failed_attempt_count", "tokens_lower_bound")}
    report["unmeasured"]["verifier_verdict"] = "verification not reached"
    if report["ledger_sha256"] is None:
        report["unmeasured"]["ledger_sha256"] = "ledger file was not created before the harness error"
    report["patch_stats"] = None
    report["unmeasured"]["patch_stats"] = reason
    if in_flight:
        report["unmeasured"]["in_flight_calls"] = f"calls {in_flight} started and not finished: consumption unknown"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "result.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def _brief(report: dict[str, Any], leak_clean: bool) -> dict[str, Any]:
    return {
        "task_id": report["task_id"],
        "family": report["family"],
        "status": report["status"],
        "verifier_verdict": report["verifier_verdict"],
        "oracle_pass": report["oracle_pass"],
        "telemetry_complete": report["telemetry_complete"],
        "cost_accounting_status": report["cost_accounting_status"],
        "ledger_sha256": report["ledger_sha256"],
        "outcome": report["outcome"],
        "steps": report["steps"],
        "total_tokens": report["total_tokens"],
        "tokens_lower_bound": report["tokens_lower_bound"],
        "uncached_input_tokens": report["total_uncached_input_tokens"],
        "cache_read_tokens": report["total_cache_read_tokens"],
        "reasoning_tokens": report["total_reasoning_tokens"],
        "cost_usd": report["total_cost_usd"],
        "cost_basis": report["cost_basis"],
        "task_cost_cap_usd": report["task_cost_cap_usd"],
        "wall_clock_ms": report["total_wall_clock_ms"],
        "patch_stats": report.get("patch_stats"),
        "failed_attempt_count": report["failed_attempt_count"],
        "error_types": report["error_types"],
        "leak_clean": leak_clean,
        "result_json": f"runs/{report['task_id']}/result.json",
    }


# --- resumo consolidado ---------------------------------------------------------------

NULLABLE_SUMMARY_FIELDS = (
    "total_tokens", "tokens_lower_bound", "total_cost_usd", "total_wall_clock_ms", "total_uncached_input_tokens",
    "total_cache_read_tokens", "total_cache_write_tokens", "total_reasoning_tokens", "total_output_tokens",
    "failed_attempt_count_total", "pass_rate", "solved_rate", "false_finish_rate",
    "core_tokens_partial_per_solved", "git_head", "tokens_lower_bound_per_solved",
    "cost_lower_bound_usd_per_solved", "spent_lower_bound_usd", "cost_coverage_rate",
)


def _sum_or_none(runs: list[dict[str, Any]], key: str, reason: str, unmeasured: dict[str, str]) -> Any:
    values = [r[key] for r in runs]
    if not values or any(v is None for v in values):
        unmeasured[key] = reason
        return None
    return sum(values)


def _rates(runs: list[dict[str, Any]], outcomes: Counter, unmeasured: dict[str, str]) -> dict[str, Any]:
    n = len(runs)
    solved_n = outcomes.get(protocol.OUTCOME_SOLVED, 0)
    failure_n = outcomes.get(protocol.OUTCOME_FAILURE, 0)
    rates: dict[str, Any] = {
        # INCONCLUSIVE (provider_error, budget_unverifiable, erro de harness) não tem resultado verificado:
        # fica fora do denominador, e o contador é reportado à parte.
        "pass_rate": solved_n / (solved_n + failure_n) if solved_n + failure_n else None,
        "solved_rate": solved_n / n if n else None,
        "false_finish_rate": protocol.false_finish_rate([(r["status"], r["outcome"]) for r in runs]),
        "inconclusive_count": outcomes.get(protocol.OUTCOME_INCONCLUSIVE, 0),
        "verdict_available_count": sum(1 for r in runs if r["verifier_verdict"] is not None),
        "pass_rate_definition": "SOLVED / (SOLVED + FAILURE); INCONCLUSIVE runs excluded",
        "solved_rate_definition": "SOLVED / total_tasks; INCONCLUSIVE counted as unresolved",
    }
    reasons = {
        "pass_rate": "no SOLVED or FAILURE outcome (no verified run)",
        "solved_rate": "no runs executed",
        "false_finish_rate": "no completed run with a SOLVED or FAILURE outcome",
    }
    for key, reason in reasons.items():
        if rates[key] is None:
            unmeasured[key] = reason
    return rates


def qualify_s0(runs: list[dict[str, Any]], totals: dict[str, Any], registry: dict[str, Any]) -> dict[str, Any]:
    """Classificação S0 (free-local-executor-screening-v1), pela medição. Nunca concede elegibilidade."""
    reasons: list[str] = []
    if any(set(r.get("error_types", [])) & CREDENTIAL_ERRORS for r in runs):
        status = "S0_CREDENTIAL_BLOCKED"
        reasons.append("provider returned a credential or permission error")
    elif not any(r["verifier_verdict"] is not None for r in runs):
        status = "S0_ENVIRONMENT_BLOCKED"
        reasons.append("no run reached a verifier verdict")
    elif not any(r["outcome"] == protocol.OUTCOME_SOLVED for r in runs):
        status = "S0_FAIL"
        reasons.append("verified runs with zero SOLVED outcomes")
    elif any(totals.get(b) is None for b in TELEMETRY_BUCKETS):
        status = "S0_READY_TELEMETRY_LIMITED"
        missing = [b for b in TELEMETRY_BUCKETS if totals.get(b) is None]
        reasons.append("telemetry buckets null (not exposed by the provider/litellm): " + ", ".join(missing))
    else:
        status = "S0_READY"
    reasons.append("batch output is not qualification evidence: the binding has no qualification_evidence_ref "
                   "(experiments/p1/wp04-executor-binding-v2.template.json)")
    if registry["registry_status"] != "REGISTERED":
        reasons.append(f"candidate registry status: {registry['registry_status']}")
    return {"status": status, "treatment_eligible": False, "reasons": reasons, "registry": registry}


def screening_registry_status(candidate_id: str | None, registry_path: Path = REGISTRY_PATH) -> dict[str, Any]:
    """Situação do candidato no registro de triagem (somente leitura)."""
    if candidate_id is None:
        return {"candidate_id": None, "registry_status": "NOT_PROVIDED"}
    data = json.loads(registry_path.read_text(encoding="utf-8"))
    for entry in [*data.get("hosted_free_candidates", []), *data.get("local_candidates", [])]:
        if entry.get("candidate_id") == candidate_id:
            return {"candidate_id": candidate_id, "registry_status": "REGISTERED",
                    "screening_status": entry.get("screening_status"), "model_id": entry.get("model_id")}
    return {"candidate_id": candidate_id, "registry_status": "NOT_IN_REGISTRY"}


def _percentile(values: list[float], p: float) -> float | None:
    """Percentil por posto (nearest-rank). Sem valores: ``None``, nunca 0 como sentinela."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(p * len(ordered))) - 1]


def _nested(parent: str, values: dict[str, Any], reasons: dict[str, str],
            unmeasured: dict[str, str]) -> dict[str, Any]:
    """Todo ``None`` de um sub-bloco do resumo precisa de motivo em ``unmeasured["<bloco>.<campo>"]``."""
    for key, value in values.items():
        if value is None:
            unmeasured[f"{parent}.{key}"] = reasons[key]
    return values


def _wall_stats(runs: list[dict[str, Any]], unmeasured: dict[str, str]) -> dict[str, Any]:
    run_vals = [r["total_wall_clock_ms"] for r in runs if r["total_wall_clock_ms"] is not None]
    call_vals = [c["wall_clock_ms"] for r in runs for c in r["calls"] if c.get("wall_clock_ms") is not None]
    stats = {"run_p50": _percentile(run_vals, 0.5), "run_p95": _percentile(run_vals, 0.95),
             "call_p50": _percentile(call_vals, 0.5), "call_p95": _percentile(call_vals, 0.95)}
    reasons = {k: "no measured wall-clock values" for k in stats}
    return _nested("wall_clock_ms_stats", stats, reasons, unmeasured)


def _tool_stats(runs: list[dict[str, Any]], unmeasured: dict[str, str]) -> dict[str, Any]:
    events = [e for r in runs for e in r["tool_events"]]
    kinds = Counter(e.get("failure_kind", "UNKNOWN") for e in events)
    errors = sum(1 for e in events if not e["ok"])
    stats = {
        "total_tool_calls": len(events),
        "tool_error_rate": errors / len(events) if events else None,  # ok=False / total; inclui DOMAIN e PROTOCOL
        "failure_kind_counts": {k: kinds.get(k, 0) for k in (protocol.FAILURE_OK, protocol.FAILURE_DOMAIN,
                                                             protocol.FAILURE_PROTOCOL)},
    }
    reasons = {"tool_error_rate": "no tool calls executed", "total_tool_calls": "",
               "failure_kind_counts": ""}
    return _nested("tool_stats", stats, reasons, unmeasured)


def _patch_stats(initial: dict[str, str], workspace: Path) -> dict[str, Any]:
    """Arquivos alterados e linhas somadas/removidas: workspace final contra os arquivos iniciais."""
    final: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        if path.is_file():
            final[path.relative_to(workspace).as_posix()] = path.read_bytes().decode("utf-8", errors="replace")
    files_changed = added = removed = 0
    for name in sorted(set(initial) | set(final)):
        before, after = initial.get(name), final.get(name)
        if before == after:
            continue
        files_changed += 1
        matcher = difflib.SequenceMatcher(a=(before or "").splitlines(), b=(after or "").splitlines(),
                                          autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "delete"):
                removed += i2 - i1
            if tag in ("replace", "insert"):
                added += j2 - j1
    return {"files_changed": files_changed, "lines_added": added, "lines_removed": removed}


def _patch_summary(runs: list[dict[str, Any]], unmeasured: dict[str, str]) -> dict[str, Any]:
    measured = [r["patch_stats"] for r in runs if r.get("patch_stats") is not None]
    stats = {
        "measured_runs": len(measured),
        "unmeasured_runs": len(runs) - len(measured),
        "files_changed_total": sum(p["files_changed"] for p in measured) if measured else None,
        "lines_added_total": sum(p["lines_added"] for p in measured) if measured else None,
        "lines_removed_total": sum(p["lines_removed"] for p in measured) if measured else None,
    }
    reasons = {k: "no run produced a measurable workspace (harness error)" for k in stats}
    return _nested("patch_stats", stats, reasons, unmeasured)


def _derived_metrics(runs: list[dict[str, Any]], solved: int, tokens_lb: int | None, spent_lb: float | None,
                      unmeasured: dict[str, str]) -> dict[str, Any]:
    """Métricas derivadas do resumo. Limites inferiores são rotulados; ``None`` sempre com motivo."""
    tokens_lb_per_solved = tokens_lb / solved if tokens_lb is not None and solved > 0 else None
    cost_lb_per_solved = spent_lb / solved if spent_lb is not None and solved > 0 else None
    if tokens_lb_per_solved is None:
        unmeasured["tokens_lower_bound_per_solved"] = "no SOLVED task or no measured usage"
    if cost_lb_per_solved is None:
        unmeasured["cost_lower_bound_usd_per_solved"] = "no SOLVED task or no known call price"
    all_calls = [c for r in runs for c in r["calls"]]
    cost_coverage = sum(1 for c in all_calls if c.get("cost_usd") is not None) / len(all_calls) if all_calls else None
    if cost_coverage is None:
        unmeasured["cost_coverage_rate"] = "no provider calls executed"
    wall = _wall_stats(runs, unmeasured)
    tool = _tool_stats(runs, unmeasured)
    patch = _patch_summary(runs, unmeasured)
    return {"tokens_lower_bound_per_solved": tokens_lb_per_solved,
            "cost_lower_bound_usd_per_solved": cost_lb_per_solved, "cost_coverage_rate": cost_coverage,
            "wall_clock_ms_stats": wall, "tool_stats": tool, "patch_stats": patch}


def summarize(runs: list[dict[str, Any]], *, planned: int, config: BatchConfig, aborted: str | None) -> dict[str, Any]:
    n = len(runs)
    outcomes = Counter(r["outcome"] for r in runs)
    verdicts = Counter((r["verifier_verdict"] or "NO_VERDICT") for r in runs)
    statuses = Counter(r["status"] for r in runs)
    unmeasured: dict[str, str] = {}
    reason = "one or more runs did not measure this quantity"

    total_tokens = _sum_or_none(runs, "total_tokens", "one or more runs have incomplete token usage (contract v3, D5)",
                                unmeasured)
    lb_values = [r["tokens_lower_bound"] for r in runs if r["tokens_lower_bound"] is not None]
    tokens_lb = sum(lb_values) if lb_values else None
    if tokens_lb is None:
        unmeasured["tokens_lower_bound"] = "no run returned measured usage"
    known_costs = [r["total_cost_usd"] for r in runs if r["total_cost_usd"] is not None]
    spent_lb = sum(known_costs) if known_costs else None  # None, nunca 0: sem custo conhecido nada foi medido
    if spent_lb is None:
        unmeasured["spent_lower_bound_usd"] = "no call has a known price"
    total_cost = _sum_or_none(runs, "total_cost_usd", "one or more calls have unknown price (cost_basis unknown)",
                              unmeasured)
    totals: dict[str, Any] = {
        key: _sum_or_none(runs, key, reason, unmeasured)
        for key in ("total_uncached_input_tokens", "total_cache_read_tokens", "total_cache_write_tokens",
                    "total_reasoning_tokens", "total_output_tokens", "total_wall_clock_ms")
    }
    totals["failed_attempt_count_total"] = _sum_or_none(runs, "failed_attempt_count", reason, unmeasured)
    if "failed_attempt_count" in unmeasured:  # mesma grandeza, nome do campo de resumo
        unmeasured["failed_attempt_count_total"] = unmeasured.pop("failed_attempt_count")
    rates = _rates(runs, outcomes, unmeasured)
    solved = outcomes.get(protocol.OUTCOME_SOLVED, 0)
    per_solved = total_tokens / solved if total_tokens is not None and solved > 0 else None
    if per_solved is None:
        unmeasured["core_tokens_partial_per_solved"] = (
            "requires all runs with measured usage and at least one SOLVED task")
    if config.git_head is None:
        unmeasured["git_head"] = "not provided (--git-head) and git unavailable in this environment"

    derived = _derived_metrics(runs, solved, tokens_lb, spent_lb, unmeasured)

    families: dict[str, Counter] = {}
    for r in runs:
        families.setdefault(r["family"], Counter())[r["outcome"]] += 1
    family_counts = {
        f: {k: families[f].get(k, 0) for k in (protocol.OUTCOME_SOLVED, protocol.OUTCOME_FAILURE,
                                               protocol.OUTCOME_INCONCLUSIVE)}
        for f in sorted(families)
    }
    registry = screening_registry_status(config.candidate_id)

    summary: dict[str, Any] = {
        "schema": BATCH_SCHEMA,
        "contract_id": protocol.CONTRACT_ID,
        "scope": SCOPE,
        "harness_variant": config.harness_variant,
        "reference_provenance": REFERENCE_PROVENANCE,
        "git_head": config.git_head,
        "model_requested": config.model,
        "request_timeout_s": config.request_timeout_s,
        "max_steps": config.max_steps,
        "max_cost_usd_batch": config.max_cost_usd,
        "spent_lower_bound_usd": spent_lb,
        "fail_fast": config.fail_fast,
        "suite_or_manifest": config.suite_or_manifest,
        "manifest_sha256": config.manifest_sha256,
        "total_tasks": planned,
        "executed": n,
        "aborted": aborted,
        "outcome_counts": {k: outcomes.get(k, 0) for k in (protocol.OUTCOME_SOLVED, protocol.OUTCOME_FAILURE,
                                                           protocol.OUTCOME_INCONCLUSIVE)},
        "verifier_verdict_counts": {k: verdicts.get(k, 0) for k in (protocol.VERDICT_PASS, protocol.VERDICT_FAIL,
                                                                    "NO_VERDICT")},
        "status_counts": dict(sorted(statuses.items())),
        "family_outcome_counts": family_counts,
        "total_steps": sum(r["steps"] for r in runs),
        "total_tokens": total_tokens,
        "tokens_lower_bound": tokens_lb,
        "total_cost_usd": total_cost,
        "cost_basis": protocol.run_cost_basis([r["cost_basis"] for r in runs]),
        # v4, regra a: INCONCLUSIVE de custo é contado à parte; oracle_pass não muda.
        "cost_accounting_status_counts": {
            k: sum(1 for r in runs if r["cost_accounting_status"] == k)
            for k in (protocol.COST_ACCOUNTING_MEASURED, protocol.COST_ACCOUNTING_INCONCLUSIVE)
        },
        "total_wall_clock_ms": totals["total_wall_clock_ms"],
        "total_output_tokens": totals["total_output_tokens"],
        "total_uncached_input_tokens": totals["total_uncached_input_tokens"],
        "total_cache_read_tokens": totals["total_cache_read_tokens"],
        "total_cache_write_tokens": totals["total_cache_write_tokens"],
        "total_reasoning_tokens": totals["total_reasoning_tokens"],
        "failed_attempt_count_total": totals["failed_attempt_count_total"],
        **rates,
        "core_tokens_partial_per_solved": per_solved,
        # Limites inferiores: somam só o que foi medido; rotulados como tal.
        **derived,
        "s0_qualification": qualify_s0(runs, totals, registry),
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
              secret: str | None, integrity_validated: bool = False) -> dict[str, Any]:
    """Executa ``tasks`` em ordem. Devolve o resumo (também gravado em ``batch-summary.json``).

    ``secret``: valor da chave (só em memória, nunca gravado). ``None`` desliga a varredura de vazamento.
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
                                    "total_tasks": len(tasks)})
        for task in tasks:
            spent = sum(r["total_cost_usd"] for r in runs if r["total_cost_usd"] is not None)
            if spent >= config.max_cost_usd:
                aborted = "batch_budget_exceeded"  # limite inferior conhecido já atingiu o teto do lote
                break
            cap = config.max_cost_usd - spent
            run_dir = runs_dir / task.task_id
            if run_dir.exists():
                raise FileExistsError(f"run directory already exists (use a clean --outdir): {task.task_id}")
            try:
                report = run_one(task, run_dir, make_llm(task), config, max_cost_usd=cap)
            except Exception as exc:  # noqa: BLE001 - bug do harness; registrado por tarefa, nunca silenciado
                report = harness_error_report(task, run_dir, exc, config, cap)
            leaks = scan_for_secret(run_dir, secret_bytes) if secret_bytes else []
            report["leak_clean"] = not leaks
            runs.append(report)
            bl.append("batch_run_finished", {"run": _brief(report, not leaks)})
            if leaks:
                aborted = "secret_leak"  # caminhos apenas; o valor nunca é impresso
                break
            if config.fail_fast and report["outcome"] != protocol.OUTCOME_SOLVED:
                aborted = f"fail_fast_on_{report['outcome']}"
                break

        summary = summarize(runs, planned=len(tasks), config=config, aborted=aborted)
        bl.append("batch_finished", {"summary": {k: summary[k] for k in (
            "total_tasks", "executed", "aborted", "outcome_counts", "verifier_verdict_counts", "total_tokens",
            "total_cost_usd", "pass_rate")}})

    from dv_harness.task_integrity import dataset_fingerprint  # import tardio: task_integrity importa batch

    summary["ledger_sha256"] = file_sha256(outdir / "batch-ledger.jsonl")  # bytes exatos, ledger já fechado
    summary["dataset_sha256"] = dataset_fingerprint(tasks)  # v4, regra d: rastreia o lote até o snapshot exato
    summary["integrity_validated"] = integrity_validated  # True somente quando o gate pré-lote passou

    batch_leaks = scan_for_secret(outdir, secret_bytes) if secret_bytes else []
    summary["leak_scan"] = {"performed": secret_bytes is not None,
                            "clean": (not batch_leaks) if secret_bytes else None,
                            "hit_paths": batch_leaks}
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if secret and secret in text:  # defesa final: o resumo nunca pode conter o segredo
        raise RuntimeError("secret found in batch summary; summary not written")
    (outdir / "batch-summary.json").write_text(text, encoding="utf-8")
    return summary


def ladder_step_runner(task: BatchTask, make_llm: Callable[[Any], Any], config: BatchConfig) -> Callable[..., Any]:
    """Adaptador do laddering: um degrau é uma execução do AgentLoop b0_plus no workspace limpo do controlador.

    ``make_llm(rung)`` devolve o cliente do degrau (modelo e esforço do degrau). O custo do degrau é o limite
    inferior medido (soma das chamadas com preço conhecido), nunca um valor estimado.
    """

    def run_step(rung: Any, workspace: Path, bash_logs: Path, cap_usd: float) -> StepResult:
        step_config = dataclasses.replace(config, model=rung.model, max_steps=rung.max_steps,
                                          harness_variant=rung.harness_variant)
        ledger_path = bash_logs.parent / "ledger.jsonl"
        with RunLedger(ledger_path) as ledger:
            result = run_agent_loop(task, workspace, make_llm(rung), step_config, max_cost_usd=cap_usd,
                                    ledger=ledger, bash_logs=bash_logs)
        known = [call.cost_usd for call in result.calls if call.cost_usd is not None]
        return StepResult(
            status=result.status,
            cost_lower_bound_usd=sum(known) if known else None,
            tokens_lower_bound=result.tokens_lower_bound,
            wall_clock_ms=int(result.total_wall_clock_ms or 0),
            telemetry_complete=result.telemetry_complete,
            ledger_sha256=file_sha256(ledger_path),
        )

    return run_step


def resolve_git_head(repo: Path) -> str | None:
    """HEAD do repositório, ou ``None`` se o git não estiver disponível (ex.: dentro do container)."""
    try:
        proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                              timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    head = proc.stdout.strip()
    return head if proc.returncode == 0 and SHA40_RE.fullmatch(head) else None


def select(tasks: list[BatchTask], limit: int | None) -> list[BatchTask]:
    if limit is None:
        return tasks
    if limit < 1:
        raise ValueError("--limit must be >= 1")
    return tasks[:limit]


__all__ = [
    "BATCH_SCHEMA", "BatchConfig", "BatchTask", "ManifestError", "SUITE_SCREENING_N10", "builtin_suite",
    "harness_error_report", "load_manifest", "qualify_s0", "resolve_git_head", "run_batch", "run_one",
    "scan_for_secret", "screening_registry_status", "select", "summarize", "suite_sha256", "verify",
]
