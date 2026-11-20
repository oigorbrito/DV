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


# --- protocolo v2: ambiente (B6), auditoria bruta (A6), linhas (B7), CRLF (B8), byte nulo (B9) ---

from dv_harness.tool_executor import ENV_ALLOWLIST, NON_INTERACTIVE_ENV, build_env, split_lines  # noqa: E402


@posix_only
def test_host_secrets_never_reach_the_subprocess(ex, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-leak-test-111")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-leak-222")
    monkeypatch.setenv("DV_PROBE_KEY", "sk-probe-333")
    r = ex.run_bash("env")
    assert r.ok
    for secret in ("sk-leak-test-111", "sk-ant-leak-222", "sk-probe-333", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        assert secret not in r.raw_output, secret
        assert secret not in r.output, secret


@posix_only
def test_allowlisted_and_non_interactive_vars_are_present(ex):
    r = ex.run_bash("env")
    names = {line.split("=", 1)[0] for line in r.raw_output.splitlines() if "=" in line}
    # Variáveis que o próprio sh cria (não herdadas do host) são permitidas; o resto deve ser allowlist.
    shell_generated = {"PWD", "OLDPWD", "SHLVL", "_"}
    assert set(names) <= set(ENV_ALLOWLIST) | set(NON_INTERACTIVE_ENV) | shell_generated
    assert "PATH" in names
    assert "PIP_PROGRESS_BAR" in names and "TQDM_DISABLE" in names


def test_build_env_is_strict_allowlist_plus_non_interactive():
    env = build_env({"PATH": "/bin", "HOME": "/h", "OPENAI_API_KEY": "x", "SECRET": "y", "LANG": "C"})
    assert set(env) == {"PATH", "HOME", "LANG"} | set(NON_INTERACTIVE_ENV)
    assert env["PIP_PROGRESS_BAR"] == "off" and env["TQDM_DISABLE"] == "1"
    assert env["PAGER"] == "cat" and env["MANPAGER"] == "cat" and env["GIT_PAGER"] == "cat" and env["LESS"] == "-R"


def test_output_is_truncated_for_model_but_raw_is_complete(tmp_path):
    small = ToolExecutor(tmp_path, max_output_chars=500)
    raw_text = "A" * 3000 + "MIDDLE_MARKER" + "B" * 3000
    (tmp_path / "big.txt").write_text(raw_text, encoding="utf-8")
    r = small.read_file("big.txt")
    assert r.truncated is True
    assert len(r.output) <= 500
    assert "MIDDLE_MARKER" in r.raw_output
    assert "MIDDLE_MARKER" not in r.output


@posix_only
def test_bash_raw_output_keeps_the_elided_middle(tmp_path):
    small = ToolExecutor(tmp_path, max_output_chars=300)
    r = small.run_bash("printf 'HEAD'; python3 -c 'print(\"M\"*2000)'; printf 'TAIL'")
    assert r.truncated is True
    assert "HEAD" in r.raw_output and "TAIL" in r.raw_output and "M" * 2000 in r.raw_output
    assert len(r.output) <= 300


def test_split_lines_uses_only_newline():
    assert split_lines("a\x0cb\nc\n") == ["a\x0cb", "c"]
    assert split_lines("x") == ["x"]
    assert split_lines("") == []
    assert split_lines("\n") == [""]
    assert split_lines("a\n\nb") == ["a", "", "b"]
    assert split_lines("a b\n") == ["a b"]


def test_read_file_numbering_matches_newline_count(tmp_path):
    (tmp_path / "ff.txt").write_bytes(b"a\x0cb\nc\n")
    r = ToolExecutor(tmp_path).read_file("ff.txt")
    assert r.output == "1\ta\x0cb\n2\tc"
    assert b"a\x0cb\nc\n".count(b"\n") == 2


def test_edit_crlf_file_with_lf_old_text_succeeds_and_keeps_crlf(ex, tmp_path):
    f = tmp_path / "crlf.py"
    f.write_bytes(b"def f():\r\n    return 1\r\n")
    r = ex.edit_file("crlf.py", "def f():\n    return 1", "def f():\n    return 2")
    assert r.ok, r.error
    assert f.read_bytes() == b"def f():\r\n    return 2\r\n"


def test_edit_crlf_file_untouched_lines_are_byte_identical(ex, tmp_path):
    f = tmp_path / "crlf2.txt"
    f.write_bytes(b"keep\r\nchange me\r\nkeep too\r\n")
    assert ex.edit_file("crlf2.txt", "change me", "changed").ok
    assert f.read_bytes() == b"keep\r\nchanged\r\nkeep too\r\n"


def test_edit_lf_file_behaviour_unchanged(ex, tmp_path):
    f = tmp_path / "lf.txt"
    f.write_bytes(b"one\ntwo\n")
    assert ex.edit_file("lf.txt", "two", "2").ok
    assert f.read_bytes() == b"one\n2\n"


def test_edit_mixed_line_endings_keeps_bytes_exact(ex, tmp_path):
    f = tmp_path / "mixed.txt"
    f.write_bytes(b"a\r\nb\nc\r\n")
    assert ex.edit_file("mixed.txt", "b", "B").ok
    assert f.read_bytes() == b"a\r\nB\nc\r\n"


def test_nul_byte_path_is_a_tool_result_not_an_exception(ex):
    for r in (ex.read_file("a\x00b"), ex.write_file("a\x00b", "x"), ex.edit_file("a\x00b", "x", "y")):
        assert r.ok is False
        assert "NUL" in r.error


@posix_only
def test_nul_byte_cwd_is_a_tool_result(ex):
    r = ex.run_bash("pwd", cwd="a\x00b")
    assert r.ok is False and "NUL" in r.error


def test_symlink_loop_is_a_tool_result(ex, tmp_path):
    loop = tmp_path / "loop"
    loop.symlink_to(loop)
    r = ex.read_file("loop")
    assert r.ok is False
    assert r.error is not None


@posix_only
def test_timeout_keeps_raw_output(tmp_path):
    small = ToolExecutor(tmp_path, max_output_chars=50)
    r = small.run_bash("printf 'before'; sleep 5", timeout=1)
    assert r.ok is False and r.exit_code is None
    assert "before" in r.raw_output


def test_name_too_long_is_a_tool_result_for_every_primitive(ex):
    # ENAMETOOLONG: Path.exists/is_dir levantariam OSError. A sonda de _resolve torna isso ToolResult.
    long_name = "a" * 3000
    for r in (ex.read_file(long_name), ex.write_file(long_name, "x"), ex.edit_file(long_name, "x", "y")):
        assert r.ok is False
        assert r.error is not None


# --- falhas de I/O viram ToolResult (contrato do módulo) --------------------------------

import errno as _errno  # noqa: E402


def _os_error(msg="disk on fire"):
    return OSError(_errno.EIO, msg)


def test_read_io_error_is_a_tool_result(ex, tmp_path, monkeypatch):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(_os_error()))
    r = ex.read_file("f.txt")
    assert r.ok is False and "read failed" in r.error


def test_write_io_error_is_a_tool_result(ex, monkeypatch):
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: (_ for _ in ()).throw(_os_error()))
    r = ex.write_file("new.txt", "x")
    assert r.ok is False and "write failed" in r.error


def test_edit_read_and_write_io_errors_are_tool_results(ex, tmp_path, monkeypatch):
    (tmp_path / "e.txt").write_text("abc\n", encoding="utf-8")
    monkeypatch.setattr(Path, "read_bytes", lambda self: (_ for _ in ()).throw(_os_error()))
    r1 = ex.edit_file("e.txt", "abc", "x")
    assert r1.ok is False and "read failed" in r1.error
    monkeypatch.undo()
    monkeypatch.setattr(Path, "write_text", lambda self, *a, **k: (_ for _ in ()).throw(_os_error()))
    r2 = ex.edit_file("e.txt", "abc", "x")
    assert r2.ok is False and "write failed" in r2.error


@posix_only
def test_popen_failure_is_a_tool_result(ex, monkeypatch):
    import subprocess as _sp

    def boom(*a, **k):
        raise OSError(_errno.ENOMEM, "cannot fork")

    monkeypatch.setattr(_sp, "Popen", boom)
    r = ex.run_bash("echo x")
    assert r.ok is False and "could not start command" in r.error


def test_truncate_rejects_negative_limit():
    with pytest.raises(ValueError):
        truncate_output("abc", -1)


def test_kill_group_falls_back_to_kill_when_group_is_gone():
    from dv_harness.tool_executor import _kill_group

    class FakeProc:
        pid = 999999999
        killed = False

        def kill(self):
            self.killed = True

    proc = FakeProc()
    _kill_group(proc)  # killpg num pid inexistente -> ProcessLookupError -> kill()
    assert proc.killed is True


def test_decode_handles_none_and_str():
    from dv_harness.tool_executor import _decode
    assert _decode(None) == ""
    assert _decode("já é texto") == "já é texto"
