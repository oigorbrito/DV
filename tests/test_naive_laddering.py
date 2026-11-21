"""Naïve Laddering guiado por oráculo externo (src/dv_harness/laddering.py) e sua CLI.

Zero rede: o executor de degrau e o oráculo são mocks determinísticos. Executar:
python -m pytest tests/test_naive_laddering.py -v
"""

from __future__ import annotations

import json
import sys

import pytest

from support import ROOT, ScriptedLLM, make_response, submit  # noqa: F401
from dv_harness import batch
from dv_harness.laddering import (
    LadderController,
    LadderPolicy,
    LadderPolicyError,
    LadderRung,
    StepResult,
)

sys.path.insert(0, str(ROOT / "scripts"))
import dv_ladder_run as cli  # noqa: E402

BUG = "def add(a, b):\n    return a - b\n"
FIX = "def add(a, b):\n    return a + b\n"
SECRET = "sk-LADDER-SECRET-0123456789abcdef"


def _snapshot(tmp_path):
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "calc.py").write_text(BUG, encoding="utf-8")
    return snap


def _oracle(workspace):  # oráculo externo: o arquivo corrigido, nada que o agente diga
    return (workspace / "calc.py").read_text(encoding="utf-8") == FIX


def _fix(cost):
    def run(ws):
        (ws / "calc.py").write_text(FIX, encoding="utf-8")
        return StepResult("completed", cost, 100, 10)
    return run


def _wrong(status, cost):
    def run(ws):  # não corrige: o agente pode declarar sucesso, mas o oráculo rejeita
        return StepResult(status, cost, 50, 5)
    return run


def _junk_and_claim(cost):  # altera o arquivo, cria lixo e declara sucesso (falso positivo)
    def run(ws):
        (ws / "calc.py").write_text("changed by rung zero", encoding="utf-8")
        (ws / "junk.txt").write_text("leftover", encoding="utf-8")
        return StepResult("completed", cost, 80, 7)
    return run


def _must_not_run(ws):
    pytest.fail("this rung must not be invoked")


class Script:
    """Executor de degrau determinístico. Registra o estado inicial de cada degrau e o teto recebido."""

    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    def __call__(self, rung, workspace, bash_logs, cap_usd):
        self.calls.append({
            "rung": rung.rung_id,
            "cap": cap_usd,
            "start": (workspace / "calc.py").read_text(encoding="utf-8"),
            "files": sorted(p.name for p in workspace.iterdir()),
            "bash_logs": bash_logs,
        })
        return self.plan[rung.rung_id](workspace)


def _policy(rungs=None, global_max=1.0):
    rungs = rungs or (LadderRung("r0", "model-zero"), LadderRung("r1", "model-one", reasoning_effort="high"))
    return LadderPolicy(policy_id="pilot", global_max_cost_usd=global_max, rungs=tuple(rungs))


def _run(tmp_path, policy, plan, oracle=_oracle):
    runner = Script(plan)
    controller = LadderController(policy, step_runner=runner, oracle_verifier=oracle,
                                  snapshot_dir=_snapshot(tmp_path), work_root=tmp_path / "work")
    return controller.run("t1"), runner


# --- a) resolução logo no Rung 0 ------------------------------------------------------------


def test_solved_on_rung_zero_never_invokes_rung_one(tmp_path):
    result, runner = _run(tmp_path, _policy(), {"r0": _fix(0.1), "r1": _must_not_run})
    assert result.final_outcome == "SOLVED" and result.resolved_at_rung == "r0"
    assert result.escalation_count == 0
    assert result.total_cost_lower_bound_usd == pytest.approx(0.1)  # só o custo do Rung 0
    assert [c["rung"] for c in runner.calls] == ["r0"]
    assert len(result.rung_traces) == 1


# --- b) falso positivo: escalona com workspace limpo e soma os custos -------------------------


def test_false_positive_escalates_to_clean_workspace_and_sums_costs(tmp_path):
    result, runner = _run(tmp_path, _policy(), {"r0": _junk_and_claim(0.2), "r1": _fix(0.3)})
    assert result.final_outcome == "SOLVED" and result.resolved_at_rung == "r1"
    assert result.escalation_count == 1
    assert result.total_cost_lower_bound_usd == pytest.approx(0.5)  # 0.2 + 0.3
    rung_one = runner.calls[1]
    assert rung_one["start"] == BUG  # o patch sujo do Rung 0 não chega ao Rung 1
    assert rung_one["files"] == ["calc.py"]  # nem o arquivo novo criado pelo Rung 0
    assert result.rung_traces[0].rung_status == "failure" and result.rung_traces[0].oracle_pass is False
    assert result.rung_traces[1].oracle_pass is True


# --- c) falha em ambos os degraus ---------------------------------------------------------------


def test_failure_on_both_rungs_is_failure_with_no_resolution(tmp_path):
    result, _runner = _run(tmp_path, _policy(),
                           {"r0": _wrong("completed", 0.2), "r1": _wrong("completed", 0.2)})
    assert result.final_outcome == "FAILURE" and result.resolved_at_rung is None
    assert result.escalation_count == 1
    assert result.total_cost_lower_bound_usd == pytest.approx(0.4)


# --- d) corte por orçamento global --------------------------------------------------------------


def test_global_budget_exhaustion_blocks_escalation_before_calling_provider(tmp_path):
    policy = _policy(global_max=0.5)
    result, runner = _run(tmp_path, policy, {"r0": _wrong("completed", 0.5), "r1": _must_not_run})
    assert result.final_outcome == "FAILURE" and result.resolved_at_rung is None
    assert result.escalation_count == 0
    assert [c["rung"] for c in runner.calls] == ["r0"]  # r1 nunca foi chamado
    assert runner.calls[0]["cap"] == pytest.approx(0.5)


def test_per_rung_cap_is_share_of_global_and_never_exceeds_the_remainder(tmp_path):
    rungs = (LadderRung("r0", "m0", cost_share_cap=0.5), LadderRung("r1", "m1"))
    result, runner = _run(tmp_path, _policy(rungs=rungs, global_max=1.0),
                          {"r0": _wrong("completed", 0.3), "r1": _fix(0.2)})
    assert [c["cap"] for c in runner.calls] == [pytest.approx(0.5), pytest.approx(0.7)]
    assert result.final_outcome == "SOLVED"


# --- e) status não escalável e propagação de INCONCLUSIVE --------------------------------------


def test_non_escalable_status_stops_with_failure(tmp_path):
    result, runner = _run(tmp_path, _policy(), {"r0": _wrong("budget_exceeded", 0.1), "r1": _must_not_run})
    assert result.final_outcome == "FAILURE" and result.escalation_count == 0
    assert [c["rung"] for c in runner.calls] == ["r0"]


def test_provider_error_on_last_rung_is_inconclusive(tmp_path):
    result, _runner = _run(tmp_path, _policy(),
                           {"r0": _wrong("completed", 0.1), "r1": _wrong("provider_error", None)})
    assert result.final_outcome == "INCONCLUSIVE"
    assert result.escalation_count == 1
    assert result.total_cost_lower_bound_usd == pytest.approx(0.1)  # custo desconhecido não vira zero


def test_budget_unverifiable_on_last_rung_is_inconclusive(tmp_path):
    result, _runner = _run(tmp_path, _policy(),
                           {"r0": _wrong("completed", 0.1), "r1": _wrong("budget_unverifiable", None)})
    assert result.final_outcome == "INCONCLUSIVE"


def test_single_rung_policy_never_escalates(tmp_path):
    result, _runner = _run(tmp_path, _policy(rungs=(LadderRung("only", "m"),)), {"only": _wrong("completed", 0.1)})
    assert result.final_outcome == "FAILURE" and result.escalation_count == 0


# --- f) parser de LadderPolicy e validação ------------------------------------------------------


def _valid_dict():
    return {
        "policy_id": "pilot",
        "global_max_cost_usd": 0.5,
        "rungs": [
            {"rung_id": "r0", "model": "gemini/gemini-3.1-flash-lite"},
            {"rung_id": "r1", "model": "gemini/gemini-3.1-flash-lite", "reasoning_effort": "high",
             "max_steps": 12, "cost_share_cap": 0.5, "escalate_on_statuses": ["failure", "provider_error"]},
        ],
    }


def test_valid_policy_dict_parses_with_defaults():
    policy = LadderPolicy.from_dict(_valid_dict())
    assert policy.state_carryover == "clean_workspace"
    assert policy.rungs[0].harness_variant == "b0_plus" and policy.rungs[0].max_steps == 8
    assert policy.rungs[0].escalate_on_statuses == ("failure", "max_steps_reached", "error_limit_reached",
                                                    "provider_error")
    assert policy.rungs[1].escalate_on_statuses == ("failure", "provider_error")
    assert policy.rungs[1].reasoning_effort == "high"


def test_policy_file_roundtrip_and_invalid_json(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps(_valid_dict()), encoding="utf-8")
    assert LadderPolicy.from_json_file(good).policy_id == "pilot"
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(LadderPolicyError):
        LadderPolicy.from_json_file(bad)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(rungs=[]),
    lambda d: d.update(rungs="r0"),
    lambda d: d.update(global_max_cost_usd=0),
    lambda d: d.update(global_max_cost_usd=-1.0),
    lambda d: d.update(global_max_cost_usd=True),
    lambda d: d.update(state_carryover="shared_workspace"),
    lambda d: d.update(unexpected_key=1),
    lambda d: d.update(policy_id=""),
    lambda d: d["rungs"][1].update(rung_id="r0"),
    lambda d: d["rungs"][0].update(rung_id="../escape"),
    lambda d: d["rungs"][0].update(cost_share_cap=1.5),
    lambda d: d["rungs"][0].update(cost_share_cap=0),
    lambda d: d["rungs"][0].update(max_steps=0),
    lambda d: d["rungs"][0].update(harness_variant="b9"),
    lambda d: d["rungs"][0].update(unknown_field=True),
    lambda d: d["rungs"][0].pop("model"),
    lambda d: d["rungs"][0].update(model="   "),
    lambda d: d["rungs"][0].update(reasoning_effort=""),
])
def test_invalid_policies_are_rejected(mutate):
    data = _valid_dict()
    mutate(data)
    with pytest.raises(LadderPolicyError):
        LadderPolicy.from_dict(data)


def test_controller_rejects_missing_snapshot(tmp_path):
    with pytest.raises(LadderPolicyError):
        LadderController(_policy(), step_runner=Script({}), oracle_verifier=_oracle,
                         snapshot_dir=tmp_path / "missing", work_root=tmp_path / "work")


# --- adaptador: um degrau é uma execução do AgentLoop b0_plus no workspace limpo ----------------


def test_ladder_step_runner_runs_the_agent_loop_on_the_workspace(tmp_path):
    task = batch.builtin_suite()[0]  # f1-greet
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "greet.py").write_text(task.files["greet.py"], encoding="utf-8")
    bash_logs = tmp_path / "rung-00" / "bash_logs"
    bash_logs.mkdir(parents=True)
    config = batch.BatchConfig(model="unused", request_timeout_s=30.0, max_steps=8, max_cost_usd=0.5,
                               fail_fast=False, suite_or_manifest="t", manifest_sha256="0" * 64,
                               git_head=None, api_key_env="K")
    rung = LadderRung("r0", "scripted", max_steps=4)
    runner = batch.ladder_step_runner(
        task, lambda r: ScriptedLLM([make_response(calls=[submit("s1", "done")])]), config)
    step = runner(rung, workspace, bash_logs, 0.5)
    assert step.status == "completed"
    assert step.cost_lower_bound_usd == pytest.approx(0.01)  # ScriptedLLM mede 0.01 por chamada
    assert step.tokens_lower_bound == 15
    assert (bash_logs.parent / "ledger.jsonl").exists()  # ledger fora do workspace


# --- CLI: --help e gate sem execução ------------------------------------------------------------


def test_help_lists_the_ladder_flags(capsys):
    with pytest.raises(SystemExit) as info:
        cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--policy", "--tasks-dir", "--output-dir", "--api-key-env", "--request-timeout-s", "--limit"):
        assert flag in out


def _policy_file(tmp_path, data=None):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(data if data is not None else _valid_dict()), encoding="utf-8")
    return path


def _tasks_dir(tmp_path):
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    manifest = {"schema_id": "dv-batch-manifest-v1", "suite_id": "one", "tasks": [{
        "task_id": "t-one", "family": "F1", "statement": "fix it", "files": {"m.py": "x = 1\n"},
        "oracle": "print('OK')\n"}]}
    (tasks / "m.json").write_text(json.dumps(manifest), encoding="utf-8")
    return tasks


def test_invalid_policy_exits_two_and_creates_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    bad = _policy_file(tmp_path, {"policy_id": "x", "global_max_cost_usd": 0, "rungs": []})
    out = tmp_path / "out"
    code = cli.main(["--policy", str(bad), "--tasks-dir", str(_tasks_dir(tmp_path)), "--output-dir", str(out)])
    captured = capsys.readouterr()
    assert code == 2 and "invalid policy" in captured.out and not out.exists()
    assert SECRET not in captured.out


def test_missing_key_exits_two_before_any_execution(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    out = tmp_path / "out"
    code = cli.main(["--policy", str(_policy_file(tmp_path)), "--tasks-dir", str(_tasks_dir(tmp_path)),
                     "--output-dir", str(out)])
    assert code == 2 and not out.exists()
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().out


def test_nonempty_output_dir_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    out = tmp_path / "out"
    out.mkdir()
    (out / "old.txt").write_text("previous", encoding="utf-8")
    code = cli.main(["--policy", str(_policy_file(tmp_path)), "--tasks-dir", str(_tasks_dir(tmp_path)),
                     "--output-dir", str(out)])
    assert code == 2 and "not empty" in capsys.readouterr().out


def test_empty_tasks_dir_is_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    empty = tmp_path / "empty"
    empty.mkdir()
    code = cli.main(["--policy", str(_policy_file(tmp_path)), "--tasks-dir", str(empty),
                     "--output-dir", str(tmp_path / "out")])
    assert code == 2 and "no *.json manifests" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--request-timeout-s", "0"], ["--request-timeout-s", "nan"],
                                  ["--limit", "0"], ["--api-key-env", "lower"]])
def test_invalid_numeric_or_name_parameters_exit_two(tmp_path, monkeypatch, capsys, argv):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    code = cli.main(["--policy", str(_policy_file(tmp_path)), "--tasks-dir", str(_tasks_dir(tmp_path)),
                     "--output-dir", str(tmp_path / "out"), *argv])
    assert code == 2 and SECRET not in capsys.readouterr().out
