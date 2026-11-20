"""Testes do Módulo 2 (ToolExecutor). Sem chamada de API; usa tmp_path.

Executar a partir da raiz do repositório (com litellm/pytest instalados ou só pytest):
    python -m pytest tests/test_tool_executor.py -v

Os testes de bash exigem um shell POSIX (``sh``) e são pulados em outras plataformas.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dv_harness.tool_executor import (  # noqa: E402
    TOOL_SCHEMAS,
    ToolExecutor,
    ToolResult,
    truncate_output,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="requires POSIX shell")


@pytest.fixture
def ex(tmp_path):
    return ToolExecutor(tmp_path, max_output_chars=10_000)


# --- read_file ----------------------------------------------------------------


def test_read_file_numbers_lines(ex, tmp_path):
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
    r = ex.read_file("a.txt")
    assert r.ok and r.error is None and r.truncated is False
    assert r.output == "1\talpha\n2\tbeta\n3\tgamma"


def test_read_file_line_range_is_inclusive_one_based(ex, tmp_path):
    (tmp_path / "a.txt").write_text("1\n2\n3\n4\n5\n", encoding="utf-8")
    r = ex.read_file("a.txt", start_line=2, end_line=4)
    assert r.ok
    assert r.output == "2\t2\n3\t3\n4\t4"


def test_read_file_end_line_beyond_eof_is_clamped(ex, tmp_path):
    (tmp_path / "a.txt").write_text("x\ny\n", encoding="utf-8")
    assert ex.read_file("a.txt", start_line=2, end_line=99).output == "2\ty"


def test_read_file_missing_file_is_structured_error(ex):
    r = ex.read_file("nope.txt")
    assert r.ok is False
    assert "not found" in r.error
    assert r.output == ""


def test_read_file_directory_is_error(ex, tmp_path):
    (tmp_path / "d").mkdir()
    r = ex.read_file("d")
    assert r.ok is False and "directory" in r.error


def test_read_file_binary_is_refused(ex, tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x89PNG\r\n\x00\x00binary")
    r = ex.read_file("b.bin")
    assert r.ok is False and "binary" in r.error


def test_read_file_invalid_utf8_is_refused(ex, tmp_path):
    (tmp_path / "bad.txt").write_bytes(b"caf\xe9 no utf8 \xff")
    r = ex.read_file("bad.txt")
    assert r.ok is False and "UTF-8" in r.error


def test_read_file_rejects_invalid_range(ex, tmp_path):
    (tmp_path / "a.txt").write_text("x\n", encoding="utf-8")
    assert ex.read_file("a.txt", start_line=0).ok is False
    assert ex.read_file("a.txt", start_line=3, end_line=2).ok is False


def test_read_file_start_beyond_eof_is_error(ex, tmp_path):
    (tmp_path / "a.txt").write_text("x\ny\n", encoding="utf-8")
    r = ex.read_file("a.txt", start_line=10)
    assert r.ok is False and "beyond end" in r.error


def test_read_file_long_content_is_truncated(tmp_path):
    small = ToolExecutor(tmp_path, max_output_chars=200)
    (tmp_path / "big.txt").write_text("line of text\n" * 500, encoding="utf-8")
    r = small.read_file("big.txt")
    assert r.ok and r.truncated is True
    assert len(r.output) <= 200


# --- write_file ---------------------------------------------------------------


def test_write_file_creates_parent_directories(ex, tmp_path):
    r = ex.write_file("deep/nested/dir/new.txt", "hello\n")
    assert r.ok and r.error is None
    assert (tmp_path / "deep/nested/dir/new.txt").read_text(encoding="utf-8") == "hello\n"


def test_write_file_overwrites_existing(ex, tmp_path):
    (tmp_path / "f.txt").write_text("old", encoding="utf-8")
    assert ex.write_file("f.txt", "new").ok
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "new"


def test_write_file_onto_directory_is_error(ex, tmp_path):
    (tmp_path / "d").mkdir()
    r = ex.write_file("d", "x")
    assert r.ok is False and "directory" in r.error


def test_write_then_read_roundtrip_utf8(ex):
    text = "ação — 日本語\n"
    assert ex.write_file("u.txt", text).ok
    assert ex.read_file("u.txt").output == "1\tação — 日本語"


# --- edit_file ----------------------------------------------------------------


def test_edit_file_replaces_unique_block(ex, tmp_path):
    (tmp_path / "code.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    r = ex.edit_file("code.py", "return 1", "return 2")
    assert r.ok and r.error is None
    assert (tmp_path / "code.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_edit_file_multiline_block(ex, tmp_path):
    (tmp_path / "m.txt").write_text("a\nb\nc\n", encoding="utf-8")
    assert ex.edit_file("m.txt", "a\nb", "A\nB").ok
    assert (tmp_path / "m.txt").read_text(encoding="utf-8") == "A\nB\nc\n"


def test_edit_file_old_text_missing_fails_and_leaves_file_unchanged(ex, tmp_path):
    (tmp_path / "f.txt").write_text("keep me", encoding="utf-8")
    r = ex.edit_file("f.txt", "absent", "x")
    assert r.ok is False
    assert "not found" in r.error
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "keep me"


def test_edit_file_old_text_ambiguous_fails_and_leaves_file_unchanged(ex, tmp_path):
    (tmp_path / "f.txt").write_text("x = 1\nx = 1\n", encoding="utf-8")
    r = ex.edit_file("f.txt", "x = 1", "x = 2")
    assert r.ok is False
    assert "ambiguous" in r.error and "2 occurrences" in r.error
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "x = 1\nx = 1\n"


def test_edit_file_empty_old_text_is_rejected(ex, tmp_path):
    (tmp_path / "f.txt").write_text("abc", encoding="utf-8")
    r = ex.edit_file("f.txt", "", "x")
    assert r.ok is False and "empty" in r.error
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "abc"


def test_edit_file_missing_file_is_error(ex):
    r = ex.edit_file("missing.txt", "a", "b")
    assert r.ok is False and "not found" in r.error


def test_edit_file_binary_is_refused(ex, tmp_path):
    (tmp_path / "b.bin").write_bytes(b"\x00\x01abc")
    r = ex.edit_file("b.bin", "abc", "x")
    assert r.ok is False and "binary" in r.error


# --- run_bash -----------------------------------------------------------------


@posix_only
def test_run_bash_success_returns_output_and_zero_exit(ex):
    r = ex.run_bash("echo hello")
    assert r.ok is True
    assert r.exit_code == 0
    assert r.error is None
    assert r.output == "hello\n"


@posix_only
def test_run_bash_nonzero_exit_is_reported(ex):
    r = ex.run_bash("echo partial; exit 3")
    assert r.ok is False
    assert r.exit_code == 3
    assert r.error == "exit code 3"
    assert "partial" in r.output


@posix_only
def test_run_bash_captures_stderr_separately_labelled(ex):
    r = ex.run_bash("echo out; echo err 1>&2")
    assert r.ok is True
    assert "out" in r.output and "[stderr]" in r.output and "err" in r.output


@posix_only
def test_run_bash_timeout_is_structured_and_fast(ex):
    start = time.monotonic()
    r = ex.run_bash("sleep 10", timeout=1)
    elapsed = time.monotonic() - start
    assert r.ok is False
    assert r.exit_code is None
    assert r.error == "timeout after 1s"
    assert elapsed < 5, f"timeout did not stop the command promptly ({elapsed:.1f}s)"


@posix_only
def test_run_bash_timeout_kills_background_children(ex):
    # Filho em background escreve o próprio PID antes do timeout. Ele não pode sobreviver.
    r = ex.run_bash("sleep 60 & echo $!; wait", timeout=1)
    assert r.ok is False and r.exit_code is None
    pid = int(r.output.strip().splitlines()[0])
    time.sleep(0.3)
    assert not _alive(pid), f"background child {pid} survived the timeout"


def _alive(pid: int) -> bool:
    stat = Path(f"/proc/{pid}/stat")
    if not stat.exists():
        return False
    # Estado 'Z' (zumbi) já terminou; só falta ser colhido pelo pai.
    return stat.read_text().split(") ", 1)[1].split()[0] != "Z"


@posix_only
def test_run_bash_pager_is_non_interactive(ex):
    r = ex.run_bash('printf "%s" "$PAGER"')
    assert r.ok and r.output == "cat"


@posix_only
def test_run_bash_is_stateless_between_calls(ex):
    assert ex.run_bash("export DV_STATE_X=1").ok
    r = ex.run_bash('printf "%s" "${DV_STATE_X:-unset}"')
    assert r.output == "unset"


@posix_only
def test_run_bash_uses_cwd_argument(ex, tmp_path):
    (tmp_path / "sub").mkdir()
    r = ex.run_bash("pwd", cwd="sub")
    assert r.ok and r.output.strip().endswith(os.sep + "sub")


@posix_only
def test_run_bash_long_output_is_truncated_and_flagged(tmp_path):
    small = ToolExecutor(tmp_path, max_output_chars=500)
    r = small.run_bash("yes abcdefghij | head -n 5000")
    assert r.ok is True
    assert r.truncated is True
    assert len(r.output) <= 500
    assert "omitidos" in r.output


@posix_only
def test_run_bash_short_output_not_truncated(ex):
    r = ex.run_bash("echo short")
    assert r.truncated is False


def test_run_bash_rejects_nonpositive_timeout(ex):
    with pytest.raises(ValueError):
        ex.run_bash("echo x", timeout=0)


# --- truncate_output ----------------------------------------------------------


def test_truncate_output_keeps_head_and_tail_within_limit():
    text = "H" * 1000 + "T" * 1000
    out, flag = truncate_output(text, 300)
    assert flag is True
    assert len(out) <= 300
    assert out.startswith("H") and out.endswith("T")
    assert "omitidos" in out


def test_truncate_output_no_change_when_under_limit():
    assert truncate_output("abc", 10) == ("abc", False)


def test_truncate_output_tiny_limit_is_hard_cut():
    out, flag = truncate_output("x" * 100, 5)
    assert flag is True and len(out) == 5


# --- schemas and result type ---------------------------------------------------


def test_tool_schemas_cover_the_four_primitives():
    names = [s["function"]["name"] for s in TOOL_SCHEMAS]
    assert names == ["read_file", "write_file", "edit_file", "run_bash"]
    for schema in TOOL_SCHEMAS:
        assert schema["type"] == "function"
        params = schema["function"]["parameters"]
        assert params["type"] == "object"
        for req in params["required"]:
            assert req in params["properties"]


def test_tool_result_defaults():
    r = ToolResult(ok=True)
    assert (r.output, r.error, r.exit_code, r.truncated) == ("", None, None, False)


# --- trava de workspace (revisão de auditoria) ----------------------------------


def test_absolute_path_inside_root_is_allowed(ex, tmp_path):
    (tmp_path / "inside.txt").write_text("ok\n", encoding="utf-8")
    assert ex.read_file(str(tmp_path / "inside.txt")).ok


def test_absolute_path_outside_root_is_refused(ex, tmp_path):
    outside = tmp_path.parent / "outside_probe.txt"
    outside.write_text("secret", encoding="utf-8")
    try:
        r = ex.read_file(str(outside))
        assert r.ok is False and "escapes workspace root" in r.error
        assert r.output == ""
    finally:
        outside.unlink()


def test_traversal_with_dotdot_is_refused_for_write_and_edit(ex, tmp_path):
    escape_target = tmp_path.parent / "dv_escape_write.txt"
    if escape_target.exists():
        escape_target.unlink()
    r = ex.write_file("../dv_escape_write.txt", "pwned")
    assert r.ok is False and "escapes workspace root" in r.error
    assert not escape_target.exists()

    (tmp_path / "f.txt").write_text("abc", encoding="utf-8")
    r2 = ex.edit_file("../f.txt", "abc", "x")
    assert r2.ok is False and "escapes workspace root" in r2.error


def test_symlink_inside_root_pointing_outside_is_refused(ex, tmp_path):
    outside = tmp_path.parent / "dv_symlink_target.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(outside)
    try:
        r = ex.read_file("link.txt")
        assert r.ok is False and "escapes workspace root" in r.error
    finally:
        outside.unlink()


@posix_only
def test_run_bash_cwd_escape_is_refused(ex):
    r = ex.run_bash("pwd", cwd="/")
    assert r.ok is False and "escapes workspace root" in r.error
    r2 = ex.run_bash("pwd", cwd="../..")
    assert r2.ok is False and "escapes workspace root" in r2.error


@posix_only
def test_run_bash_cwd_root_itself_is_allowed(ex, tmp_path):
    r = ex.run_bash("pwd", cwd=".")
    assert r.ok is True
    assert Path(r.output.strip()).resolve() == tmp_path.resolve()


def test_workspace_root_is_resolved_once(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linkroot"
    link.symlink_to(real, target_is_directory=True)
    executor = ToolExecutor(link)
    assert executor.root == real.resolve()
    (real / "a.txt").write_text("hi\n", encoding="utf-8")
    assert executor.read_file("a.txt").ok
