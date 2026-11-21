"""Validador de integridade de tarefas, oráculos e higiene de ambiente. Fixtures locais, zero rede.

Executar: python -m pytest tests/test_task_integrity.py -v
"""

from __future__ import annotations

import json
import sys

import pytest

from support import ROOT  # noqa: F401
from dv_harness import batch
from dv_harness.batch import BatchTask
from dv_harness.task_integrity import (
    integrity_gate,
    integrity_table,
    validate_batch_integrity,
    validate_task_integrity,
)

sys.path.insert(0, str(ROOT / "scripts"))
import dv_batch_run as batch_cli  # noqa: E402
import dv_ladder_run as ladder_cli  # noqa: E402
import dv_validate_tasks as validate_cli  # noqa: E402

BUG = "def add(a, b):\n    return a - b\n"
FIX = "def add(a, b):\n    return a + b\n"
ORACLE = "from calc import add\n\nassert add(2, 3) == 5\nassert add(-1, 1) == 0\nprint('OK')\n"
SECRET = "sk-INTEGRITY-SECRET-0123456789abcdef"


def _task(task_id="t-ok", files=None, oracle=ORACLE, reference=None):
    return BatchTask(task_id=task_id, family="F1", statement="fix add",
                     files=files if files is not None else {"calc.py": BUG},
                     oracle=oracle, reference_files=reference if reference is not None else {"calc.py": FIX})


def _codes(report):
    return [v.code for v in report.violations]


def _validate(tmp_path, task, name="s"):
    return validate_task_integrity(task, scratch_dir=tmp_path / name)


# --- a) tarefa íntegra e determinismo ---------------------------------------------------------


def test_intact_task_passes_all_gates(tmp_path):
    report = _validate(tmp_path, _task())
    assert report.passed is True
    assert _codes(report) == []  # gold declarado: nenhum aviso de ausência


def test_fingerprints_are_deterministic_across_scratch_roots(tmp_path):
    first = validate_batch_integrity([_task()], scratch_root=tmp_path / "b1")
    second = validate_batch_integrity([_task()], scratch_root=tmp_path / "b2")
    assert first.dataset_sha256 == second.dataset_sha256
    assert first.task_reports[0].workspace_sha256 == second.task_reports[0].workspace_sha256
    assert first.task_reports[0].manifest_sha256 == second.task_reports[0].manifest_sha256


def test_dataset_sha_does_not_depend_on_task_order(tmp_path):
    a, b = _task("t-a"), _task("t-b", files={"calc.py": BUG + "# b\n"})
    forward = validate_batch_integrity([a, b], scratch_root=tmp_path / "f")
    backward = validate_batch_integrity([b, a], scratch_root=tmp_path / "r")
    assert forward.dataset_sha256 == backward.dataset_sha256


def test_workspace_hash_preserves_crlf_bytes(tmp_path):
    lf = _validate(tmp_path, _task(files={"calc.py": BUG}), "lf")
    crlf = _validate(tmp_path, _task(files={"calc.py": BUG.replace("\n", "\r\n")}), "crlf")
    assert lf.workspace_sha256 != crlf.workspace_sha256  # bytes exatos, não texto normalizado


def test_scratch_dir_is_never_the_original_dataset(tmp_path):
    original = tmp_path / "dataset"
    original.mkdir()
    (original / "calc.py").write_text(BUG, encoding="utf-8")
    before = (original / "calc.py").read_bytes()
    _validate(tmp_path, _task(), "isolated")
    assert (original / "calc.py").read_bytes() == before


# --- b) higiene de Git, vazamento de solução e isolamento do oráculo ----------------------------


def test_git_directory_in_exposed_workspace_is_a_git_leak(tmp_path):
    report = _validate(tmp_path, _task(files={"calc.py": BUG, ".git/HEAD": "ref: refs/heads/main\n"}))
    assert report.passed is False and "GIT_LEAK_DETECTED" in _codes(report)


def test_solution_file_name_in_exposed_workspace_is_a_leak(tmp_path):
    report = _validate(tmp_path, _task(files={"calc.py": BUG, "gold.patch": "--- a\n+++ b\n"}))
    assert report.passed is False and "SOLUTION_LEAK_DETECTED" in _codes(report)


def test_solution_marker_inside_file_content_is_a_leak(tmp_path):
    report = _validate(tmp_path, _task(files={"calc.py": BUG, "notes.txt": "see reference_patch for it"}))
    assert report.passed is False and "SOLUTION_LEAK_DETECTED" in _codes(report)


def test_oracle_text_copied_into_workspace_is_an_oracle_leak(tmp_path):
    report = _validate(tmp_path, _task(files={"calc.py": BUG, "notes.txt": ORACLE}))
    assert report.passed is False and "ORACLE_PATH_LEAK" in _codes(report)


# --- c) gate pré-patch -------------------------------------------------------------------------


def test_oracle_passing_on_initial_state_is_rejected(tmp_path):
    report = _validate(tmp_path, _task(files={"calc.py": FIX}))  # já está corrigido: tarefa trivial
    assert report.passed is False and "PRE_PATCH_ORACLE_PASSED" in _codes(report)


# --- d) gate pós-patch de referência -----------------------------------------------------------


def test_reference_patch_that_oracle_rejects_is_gold_failed(tmp_path):
    report = _validate(tmp_path, _task(reference={"calc.py": BUG}))  # "referência" não corrige nada
    assert report.passed is False and "GOLD_PATCH_FAILED" in _codes(report)


def test_reference_path_that_escapes_is_gold_invalid(tmp_path):
    report = _validate(tmp_path, _task(reference={"../escape.py": FIX}))
    assert report.passed is False and "GOLD_PATCH_INVALID" in _codes(report)


def test_missing_reference_is_informational_only(tmp_path):
    report = _validate(tmp_path, _task(reference={}))
    assert report.passed is True
    assert _codes(report) == ["GOLD_PATCH_ABSENT"]
    assert report.violations[0].severity == "info"


def test_duplicate_task_id_is_rejected_on_the_second_use(tmp_path):
    batch_report = validate_batch_integrity([_task("same"), _task("same")], scratch_root=tmp_path / "d")
    assert batch_report.passed is False
    assert batch_report.task_reports[0].passed is True
    assert "DUPLICATE_TASK_ID" in _codes(batch_report.task_reports[1])


def test_integrity_table_lists_failures_and_fingerprint(tmp_path):
    batch_report = validate_batch_integrity([_task(files={"calc.py": FIX})], scratch_root=tmp_path / "t")
    text = integrity_table(batch_report)
    assert "FAIL" in text and "PRE_PATCH_ORACLE_PASSED" in text
    assert f"dataset_sha256: {batch_report.dataset_sha256}" in text and "overall: FAIL" in text


# --- manifesto: reference_files é opcional e validado --------------------------------------------


def _manifest_dict(reference=None, files=None):
    entry = {"task_id": "t-m", "family": "F1", "statement": "fix add", "files": files or {"calc.py": BUG},
             "oracle": ORACLE}
    if reference is not None:
        entry["reference_files"] = reference
    return {"schema_id": "dv-batch-manifest-v1", "suite_id": "m", "tasks": [entry]}


def test_manifest_reference_files_are_parsed(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_manifest_dict(reference={"calc.py": FIX})), encoding="utf-8")
    _suite, tasks, _sha = batch.load_manifest(path)
    assert tasks[0].reference_files == {"calc.py": FIX}


def test_manifest_reference_with_unsafe_path_is_rejected(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps(_manifest_dict(reference={"../x.py": FIX})), encoding="utf-8")
    with pytest.raises(batch.ManifestError):
        batch.load_manifest(path)


def test_builtin_suite_passes_the_integrity_gate_without_references(tmp_path):
    report = validate_batch_integrity(batch.builtin_suite(), scratch_root=tmp_path / "builtin")
    assert report.passed is True  # cada oráculo reprova o estado inicial; referências são opcionais
    assert all(_codes(r) in ([], ["GOLD_PATCH_ABSENT"]) for r in report.task_reports)


# --- e) CLI scripts/dv_validate_tasks.py -------------------------------------------------------


def _write_manifest(directory, data, name="m.json"):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(data), encoding="utf-8")
    return directory


def test_validate_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as info:
        validate_cli.main(["--help"])
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "--tasks-dir" in out and "--output-json" in out and "--strict" in out


def test_validate_passing_manifest_exits_zero_and_writes_json(tmp_path, capsys):
    tasks = _write_manifest(tmp_path / "ok", _manifest_dict(reference={"calc.py": FIX}))
    out_json = tmp_path / "report.json"
    code = validate_cli.main(["--tasks-dir", str(tasks), "--output-json", str(out_json)])
    assert code == 0
    data = json.loads(out_json.read_text(encoding="utf-8"))
    assert data["passed"] is True and len(data["dataset_sha256"]) == 64
    assert "dataset_sha256:" in capsys.readouterr().out


def test_validate_failing_manifest_is_exit_one_when_strict(tmp_path):
    tasks = _write_manifest(tmp_path / "bad", _manifest_dict(files={"calc.py": FIX}))  # passa no pré-patch
    assert validate_cli.main(["--tasks-dir", str(tasks)]) == 1


def test_validate_failing_manifest_is_exit_zero_without_strict(tmp_path):
    tasks = _write_manifest(tmp_path / "bad", _manifest_dict(files={"calc.py": FIX}))
    assert validate_cli.main(["--tasks-dir", str(tasks), "--no-strict"]) == 0


def test_validate_does_not_modify_manifests(tmp_path):
    tasks = _write_manifest(tmp_path / "ok", _manifest_dict(reference={"calc.py": FIX}))
    before = (tasks / "m.json").read_bytes()
    validate_cli.main(["--tasks-dir", str(tasks)])
    assert (tasks / "m.json").read_bytes() == before


def test_validate_without_manifests_is_exit_two(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert validate_cli.main(["--tasks-dir", str(empty)]) == 2


def test_validate_malformed_manifest_is_exit_two(tmp_path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "m.json").write_text("{not json", encoding="utf-8")
    assert validate_cli.main(["--tasks-dir", str(bad)]) == 2


# --- gates integrados aos lançadores: nenhuma chamada de modelo se houver violação ----------------


def _policy_file(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(json.dumps({"policy_id": "p", "global_max_cost_usd": 0.5,
                                "rungs": [{"rung_id": "r0", "model": "m0"}]}), encoding="utf-8")
    return path


def test_ladder_blocks_before_any_model_call_on_integrity_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    tasks = _write_manifest(tmp_path / "bad", _manifest_dict(files={"calc.py": FIX}))
    out = tmp_path / "out"
    code = ladder_cli.main(["--policy", str(_policy_file(tmp_path)), "--tasks-dir", str(tasks),
                            "--output-dir", str(out)])
    captured = capsys.readouterr()
    assert code == 2 and not out.exists()
    assert "task integrity gate failed" in captured.out and SECRET not in captured.out


def test_batch_validate_flag_blocks_before_any_model_call(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    manifest = tmp_path / "bad.json"
    manifest.write_text(json.dumps(_manifest_dict(files={"calc.py": FIX})), encoding="utf-8")
    out = tmp_path / "out"
    code = batch_cli.main(["--outdir", str(out), "--manifest", str(manifest), "--validate-tasks"])
    captured = capsys.readouterr()
    assert code == 2 and not out.exists()
    assert "task integrity gate failed" in captured.out and SECRET not in captured.out


def test_integrity_gate_helper_returns_the_batch_report(tmp_path):
    report = integrity_gate([_task()])
    assert report.passed is True and len(report.dataset_sha256) == 64
