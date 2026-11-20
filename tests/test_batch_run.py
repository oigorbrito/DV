"""Lote multi-tarefa (src/dv_harness/batch.py + scripts/dv_batch_run.py): contrato v3, S0, gate e vazamento.

Sem rede: ``ScriptedLLM``; a CLI é testada com ``LLMClient`` substituído. Executar: python -m pytest tests/test_batch_run.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from support import ROOT, ScriptedLLM, call, make_response, provider_failure, submit  # noqa: F401
from dv_harness import batch as b
from dv_harness import llm_client as lc
from dv_harness import protocol as p

sys.path.insert(0, str(ROOT / "scripts"))
import dv_batch_run as cli  # noqa: E402

SECRET = "sk-TEST-SECRET-0123456789abcdef"
SOLVED_REF = {  # solução de referência: só para os scripts de teste e a validade do conjunto
    "f1-greet": {"greet.py": 'def greet(name):\n    return "Hello, " + name + "!"\n'},
    "f1-clamp": {"clamp.py": "def clamp(x, lo, hi):\n    return max(lo, min(x, hi))\n"},
    "f2-pricing": {
        "shop/fees.py": "SERVICE_FEE = 0.1\n\n\ndef apply_fee(value):\n    return round(value * (1 + SERVICE_FEE), 2)\n",
        "shop/cart.py": "from shop.fees import apply_fee\n\n\ndef total(items):\n    return apply_fee(sum(items))\n",
    },
    "f2-grades": {
        "grades/scale.py": ("def to_letter(score):\n    if score >= 90:\n        return 'A'\n    if score >= 80:\n"
                            "        return 'B'\n    if score >= 70:\n        return 'C'\n    return 'F'\n"),
        "grades/report.py": "from grades.scale import to_letter\n\n\ndef letters(scores):\n"
                            "    return [to_letter(s) for s in scores]\n",
    },
    "f3-percent": {"pct.py": ("def parse_percent(text):\n    value = text.strip()\n    if value.endswith('%'):\n"
                              "        value = value[:-1].strip()\n    if not value:\n"
                              "        raise ValueError('empty percent')\n    return float(value) / 100\n")},
    "f3-bytes": {"sizes.py": ("def human_size(n):\n    if n < 1024:\n        return f'{n} B'\n"
                              "    units = ['KB', 'MB', 'GB']\n    value = float(n)\n    unit = None\n"
                              "    for unit in units:\n        value /= 1024\n"
                              "        if value < 1024 or unit == units[-1]:\n            break\n"
                              "    return f'{value:.1f} {unit}'\n")},
    "f4-roman": {"tests/test_roman.py": (
        "from roman import to_roman\n\ncases = {1: 'I', 3: 'III', 4: 'IV', 9: 'IX', 14: 'XIV', 40: 'XL', 58: 'LVIII',\n"
        "         90: 'XC', 400: 'CD', 900: 'CM', 1000: 'M', 1994: 'MCMXCIV', 3999: 'MMMCMXCIX'}\n"
        "for number, numeral in cases.items():\n    assert to_roman(number) == numeral, (number, to_roman(number))\n"
        "print('OK')\n")},
    "f4-leap": {"tests/test_leap.py": (
        "from leap import is_leap\n\ncases = {2024: True, 2023: False, 1900: False, 2000: True, 1996: True}\n"
        "for year, expected in cases.items():\n    assert is_leap(year) is expected, year\nprint('OK')\n")},
    "f5-money": {"report.py": ('def _money(value):\n    return f"${value:,.2f}"\n\n\ndef invoice_line(name, amount):\n'
                               '    return f"{name}: " + _money(amount)\n\n\ndef receipt_total(amount, tax):\n'
                               '    return "TOTAL " + _money(amount + tax)\n')},
    "f6-cli-config": {"cli.py": (
        "import argparse\nimport json\nimport os\nimport sys\n\n"
        "HERE = os.path.dirname(os.path.abspath(__file__))\n\n\n"
        "def main(argv):\n    with open(os.path.join(HERE, 'settings.json'), encoding='utf-8') as fh:\n"
        "        settings = json.load(fh)\n    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('--repeat', type=int, default=settings['repeat'])\n"
        "    args = parser.parse_args(argv)\n    for _ in range(args.repeat):\n"
        "        print(settings['greeting'])\n    return 0\n\n\n"
        "if __name__ == '__main__':\n    sys.exit(main(sys.argv[1:]))\n")},
}
UNSOLVED = {"f5-money", "f6-cli-config"}  # o agente declara sucesso sem mudar nada -> FAILURE
PROVIDER_ERROR_TASK = "f4-leap"  # a primeira chamada falha -> provider_error -> INCONCLUSIVE


def _script(task_id: str) -> list:
    if task_id == PROVIDER_ERROR_TASK:
        return [provider_failure("upstream unavailable")]
    if task_id in UNSOLVED:
        return [make_response(calls=[submit("s1", "done")])]
    steps = [make_response(calls=[call(f"w{i}", "write_file", {"path": path, "content": content})])
             for i, (path, content) in enumerate(SOLVED_REF[task_id].items())]
    return steps + [make_response(calls=[submit("s1", "fixed the bug")])]


def _config(**over) -> b.BatchConfig:
    base = dict(model="mock", request_timeout_s=30.0, max_steps=8, max_cost_usd=0.50, fail_fast=False,
                suite_or_manifest=b.SUITE_SCREENING_N10, manifest_sha256="0" * 64, git_head="a" * 40,
                api_key_env="GEMINI_API_KEY")
    base.update(over)
    return b.BatchConfig(**base)


def _run(tmp_path, tasks=None, *, make_llm=None, secret=SECRET, **over):
    outdir = tmp_path / "out"
    tasks = tasks if tasks is not None else b.builtin_suite()
    factory = make_llm or (lambda t: ScriptedLLM(_script(t.task_id)))
    return b.run_batch(tasks, outdir, config=_config(**over), make_llm=factory, secret=secret), outdir


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# --- suíte embutida: validade do conjunto -----------------------------------------------


def test_suite_has_ten_tasks_covering_f1_to_f6():
    tasks = b.builtin_suite()
    assert len(tasks) == 10
    assert {t.family for t in tasks} == set(b.FAMILIES)
    assert len({t.task_id for t in tasks}) == 10


@pytest.mark.parametrize("task", b.builtin_suite(), ids=lambda t: t.task_id)
def test_each_oracle_fails_initially_and_passes_with_reference_solution(tmp_path, task):
    ws = tmp_path / "ws"
    for name, content in task.files.items():
        (ws / name).parent.mkdir(parents=True, exist_ok=True)
        (ws / name).write_text(content, encoding="utf-8")
    oracle = tmp_path / "oracle.py"
    oracle.write_text(task.oracle, encoding="utf-8")
    assert b.verify(oracle, ws) == p.VERDICT_FAIL  # o estado inicial não pode passar
    for name, content in SOLVED_REF[task.task_id].items():
        (ws / name).parent.mkdir(parents=True, exist_ok=True)
        (ws / name).write_text(content, encoding="utf-8")
    assert b.verify(oracle, ws) == p.VERDICT_PASS  # a referência tem de passar


# --- (a) lote N=10 com SOLVED, FAILURE e provider_error ---------------------------------


def test_batch_of_ten_mixes_solved_failure_and_provider_error(tmp_path):
    summary, outdir = _run(tmp_path)

    assert summary["total_tasks"] == 10 and summary["executed"] == 10 and summary["aborted"] is None
    assert summary["outcome_counts"] == {"SOLVED": 7, "FAILURE": 2, "INCONCLUSIVE": 1}
    assert summary["verifier_verdict_counts"] == {"PASS": 7, "FAIL": 3, "NO_VERDICT": 0}
    assert summary["pass_rate"] == pytest.approx(7 / 9)  # SOLVED / (SOLVED + FAILURE): INCONCLUSIVE fora
    assert summary["inconclusive_count"] == 1 and summary["verdict_available_count"] == 10
    assert "INCONCLUSIVE runs excluded" in summary["pass_rate_definition"]
    assert summary["solved_rate"] == pytest.approx(7 / 10)  # INCONCLUSIVE conta como não resolvido
    assert summary["false_finish_rate"] == pytest.approx(2 / 9)  # FAILURE / (SOLVED + FAILURE)
    assert summary["family_outcome_counts"]["F4"] == {"SOLVED": 1, "FAILURE": 0, "INCONCLUSIVE": 1}
    assert summary["family_outcome_counts"]["F5"]["FAILURE"] == 1
    assert summary["git_head"] == "a" * 40 and summary["contract_id"] == p.CONTRACT_ID
    assert summary["scope"]["executor_ranking"] == "FORBIDDEN"
    assert summary["leak_scan"] == {"performed": True, "clean": True, "hit_paths": []}

    for task in b.builtin_suite():
        run_dir = outdir / "runs" / task.task_id
        assert (run_dir / "workspace").is_dir() and (run_dir / "ledger.jsonl").is_file()
        assert (run_dir / "oracle" / "check.py").is_file()
        assert not (run_dir / "workspace" / "check.py").exists()  # oráculo fora do workspace
        assert _read(run_dir / "result.json")["task_id"] == task.task_id

    events = [json.loads(x) for x in (outdir / "batch-ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "batch_started" and kinds[-1] == "batch_finished"
    assert kinds.count("batch_run_finished") == 10
    assert SECRET not in (outdir / "batch-ledger.jsonl").read_text(encoding="utf-8")
    assert SECRET not in (outdir / "batch-summary.json").read_text(encoding="utf-8")
    assert _read(outdir / "batch-summary.json")["outcome_counts"] == summary["outcome_counts"]


def test_contract_v3_strict_aggregation_null_with_reason_never_zero(tmp_path):
    summary, outdir = _run(tmp_path)
    successful_calls = sum(
        1 for t in b.builtin_suite() for c in _read(outdir / "runs" / t.task_id / "result.json")["calls"]
        if c["total_tokens"] is not None)
    assert successful_calls == 18  # 16 nas tarefas SOLVED e 2 nas FAILURE; provider_error não mede uso
    assert summary["tokens_lower_bound"] == 18 * 15  # soma só das chamadas medidas (prompt 10 + completion 5)
    assert summary["spent_lower_bound_usd"] == pytest.approx(18 * 0.01)
    # o provider_error não mediu uso: total e custo não podem ser 0 nem parciais
    assert summary["total_tokens"] is None and summary["unmeasured"]["total_tokens"]
    assert summary["total_cost_usd"] is None and summary["unmeasured"]["total_cost_usd"]
    assert summary["cost_basis"] == p.COST_BASIS_UNKNOWN
    assert summary["core_tokens_partial_per_solved"] is None
    # sub-baldes que o provedor não expõe (Gemini via litellm): null com motivo
    for key in ("total_uncached_input_tokens", "total_cache_read_tokens", "total_reasoning_tokens"):
        assert summary[key] is None and summary["unmeasured"][key]


def test_s0_is_telemetry_limited_when_buckets_are_null(tmp_path):
    summary, _ = _run(tmp_path)
    s0 = summary["s0_qualification"]
    assert s0["status"] == "S0_READY_TELEMETRY_LIMITED"
    assert s0["treatment_eligible"] is False  # lote não é evidência de qualificação
    assert any("no qualification_evidence_ref" in r for r in s0["reasons"])
    assert s0["registry"]["registry_status"] == "NOT_PROVIDED"


@pytest.mark.parametrize("totals, runs, expected", [
    ({"total_uncached_input_tokens": 1, "total_cache_read_tokens": 2, "total_reasoning_tokens": 3},
     [{"verifier_verdict": "PASS", "outcome": "SOLVED", "error_types": []}], "S0_READY"),
    ({"total_uncached_input_tokens": None, "total_cache_read_tokens": 2, "total_reasoning_tokens": 3},
     [{"verifier_verdict": "PASS", "outcome": "SOLVED", "error_types": []}], "S0_READY_TELEMETRY_LIMITED"),
    ({"total_uncached_input_tokens": 1, "total_cache_read_tokens": 2, "total_reasoning_tokens": 3},
     [{"verifier_verdict": "FAIL", "outcome": "FAILURE", "error_types": []}], "S0_FAIL"),
    ({"total_uncached_input_tokens": 1, "total_cache_read_tokens": 2, "total_reasoning_tokens": 3},
     [{"verifier_verdict": None, "outcome": "INCONCLUSIVE", "error_types": ["RuntimeError"]}],
     "S0_ENVIRONMENT_BLOCKED"),
    ({"total_uncached_input_tokens": None, "total_cache_read_tokens": None, "total_reasoning_tokens": None},
     [{"verifier_verdict": None, "outcome": "INCONCLUSIVE", "error_types": ["AuthenticationError"]}],
     "S0_CREDENTIAL_BLOCKED"),
])
def test_s0_classification_rules(totals, runs, expected):
    registry = {"candidate_id": None, "registry_status": "NOT_PROVIDED"}
    assert b.qualify_s0(runs, totals, registry)["status"] == expected


def test_candidate_registry_is_read_only_and_matches_screening_file():
    reg = b.screening_registry_status("HF-NEX-N2.5-MINI")
    assert reg["registry_status"] == "REGISTERED" and reg["screening_status"] == "DISCOVERED_NOT_QUALIFIED"
    assert b.screening_registry_status("GEMINI-3.1-FLASH-LITE")["registry_status"] == "NOT_IN_REGISTRY"


def test_provider_error_task_is_inconclusive_and_its_calls_are_closed(tmp_path):
    _run(tmp_path)
    run_dir = tmp_path / "out" / "runs" / PROVIDER_ERROR_TASK
    kinds = [e["event"] for e in map(json.loads, (run_dir / "ledger.jsonl").read_text(encoding="utf-8").splitlines())]
    assert kinds.count("call_started") == kinds.count("call_finished") == 1
    result = _read(run_dir / "result.json")
    assert result["status"] == p.STATUS_PROVIDER_ERROR and result["outcome"] == p.OUTCOME_INCONCLUSIVE


def test_agent_cannot_forge_pass_by_rewriting_check_py_in_workspace(tmp_path):
    def forging(task):
        if task.task_id != "f1-greet":
            return ScriptedLLM([make_response(calls=[submit("s", "x")])])
        return ScriptedLLM([
            make_response(calls=[call("w1", "write_file", {"path": "check.py", "content": "print('OK')\n"})]),
            make_response(calls=[submit("s1", "forged")]),
        ])

    summary, _ = _run(tmp_path, tasks=b.builtin_suite()[:1], make_llm=forging)
    assert summary["verifier_verdict_counts"]["FAIL"] == 1
    assert summary["outcome_counts"]["FAILURE"] == 1 and summary["outcome_counts"]["SOLVED"] == 0


# --- nulos e orçamento do lote -----------------------------------------------------------


def test_all_measured_batch_reports_real_totals(tmp_path):
    tasks = [t for t in b.builtin_suite() if t.task_id in ("f1-greet", "f1-clamp")]
    summary, _ = _run(tmp_path, tasks=tasks)
    assert summary["outcome_counts"]["SOLVED"] == 2
    assert summary["total_tokens"] == 2 * 2 * 15
    assert summary["total_cost_usd"] == pytest.approx(2 * 2 * 0.01)
    assert summary["cost_basis"] == p.COST_BASIS_PRICED
    assert summary["core_tokens_partial_per_solved"] == pytest.approx(60 / 2)
    assert "total_tokens" not in summary["unmeasured"]


def test_batch_budget_stops_before_next_task_on_lower_bound(tmp_path):
    summary, _ = _run(tmp_path, max_cost_usd=0.02)  # f1-greet gasta 0.02 (2 chamadas) -> teto atingido
    assert summary["executed"] == 1 and summary["aborted"] == "batch_budget_exceeded"
    assert summary["runs"][0]["task_cost_cap_usd"] == pytest.approx(0.02)


def test_per_task_cap_is_what_is_left_of_the_batch_ceiling(tmp_path):
    summary, _ = _run(tmp_path, tasks=b.builtin_suite()[:2], max_cost_usd=0.03)
    assert summary["runs"][0]["task_cost_cap_usd"] == pytest.approx(0.03)
    assert summary["runs"][1]["task_cost_cap_usd"] == pytest.approx(0.01)


def test_harness_error_is_recorded_and_batch_continues_by_default(tmp_path):
    class Boom:
        default_model = "mock"

        def complete(self, *a, **k):
            raise ValueError("bug in the harness")

    def broken_first(task):
        return Boom() if task.task_id == "f1-greet" else ScriptedLLM(_script(task.task_id))

    summary, _ = _run(tmp_path, tasks=b.builtin_suite()[:3], make_llm=broken_first)
    assert summary["executed"] == 3 and summary["aborted"] is None
    assert summary["status_counts"]["harness_error"] == 1
    first = summary["runs"][0]
    assert first["status"] == "harness_error" and first["outcome"] == "INCONCLUSIVE"
    assert summary["tokens_lower_bound"] == (2 + 3) * 15  # f1-clamp (2 chamadas) + f2-pricing (3); f1-greet falhou no harness
    assert summary["total_tokens"] is None and summary["unmeasured"]["total_tokens"]


def test_fail_fast_stops_at_first_non_solved_outcome(tmp_path):
    summary, _ = _run(tmp_path, fail_fast=True)
    assert summary["executed"] == 7  # seis SOLVED e a 7a tarefa (f4-leap) é provider_error
    assert summary["aborted"] == "fail_fast_on_INCONCLUSIVE"


# --- (c) vazamento e gate de credenciais ------------------------------------------------


def test_secret_in_a_run_aborts_the_batch_and_is_never_written(tmp_path):
    def leaky(task):
        if task.task_id == "f1-clamp":
            return ScriptedLLM([make_response(calls=[submit("s", f"key is {SECRET}")])])
        return ScriptedLLM([make_response(calls=[submit("s", "fine")])])

    summary, outdir = _run(tmp_path, tasks=b.builtin_suite()[:5], make_llm=leaky)
    assert summary["aborted"] == "secret_leak" and summary["executed"] == 2
    assert summary["leak_scan"]["clean"] is False
    assert any("f1-clamp" in hit for hit in summary["leak_scan"]["hit_paths"])
    assert summary["runs"][1]["leak_clean"] is False
    assert SECRET not in (outdir / "batch-summary.json").read_text(encoding="utf-8")
    assert SECRET not in (outdir / "batch-ledger.jsonl").read_text(encoding="utf-8")


def test_scan_for_secret_reports_paths_only(tmp_path):
    (tmp_path / "a.txt").write_text(f"x {SECRET} y", encoding="utf-8")
    (tmp_path / "b.txt").write_text("clean", encoding="utf-8")
    assert b.scan_for_secret(tmp_path, SECRET.encode()) == ["a.txt"]


def test_empty_secret_is_refused(tmp_path):
    with pytest.raises(ValueError):
        _run(tmp_path, secret="")


def test_missing_key_is_refused_with_exit_2_and_nothing_is_created(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    out = tmp_path / "out"
    code = cli.main(["--outdir", str(out), "--api-key-env", "GEMINI_API_KEY", "--suite", "screening-n10"])
    assert code == 2 and not out.exists()
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().out


def test_empty_key_is_refused_without_echo(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert cli.main(["--outdir", str(tmp_path / "o"), "--api-key-env", "GEMINI_API_KEY"]) == 2
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().out


def test_key_value_passed_as_name_is_refused_and_never_echoed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    assert cli.main(["--outdir", str(tmp_path / "o"), "--api-key-env", SECRET]) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out and SECRET not in captured.err


def test_non_empty_outdir_is_refused(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    (tmp_path / "old.txt").write_text("previous run", encoding="utf-8")
    assert cli.main(["--outdir", str(tmp_path)]) == 2
    assert "not empty" in capsys.readouterr().out


def test_s2_candidate_wave_is_refused_as_not_executable(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    wave = ROOT / "experiments" / "p1" / "s2-candidate-wave-01.json"
    assert cli.main(["--outdir", str(tmp_path / "o"), "--manifest", str(wave)]) == 2
    assert "discovery-only" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["--request-timeout-s", "0"], ["--request-timeout-s", "nan"], ["--max-steps", "0"],
    ["--max-cost-usd", "0"], ["--limit", "0"], ["--api-key-env", "lower"],
])
def test_invalid_numeric_or_name_parameters_exit_2(monkeypatch, tmp_path, capsys, argv):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    assert cli.main(["--outdir", str(tmp_path / "o"), *argv]) == 2
    assert SECRET not in capsys.readouterr().out


# --- CLI ponta a ponta com LLMClient substituído (sem rede) -----------------------------


def _fake_client(monkeypatch, script_fn):
    monkeypatch.setattr(lc, "LLMClient", lambda **_kw: script_fn())


def test_cli_limit_3_runs_only_three_tasks(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    _fake_client(monkeypatch, lambda: ScriptedLLM([make_response(calls=[submit("s", "done")])]))
    out = tmp_path / "out"
    code = cli.main(["--outdir", str(out), "--api-key-env", "GEMINI_API_KEY", "--limit", "3",
                     "--git-head", "b" * 40, "--candidate-id", "HF-NEX-N2.5-MINI"])
    assert code == 0
    summary = _read(out / "batch-summary.json")
    assert summary["total_tasks"] == 3 and summary["executed"] == 3
    assert [r["task_id"] for r in summary["runs"]] == ["f1-greet", "f1-clamp", "f2-pricing"]
    assert summary["s0_qualification"]["registry"]["registry_status"] == "REGISTERED"
    assert SECRET not in capsys.readouterr().out


def test_cli_fail_fast_stops_on_first_failure(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    _fake_client(monkeypatch, lambda: ScriptedLLM([make_response(calls=[submit("s", "done")])]))
    out = tmp_path / "out"
    assert cli.main(["--outdir", str(out), "--api-key-env", "GEMINI_API_KEY", "--fail-fast",
                     "--git-head", "b" * 40]) == 0
    summary = _read(out / "batch-summary.json")
    assert summary["executed"] == 1 and summary["aborted"] == "fail_fast_on_FAILURE"


def test_cli_secret_in_output_exits_1_and_key_is_not_printed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    _fake_client(monkeypatch, lambda: ScriptedLLM([make_response(calls=[submit("s", f"leak {SECRET}")])]))
    code = cli.main(["--outdir", str(tmp_path / "out"), "--api-key-env", "GEMINI_API_KEY", "--limit", "2",
                     "--git-head", "b" * 40])
    captured = capsys.readouterr()
    assert code == 1 and "LEAK" in captured.out
    assert SECRET not in captured.out and SECRET not in captured.err


def test_help_lists_batch_flags_and_not_the_removed_continue_flag(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--suite", "--manifest", "--outdir", "--model", "--api-key-env", "--request-timeout-s",
                 "--max-steps", "--max-cost-usd", "--fail-fast", "--limit", "--candidate-id", "--git-head"):
        assert flag in out
    assert "--continue-on-error" not in out


def test_limit_selects_first_n_tasks_in_order():
    tasks = b.builtin_suite()
    assert [t.task_id for t in b.select(tasks, 3)] == ["f1-greet", "f1-clamp", "f2-pricing"]
    assert len(b.select(tasks, None)) == 10 and len(b.select(tasks, 99)) == 10
    with pytest.raises(ValueError):
        b.select(tasks, 0)


# --- manifesto (campos alinhados ao run-spec-v1.template.json) --------------------------


def _manifest(tmp_path, task_overrides=None, **top):
    entry = {"task_id": "t-one", "family": "F1", "statement": "fix it", "files": {"pkg/m.py": "x = 1\n"},
             "oracle": "print('OK')\n"}
    entry.update(task_overrides or {})
    data = {"schema_id": "dv-batch-manifest-v1", "suite_id": "custom", "tasks": [entry], **top}
    path = tmp_path / "m.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_custom_manifest_loads_with_family_hash_and_subdirectory_files(tmp_path):
    path = _manifest(tmp_path, {"task_statement_sha256": b.BatchTask("x", "F1", "fix it", {}, "").statement_sha256,
                                "verifier_ref": "v1", "base_sha": "a" * 40})
    suite_id, tasks, sha = b.load_manifest(path)
    assert suite_id == "custom" and tasks[0].family == "F1" and tasks[0].verifier_ref == "v1"
    assert "pkg/m.py" in tasks[0].files and len(sha) == 64


@pytest.mark.parametrize("override, message", [
    ({"family": "F9"}, "family"),
    ({"task_statement_sha256": "0" * 64}, "does not match"),
    ({"base_sha": "XYZ"}, "base_sha"),
    ({"files": {"../escape.py": "x"}}, "invalid workspace"),
    ({"files": {"/abs.py": "x"}}, "invalid workspace"),
])
def test_manifest_rejects_invalid_entries(tmp_path, override, message):
    with pytest.raises(b.ManifestError, match=message):
        b.load_manifest(_manifest(tmp_path, override))


@pytest.mark.parametrize("bad_id", ["../escape", "a b", "", "x" * 65])
def test_manifest_rejects_unsafe_task_ids(tmp_path, bad_id):
    with pytest.raises(b.ManifestError):
        b.load_manifest(_manifest(tmp_path, {"task_id": bad_id}))


def test_unknown_suite_is_refused():
    with pytest.raises(b.ManifestError):
        b.builtin_suite("screening-n99")


def test_task_workspace_has_only_visible_files_and_oracle_is_outside(tmp_path):
    task = b.builtin_suite()[0]
    run_dir = tmp_path / "r"
    b.run_one(task, run_dir, ScriptedLLM([make_response(calls=[submit("s", "x")])]), _config())
    assert {x.name for x in (run_dir / "workspace").iterdir()} == {"greet.py"}
    assert Path(run_dir / "oracle" / "check.py").read_text(encoding="utf-8") == task.oracle


# --- pass_rate, contadores e métricas derivadas (auditoria dos baldes) -------------------


def test_pass_rate_is_none_with_reason_when_no_verified_run(tmp_path):
    summary, _ = _run(tmp_path, tasks=[t for t in b.builtin_suite() if t.task_id == PROVIDER_ERROR_TASK])
    assert summary["outcome_counts"]["INCONCLUSIVE"] == 1
    assert summary["pass_rate"] is None and summary["unmeasured"]["pass_rate"]


def test_patch_stats_per_run_and_aggregate_from_workspace_diff(tmp_path):
    tasks = [t for t in b.builtin_suite() if t.task_id in ("f1-greet", "f5-money")]
    summary, outdir = _run(tmp_path, tasks=tasks)
    greet = _read(outdir / "runs" / "f1-greet" / "result.json")["patch_stats"]
    # 'return "Hello " + name' -> 'return "Hello, " + name + "!"': uma linha removida e uma adicionada
    assert greet == {"files_changed": 1, "lines_added": 1, "lines_removed": 1}
    assert _read(outdir / "runs" / "f5-money" / "result.json")["patch_stats"] == {
        "files_changed": 0, "lines_added": 0, "lines_removed": 0}
    agg = summary["patch_stats"]
    assert agg["measured_runs"] == 2 and agg["unmeasured_runs"] == 0
    assert agg["files_changed_total"] == 1 and agg["lines_added_total"] == 1 and agg["lines_removed_total"] == 1


def test_mixed_batch_derived_metrics_are_labelled_lower_bounds(tmp_path):
    summary, _ = _run(tmp_path)
    assert summary["tokens_lower_bound_per_solved"] == pytest.approx(18 * 15 / 7)
    assert summary["cost_lower_bound_usd_per_solved"] == pytest.approx(18 * 0.01 / 7)
    assert summary["cost_coverage_rate"] == pytest.approx(18 / 19)  # 19 chamadas; a do provider_error sem preço
    assert summary["tool_stats"]["total_tool_calls"] > 0  # inclui o evento de submit
    fk = summary["tool_stats"]["failure_kind_counts"]
    assert set(fk) == {"OK", "DOMAIN_FAILURE", "PROTOCOL_ERROR"}
    assert summary["tool_stats"]["tool_error_rate"] == 0.0  # só submit/write bem-sucedidos nesta suíte


def test_tool_error_rate_counts_domain_failures(tmp_path):
    def reads_missing_then_submits(task):
        if task.task_id != "f1-greet":
            return ScriptedLLM([make_response(calls=[submit("s", "x")])])
        return ScriptedLLM([
            make_response(calls=[call("r1", "read_file", {"path": "nope.py"})]),
            make_response(calls=[submit("s1", "gave up")]),
        ])

    summary, _ = _run(tmp_path, tasks=b.builtin_suite()[:1], make_llm=reads_missing_then_submits)
    stats = summary["tool_stats"]
    assert stats["total_tool_calls"] == 2
    assert stats["failure_kind_counts"]["DOMAIN_FAILURE"] == 1
    assert stats["tool_error_rate"] == pytest.approx(1 / 2)


def test_wall_clock_stats_are_nearest_rank_percentiles(tmp_path):
    assert b._percentile([1, 2, 3, 4], 0.5) == 2
    assert b._percentile([1, 2, 3, 4], 0.95) == 4
    assert b._percentile([], 0.5) is None
    summary, _ = _run(tmp_path, tasks=b.builtin_suite()[:2])
    stats = summary["wall_clock_ms_stats"]
    assert stats["run_p50"] is not None and stats["run_p95"] >= stats["run_p50"]
    assert stats["call_p50"] is not None and stats["call_p95"] >= stats["call_p50"]


def test_null_derived_metrics_have_reasons(tmp_path):
    summary, _ = _run(tmp_path, tasks=[t for t in b.builtin_suite() if t.task_id == PROVIDER_ERROR_TASK])
    assert summary["tokens_lower_bound_per_solved"] is None and summary["unmeasured"]["tokens_lower_bound_per_solved"]
    assert summary["cost_lower_bound_usd_per_solved"] is None and summary["unmeasured"]["cost_lower_bound_usd_per_solved"]
    assert summary["spent_lower_bound_usd"] is None  # o provider_error não tem custo conhecido: nunca 0
