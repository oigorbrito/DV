"""Falsificação do próprio detector de canário (I3b). O teste de canário precisa ser capaz de falhar.

Executar: python -m pytest tests/test_canary.py -v
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from support import ROOT  # noqa: F401
from dv_harness.tool_executor import ToolExecutor, ToolResult

_spec = importlib.util.spec_from_file_location("dv_canary_check", ROOT / "scripts" / "dv_canary_check.py")
canary = importlib.util.module_from_spec(_spec)
sys.modules["dv_canary_check"] = canary
_spec.loader.exec_module(canary)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell required")
def test_detector_reports_contained_when_run_bash_refuses(tmp_path, monkeypatch):
    # Executor contido de verdade: nenhum comando é executado, nenhum canário muda.
    monkeypatch.setattr(ToolExecutor, "run_bash",
                        lambda self, command, timeout=30, cwd=None: ToolResult(ok=False, error="refused"))
    report = canary.run(tmp_path)
    assert report["I3b_holds"] is True
    assert all(a["canary_changed"] is False for a in report["attempts"])


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell required")
def test_detector_reports_escape_for_unconfined_run_bash(tmp_path):
    # Executor real, sem confinamento de comando: o detector precisa acusar as cinco vias de escape.
    report = canary.run(tmp_path)
    assert report["I3b_holds"] is False
    assert {a["attempt"] for a in report["attempts"] if a["canary_changed"]} == {"rel", "abs", "cd", "symlink", "python"}


# --- modo read-only-external (isca pré-provisionada e fora do alcance de escrita) ----


def test_provision_writes_all_five_baits_with_original_content(tmp_path):
    written = canary.provision(tmp_path / "ctrl")
    assert {p.name for p in written} == set(canary.CANARY_NAMES)
    assert all(p.read_text(encoding="utf-8") == canary.ORIGINAL for p in written)


def test_read_only_control_refuses_to_run_without_provisioned_baits(tmp_path):
    with pytest.raises(canary.ControlNotProvisioned):
        canary.run(tmp_path / "wd", control_dir=tmp_path / "empty")


def test_read_only_control_refuses_altered_bait(tmp_path):
    ctrl = tmp_path / "ctrl"
    canary.provision(ctrl)
    (ctrl / "canary_abs.txt").write_text("already changed\n", encoding="utf-8")
    with pytest.raises(canary.ControlNotProvisioned):
        canary.run(tmp_path / "wd", control_dir=ctrl)


def test_main_returns_2_when_control_is_not_provisioned(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dv_canary_check.py", "--workdir", str(tmp_path / "wd"),
                                      "--control-dir", str(tmp_path / "empty")])
    assert canary.main() == 2


def test_read_only_control_is_contained_when_every_write_is_refused(tmp_path, monkeypatch):
    # Simula o efeito do mount somente leitura: toda escrita falha, a isca fica íntegra.
    ctrl = tmp_path / "ctrl"
    canary.provision(ctrl)
    monkeypatch.setattr(ToolExecutor, "run_bash",
                        lambda self, command, timeout=30, cwd=None: ToolResult(ok=False, error="Read-only file system"))
    report = canary.run(tmp_path / "wd", control_dir=ctrl)
    assert report["I3b_holds"] is True
    assert report["control_mode"] == "read-only-external"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell required")
def test_read_only_control_detector_still_flags_escape_when_control_is_writable(tmp_path):
    # Contraste: com controle gravável, o mesmo detector acusa as cinco vias. Prova que ele pode falhar.
    ctrl = tmp_path / "ctrl"
    canary.provision(ctrl)
    report = canary.run(tmp_path / "wd", control_dir=ctrl)
    assert report["I3b_holds"] is False
    assert report["control_dir_writable_by_this_process"] is True
