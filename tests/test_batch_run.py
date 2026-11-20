"""Lote multi-tarefa (src/dv_harness/batch.py + scripts/dv_batch_run.py): contrato v3, gate e vazamento.

Sem rede: ``ScriptedLLM`` e um ``completion_fn`` falso. Executar: python -m pytest tests/test_batch_run.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from support import ROOT, ScriptedLLM, call, make_response, provider_failure, submit  # noqa: F401
from dv_harness import batch as b
from dv_harness import protocol as p

sys.path.insert(0, str(ROOT / "scripts"))
import dv_batch_run as cli  # noqa: E402

SECRET = "sk-TEST-SECRET-0123456789abcdef"

# Tarefas do screening-n10: 6 corrigidas (PASS), 3 sem correção (FAIL), 1 provider_error.
FIXES = {
    "sw01-add": ("calc.py", "def add(a, b):\n    return a + b\n"),
    "sw02-sub": ("calc.py", "def sub(a, b):\n    return a - b\n"),
    "sw03-mul": ("calc.py", "def mul(a, b):\n    return a * b\n"),
    "sw04-is-even": ("parity.py", "def is_even(n):\n    return n % 2 == 0\n"),
    "sw05-max2": ("order.py", "def max2(a, b):\n    return a if a > b else b\n"),
    "sw06-count-a": ("text.py", "def count_a(s):\n    return sum(1 for c in s if c == 'a')\n"),
}
PROVIDER_ERROR_TASK = "sw10-last"


def _script_for(task: b.BatchTask) -> list:
    if task.task_id in FIXES:
        name, source = FIXES[task.task_id]
        return [make_response(calls=[call("w1", "write_file", {"path": name, "content": source})]),
                make_response(calls=[submit("s1", "fixed the bug")])]
    if task.task_id == PROVIDER_ERROR_TASK:
        return [provider_failure("upstream unavailable")]
    return [make_response(calls=[submit("s1", "done")])]  # FAIL: declara sucesso sem corrigir


def _config(**over) -> b.BatchConfig:
    base = dict(model="mock", request_timeout_s=30.0, max_steps=8, max_cost_usd=0.10,
                continue_on_error=True, suite_id=b.SUITE_SCREENING_N10, manifest_sha256="0" * 64,
                git_head="a" * 40, api_key_env="GEMINI_API_KEY")
    base.update(over)
    return b.BatchConfig(**base)


def _run(tmp_path, tasks=None, *, make_llm=None, secret=SECRET, **over):
    outdir = tmp_path / "out"
    tasks = tasks if tasks is not None else b.builtin_suite()
    factory = make_llm or (lambda t: ScriptedLLM(_script_for(t)))
    return b.run_batch(tasks, outdir, config=_config(**over), make_llm=factory, secret=secret), outdir


# --- (a) lote N=10 com PASS, FAIL e provider_error --------------------------------------


def test_batch_of_ten_mixes_pass_fail_and_provider_error(tmp_path):
    summary, outdir = _run(tmp_path)

    assert summary["N"] == 10 and summary["executed"] == 10 and summary["aborted"] is None
    assert summary["outcome_counts"] == {"SOLVED": 6, "FAILURE": 3, "INCONCLUSIVE": 1}
    assert summary["verifier_verdict_counts"] == {"PASS": 6, "FAIL": 4, "NO_VERDICT": 0}
    assert summary["pass_rate"] == pytest.approx(6 / 10)
    assert summary["solved_rate"] == pytest.approx(6 / 10)
    # false finish: completed com FAILURE / completed com SOLVED ou FAILURE (provider_error fica fora)
    assert summary["false_finish_rate"] == pytest.approx(3 / 9)
    assert summary["git_head"] == "a" * 40 and summary["contract_id"] == p.CONTRACT_ID
    assert summary["leak_scan"] == {"performed": True, "clean": True, "hit_paths": []}

    for task in b.builtin_suite():
        run_dir = outdir / "runs" / task.task_id
        assert (run_dir / "workspace").is_dir() and (run_dir / "ledger.jsonl").is_file()
        assert (run_dir / "oracle" / "check.py").is_file()
        assert not (run_dir / "workspace" / "check.py").exists()  # oráculo fora do workspace
        result = json.loads((run_dir / "result.json").read_text(encoding="utf-8"))
        assert result["task_id"] == task.task_id and result["contract_id"] == p.CONTRACT_ID

    events = [json.loads(line) for line in (outdir / "batch-ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [e["event"] for e in events]
    assert kinds[0] == "batch_started" and kinds[-1] == "batch_finished"
    assert kinds.count("batch_run_finished") == 10
    assert SECRET not in (outdir / "batch-ledger.jsonl").read_text(encoding="utf-8")
    written = json.loads((outdir / "batch-summary.json").read_text(encoding="utf-8"))
    assert written["outcome_counts"] == summary["outcome_counts"]
    assert SECRET not in (outdir / "batch-summary.json").read_text(encoding="utf-8")


def test_provider_error_task_is_inconclusive_and_its_calls_are_closed(tmp_path):
    _run(tmp_path)
    ledger = [json.loads(x) for x in (tmp_path / "out" / "runs" / PROVIDER_ERROR_TASK / "ledger.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    kinds = [e["event"] for e in ledger]
    assert kinds.count("call_started") == kinds.count("call_finished") == 1
    result = json.loads((tmp_path / "out" / "runs" / PROVIDER_ERROR_TASK / "result.json").read_text(encoding="utf-8"))
    assert result["status"] == p.STATUS_PROVIDER_ERROR and result["outcome"] == p.OUTCOME_INCONCLUSIVE


def test_agent_cannot_forge_pass_by_rewriting_check_py_in_workspace(tmp_path):
    def forging(task):
        if task.task_id != "sw01-add":
            return ScriptedLLM([make_response(calls=[submit("s", "x")])])
        return ScriptedLLM([
            make_response(calls=[call("w1", "write_file", {"path": "check.py", "content": "print('OK')\n"})]),
            make_response(calls=[submit("s1", "forged")]),
        ])

    summary, outdir = _run(tmp_path, tasks=b.builtin_suite()[:1], make_llm=forging)
    assert summary["verifier_verdict_counts"]["FAIL"] == 1
    assert summary["outcome_counts"]["FAILURE"] == 1 and summary["outcome_counts"]["SOLVED"] == 0


# --- (b) contrato v3: null com motivo, nunca zero como sentinela -----------------------


def test_null_totals_have_reasons_and_are_not_zero(tmp_path):
    summary, _ = _run(tmp_path)
    assert summary["total_tokens"] is None  # o provider_error não mediu uso
    assert summary["total_cost_usd"] is None
    assert summary["cost_basis"] == p.COST_BASIS_UNKNOWN
    assert summary["core_tokens_partial_per_solved"] is None
    for key in ("total_tokens", "total_cost_usd", "core_tokens_partial_per_solved"):
        assert summary["unmeasured"][key]
    # limite inferior: soma só das chamadas medidas (9 runs com 1 ou 2 chamadas de 15 tokens)
    assert summary["tokens_lower_bound"] == 6 * 2 * 15 + 3 * 1 * 15


def test_all_measured_batch_reports_real_totals_and_no_unmeasured(tmp_path):
    tasks = b.builtin_suite()[:2]  # sw01 e sw02, ambos corrigidos
    summary, _ = _run(tmp_path, tasks=tasks, make_llm=lambda t: ScriptedLLM(_script_for(t)))
    assert summary["outcome_counts"]["SOLVED"] == 2
    assert summary["total_tokens"] == 2 * 2 * 15
    assert summary["total_cost_usd"] == pytest.approx(2 * 2 * 0.01)
    assert summary["cost_basis"] == p.COST_BASIS_PRICED
    assert summary["core_tokens_partial_per_solved"] == pytest.approx(60 / 2)
    assert "total_tokens" not in summary["unmeasured"]


def test_harness_error_is_recorded_per_task_and_batch_continues(tmp_path):
    def broken_first(task):
        if task.task_id == "sw01-add":
            class Boom:
                default_model = "mock"
                retry = None

                def complete(self, *a, **k):
                    raise ValueError("bug in the harness")

            return Boom()
        return ScriptedLLM([make_response(calls=[submit("s", "x")])])

    summary, _ = _run(tmp_path, tasks=b.builtin_suite()[:3], make_llm=broken_first)
    assert summary["executed"] == 3 and summary["aborted"] is None
    assert summary["status_counts"]["harness_error"] == 1
    assert summary["runs"][0]["status"] == "harness_error" and summary["runs"][0]["outcome"] == "INCONCLUSIVE"
    assert summary["total_tokens"] is None and summary["unmeasured"]["total_tokens"]


def test_harness_error_stops_batch_without_continue_on_error(tmp_path):
    class Boom:
        default_model = "mock"
        retry = None

        def complete(self, *a, **k):
            raise ValueError("bug")

    with pytest.raises(ValueError):
        _run(tmp_path, tasks=b.builtin_suite()[:2], make_llm=lambda t: Boom(), continue_on_error=False)


def test_no_continue_on_error_stops_at_first_non_solved(tmp_path):
    summary, _ = _run(tmp_path, continue_on_error=False)
    assert summary["executed"] == 7  # sw01..sw06 SOLVED; sw07 é o primeiro FAILURE
    assert summary["aborted"] == "stopped_on_FAILURE"


# --- (c) vazamento e gate de credenciais ----------------------------------------------


def test_secret_in_a_run_aborts_the_batch_and_is_never_written(tmp_path):
    def leaky(task):
        if task.task_id == "sw02-sub":
            return ScriptedLLM([make_response(calls=[submit("s", f"key is {SECRET}")])])
        return ScriptedLLM([make_response(calls=[submit("s", "fine")])])

    summary, outdir = _run(tmp_path, tasks=b.builtin_suite()[:5], make_llm=leaky)
    assert summary["aborted"] == "secret_leak" and summary["executed"] == 2
    assert summary["leak_scan"]["clean"] is False
    assert any("sw02-sub" in hit for hit in summary["leak_scan"]["hit_paths"])
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


# --- (d) --limit, --help e manifesto --------------------------------------------------


def test_limit_selects_first_n_tasks_in_order():
    tasks = b.builtin_suite()
    assert [t.task_id for t in b.select(tasks, 3)] == ["sw01-add", "sw02-sub", "sw03-mul"]
    assert len(b.select(tasks, None)) == 10
    assert len(b.select(tasks, 99)) == 10
    with pytest.raises(ValueError):
        b.select(tasks, 0)


def test_help_lists_the_batch_flags(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--manifest", "--outdir", "--model", "--api-key-env", "--request-timeout-s", "--max-steps",
                 "--max-cost-usd", "--continue-on-error", "--limit", "--git-head"):
        assert flag in out


def test_custom_manifest_loads_and_hashes_the_file(tmp_path):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({
        "schema_id": "dv-batch-manifest-v1", "suite_id": "custom-2",
        "tasks": [
            {"task_id": "t-one", "statement": "fix it", "files": {"m.py": "x = 1\n"}, "oracle": "print('OK')\n"},
            {"task_id": "t-two", "statement": "fix it", "files": {"n.py": "y = 1\n"}, "oracle": "print('OK')\n"},
        ],
    }), encoding="utf-8")
    suite_id, tasks, sha = b.load_manifest(manifest)
    assert suite_id == "custom-2" and [t.task_id for t in tasks] == ["t-one", "t-two"]
    assert len(sha) == 64


@pytest.mark.parametrize("bad_id", ["../escape", "a b", "", "x" * 65])
def test_manifest_rejects_unsafe_task_ids(tmp_path, bad_id):
    manifest = tmp_path / "m.json"
    manifest.write_text(json.dumps({
        "schema_id": "dv-batch-manifest-v1", "suite_id": "bad",
        "tasks": [{"task_id": bad_id, "statement": "s", "files": {"m.py": ""}, "oracle": "x"}],
    }), encoding="utf-8")
    with pytest.raises(b.ManifestError):
        b.load_manifest(manifest)


def test_unknown_suite_is_refused():
    with pytest.raises(b.ManifestError):
        b.builtin_suite("screening-n99")


def test_task_statement_and_oracle_are_not_in_workspace(tmp_path):
    task = b.builtin_suite()[0]
    run_dir = tmp_path / "r"
    b.run_one(task, run_dir, ScriptedLLM([make_response(calls=[submit("s", "x")])]), _config())
    workspace_files = {p.name for p in (run_dir / "workspace").iterdir()}
    assert workspace_files == {"calc.py"}
    assert Path(run_dir / "oracle" / "check.py").read_text(encoding="utf-8") == task.oracle
