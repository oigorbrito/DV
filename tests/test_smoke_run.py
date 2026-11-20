"""Gate do smoke test (scripts/dv_smoke_run.py): chave ausente, nome inválido, padrões. Sem chamada de modelo.

``main()`` sai com 2 antes de qualquer chamada ao provedor, então nenhum teste aqui gasta chave.
Executar: python -m pytest tests/test_smoke_run.py -v
"""

from __future__ import annotations

import sys

import pytest
from support import ROOT  # noqa: F401  (ajusta sys.path para src/)

sys.path.insert(0, str(ROOT / "scripts"))
import dv_smoke_run as smoke  # noqa: E402

SECRET = "AIza-test-SECRET-0123456789"


def test_defaults_are_unchanged():
    assert smoke.MODEL == "claude-haiku-5-5"
    assert smoke.KEY_VAR == "ANTHROPIC_API_KEY"


def test_missing_default_key_is_refused_with_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["dv_smoke_run.py", "--outdir", str(tmp_path)])
    assert smoke.main() == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["", None])
def test_empty_or_unset_gemini_key_is_refused(monkeypatch, tmp_path, capsys, value):
    if value is None:
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    else:
        monkeypatch.setenv("GEMINI_API_KEY", value)
    monkeypatch.setattr(sys, "argv", ["dv_smoke_run.py", "--outdir", str(tmp_path),
                                      "--model", "gemini/gemini-2.5-flash", "--api-key-env", "GEMINI_API_KEY"])
    assert smoke.main() == 2
    assert "GEMINI_API_KEY is not set" in capsys.readouterr().out


def test_gemini_key_missing_does_not_fall_back_to_anthropic(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["dv_smoke_run.py", "--outdir", str(tmp_path),
                                      "--api-key-env", "GEMINI_API_KEY"])
    assert smoke.main() == 2


def test_key_value_passed_as_name_is_refused_and_never_echoed(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    monkeypatch.setattr(sys, "argv", ["dv_smoke_run.py", "--outdir", str(tmp_path), "--api-key-env", SECRET])
    assert smoke.main() == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out and SECRET not in captured.err


def test_help_does_not_need_any_key(monkeypatch, capsys):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(sys, "argv", ["dv_smoke_run.py", "--help"])
    with pytest.raises(SystemExit) as info:
        smoke.main()
    assert info.value.code == 0
    out = capsys.readouterr().out
    assert "--model" in out and "--api-key-env" in out
