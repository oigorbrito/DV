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
