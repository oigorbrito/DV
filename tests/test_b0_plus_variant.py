"""Variante experimental b0_plus (opt-in) e invariância do modo padrão b0.

Sem rede e sem modelo real: ``ScriptedLLM`` e execução local de comandos triviais.
Executar: python -m pytest tests/test_b0_plus_variant.py -v
"""

from __future__ import annotations

import hashlib
import json
import sys

import pytest

from support import ROOT, ScriptedLLM, call, make_response, submit  # noqa: F401
from dv_harness import protocol as p
from dv_harness import variants
from dv_harness.agent_loop import SYSTEM_ADVISORY, AgentLoop, DoomLoopTracker
from dv_harness.context_manager import DEFAULT_PLACEHOLDER, ContextManager
from dv_harness.ledger import RunLedger
from dv_harness.tool_executor import ToolExecutor, _apply_edit_with_fallback, truncate_output, truncate_tail_priority

B0_PLUS = "b0_plus"


def _executor(root, variant=B0_PLUS, **kw):
    return ToolExecutor(root, harness_variant=variant, **kw)


# --- (a) edit_file: 3 estágios, unicidade, terminação e diff ---------------------------


def test_stage_exact_when_substring_is_unique():
    updated, stage, err = _apply_edit_with_fallback("def f():\n    return 1\n", "    return 1\n", "    return 2\n", "f.py")
    assert err is None and stage == "exact"
    assert updated == "def f():\n    return 2\n"


def test_stage_line_trimmed_matches_trailing_whitespace():
    text = "x = 1   \ny = 2\n"
    updated, stage, err = _apply_edit_with_fallback(text, "x = 1\n", "x = 9\n", "f.py")
    assert err is None and stage == "line_trimmed"
    assert updated == "x = 9\ny = 2\n"


def test_stage_indentation_flexible_matches_wrong_indentation_and_reindents_new_text():
    # Arquivo com recuo 4; o modelo enviou old e new sem recuo. O bloco novo sobe para o recuo do arquivo.
    text = "    if a:\n        b()\n"
    updated, stage, err = _apply_edit_with_fallback(text, "if a:\n    b()\n", "if a:\n    c()\n", "f.py")
    assert err is None and stage == "indentation_flexible"
    assert updated == "    if a:\n        c()\n"


def test_ambiguous_exact_match_is_rejected_with_clear_error(tmp_path):
    (tmp_path / "f.py").write_text("a = 1\na = 1\n", encoding="utf-8")
    result = _executor(tmp_path).edit_file("f.py", "a = 1\n", "a = 2\n")
    assert result.ok is False and "ambiguous" in result.error and "2 matches" in result.error
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "a = 1\na = 1\n"  # arquivo intacto


def test_ambiguous_fallback_match_is_rejected_not_silently_applied():
    text = "a()  \nz\na()\t\nz\n"  # exact 'a()\\n' não casa; line_trimmed casa em duas linhas
    updated, stage, err = _apply_edit_with_fallback(text, "a()\n", "b()\n", "f.py")
    assert updated is None and stage == "line_trimmed"
    assert err.ok is False and "ambiguous" in err.error and "line_trimmed" in err.error


def test_not_found_at_all_is_an_error(tmp_path):
    (tmp_path / "f.py").write_text("alpha\n", encoding="utf-8")
    result = _executor(tmp_path).edit_file("f.py", "beta\n", "gamma\n")
    assert result.ok is False and "not found" in result.error


def test_crlf_file_keeps_crlf_terminators(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"one\r\ntwo\r\n")
    result = _executor(tmp_path).edit_file("f.txt", "two\n", "TWO\n")
    assert result.ok is True
    assert (tmp_path / "f.txt").read_bytes() == b"one\r\nTWO\r\n"


def test_file_without_final_newline_stays_without_it(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb", encoding="utf-8")
    assert _executor(tmp_path).edit_file("f.txt", "b", "c\n").ok is True
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "a\nc"


def test_replacing_a_full_line_keeps_its_terminator(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    assert _executor(tmp_path).edit_file("f.txt", "b\n", "B\n").ok is True
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "a\nB\nc\n"


def test_deleting_a_block_removes_its_line(tmp_path):
    (tmp_path / "f.txt").write_text("a\nb\nc\n", encoding="utf-8")
    assert _executor(tmp_path).edit_file("f.txt", "b\n", "").ok is True
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "a\nc\n"


def test_success_message_is_path_plus_unified_diff_with_four_context_lines(tmp_path):
    lines = [f"L{i}" for i in range(1, 11)]
    (tmp_path / "f.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = _executor(tmp_path).edit_file("f.txt", "L5\n", "M5\n")
    assert result.ok is True
    assert result.output.startswith("edited f.txt\n--- a/f.txt\n+++ b/f.txt\n")
    assert "@@ -1,9 +1,9 @@" in result.output  # 4 linhas antes (L1-L4) e 4 depois (L6-L9); L10 fica de fora
    assert "-L5" in result.output and "+M5" in result.output
    assert " L9" in result.output and " L10" not in result.output
    assert result.raw_output == result.output


# --- (b) truncamento 20/80 e integridade do raw_output ---------------------------------


def test_tail_priority_keeps_start_and_a_full_short_traceback_at_the_end():
    traceback = "Traceback (most recent call last):\n  File 'x.py', line 3\nValueError: boom\n"
    text = "HEADER\n" + "x" * 5000 + "\n" + traceback
    out, truncated = truncate_tail_priority(text, 1000)
    assert truncated is True
    assert out.startswith("HEADER")
    assert out.endswith(traceback)  # traceback curto cabe inteiro no bloco de 80%
    assert "tail_priority" in out


def test_tail_priority_uses_twenty_percent_head_and_eighty_percent_tail():
    text = "A" * 2000 + "B" * 2000
    out, _ = truncate_tail_priority(text, 1000)
    marker_at = out.index("\n[...")
    head = out[:marker_at]
    tail = out[out.index("]\n", marker_at) + 2:]
    assert len(tail) > 3 * len(head)  # ~20/80 do orçamento útil
    assert set(head) == {"A"} and set(tail) == {"B"}


def test_run_bash_raw_output_and_sha_stay_complete_under_tail_truncation(tmp_path):
    cmd = f'"{sys.executable}" -c "print(\'H\' * 1000); print(\'END\')"'
    llm = ScriptedLLM([make_response(calls=[call("r1", "run_bash", {"command": cmd})]),
                       make_response(calls=[submit("s1", "done")])])
    result = AgentLoop(llm, _executor(tmp_path, max_output_chars=300),
                       ContextManager(keep_recent_observations=10, harness_variant=B0_PLUS),
                       harness_variant=B0_PLUS).run("t", "s", max_steps=8)
    event = result.tool_events[0]
    assert event.truncated is True
    assert event.raw_output.rstrip().endswith("END") and event.raw_output.count("H") == 1000  # integral
    assert event.raw_sha256 == hashlib.sha256(event.raw_output.encode("utf-8")).hexdigest()
    assert event.observation.rstrip().endswith("END")  # o modelo vê a cauda


def test_b0_run_bash_truncation_is_the_original_head_tail_split():
    text = "A" * 3000 + "B" * 3000
    out, _ = truncate_output(text, 1000)
    assert "tail_priority" not in out and "caracteres omitidos ...]" in out


# --- (c) placeholder informativo determinístico (b0_plus) ------------------------------


def _masked_messages(content, name, args, variant=B0_PLUS):
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": "c1", "name": name, "content": content},
        {"role": "assistant", "content": "next", "tool_calls": []},
    ]
    return ContextManager(keep_recent_observations=0, harness_variant=variant).prepare(messages), messages


def test_read_file_placeholder_has_path_and_line_count():
    content = "".join(f"{i}\tline {i}\n" for i in range(1, 201))
    prepared, _ = _masked_messages(content, "read_file", {"path": "a.py"})
    assert prepared.messages[3]["content"] == \
        "[Previous observation omitted for brevity: read_file('a.py'), 200 lines]"


def test_run_bash_placeholder_truncates_command_to_sixty_chars():
    command = "pytest -q tests/test_very_long_command_name_that_exceeds_sixty_characters_for_truncation.py"
    content = "x\n" * 500
    prepared, _ = _masked_messages(content, "run_bash", {"command": command})
    assert prepared.messages[3]["content"] == (
        "[Previous observation omitted for brevity: run_bash('" + command[:60] + "'), 500 lines]")


def test_other_tools_placeholder_uses_tool_name():
    content = "ok\n" * 300
    prepared, _ = _masked_messages(content, "write_file", {"path": "x", "content": "y"})
    assert prepared.messages[3]["content"] == "[Previous observation omitted for brevity: write_file, 300 lines]"


def test_placeholder_is_deterministic_and_keeps_static_prefix():
    content = "line\n" * 400
    first, messages = _masked_messages(content, "read_file", {"path": "a.py"})
    second = ContextManager(keep_recent_observations=0, harness_variant=B0_PLUS).prepare(messages)
    assert first.messages == second.messages
    assert first.messages[0] == messages[0] and first.messages[1] == messages[1]  # system e user intactos


def test_placeholder_only_applies_when_it_saves_characters():
    prepared, _ = _masked_messages("short\n", "read_file", {"path": "a.py"})
    assert prepared.messages[3]["content"] == "short\n"  # observação menor que o placeholder fica como está


def test_unknown_harness_variant_is_rejected():
    with pytest.raises(ValueError):
        ContextManager(harness_variant="b9")
    with pytest.raises(ValueError):
        ToolExecutor(".", harness_variant="b9")


# --- (d) DoomLoopTracker: disparo exato na 3ª idêntica consecutiva --------------------


def test_doom_loop_fires_from_third_identical_call_and_keeps_firing():
    tracker = DoomLoopTracker(threshold=3)
    results = [tracker.observe("run_bash", '{"command": "ls"}') for _ in range(5)]
    assert results == [False, False, True, True, True]  # 3ª, 4ª e 5ª repetição


def test_doom_loop_streak_counts_current_repetition():
    tracker = DoomLoopTracker(threshold=3)
    seen = []
    for _ in range(4):
        tracker.observe("run_bash", '{"command": "ls"}')
        seen.append(tracker.streak)
    assert seen == [1, 2, 3, 4]


def test_advisory_reports_the_current_count_on_third_fourth_and_fifth_repetition(tmp_path):
    cmd = {"command": "echo same"}
    llm = ScriptedLLM([make_response(calls=[call(f"r{i}", "run_bash", cmd)]) for i in range(5)]
                      + [make_response(calls=[submit("s1", "done")])])
    result = AgentLoop(llm, _executor(tmp_path), ContextManager(keep_recent_observations=10, harness_variant=B0_PLUS),
                       harness_variant=B0_PLUS).run("t", "s", max_steps=10)
    assert result.status == p.STATUS_COMPLETED and result.protocol_errors == 0
    contents = [m["content"] for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]
    assert "[SYSTEM ADVISORY]" not in contents[0] and "[SYSTEM ADVISORY]" not in contents[1]
    for position, count in ((2, 3), (3, 4), (4, 5)):
        assert f"repeated {count} times in a row" in contents[position]
    assert "[SYSTEM ADVISORY]" not in result.tool_events[4].raw_output  # ledger bruto intocado


def test_doom_loop_ignores_key_order_in_arguments():
    tracker = DoomLoopTracker(threshold=3)
    assert [tracker.observe("edit_file", '{"path": "a", "old_text": "x"}'),
            tracker.observe("edit_file", '{"old_text": "x", "path": "a"}'),
            tracker.observe("edit_file", '{"path":"a","old_text":"x"}')] == [False, False, True]


def test_doom_loop_resets_on_different_arguments_or_tool():
    tracker = DoomLoopTracker(threshold=3)
    tracker.observe("run_bash", '{"command": "ls"}')
    tracker.observe("run_bash", '{"command": "ls"}')
    assert tracker.observe("run_bash", '{"command": "pwd"}') is False  # argumentos diferentes reiniciam
    tracker.observe("run_bash", '{"command": "pwd"}')
    assert tracker.observe("read_file", '{"command": "pwd"}') is False  # ferramenta diferente reinicia
    assert [tracker.observe("run_bash", '{"command": "ls"}') for _ in range(3)] == [False, False, True]


def test_doom_loop_advisory_reaches_model_not_ledger_and_is_not_protocol_error(tmp_path):
    cmd = {"command": "echo same"}
    llm = ScriptedLLM([make_response(calls=[call(f"r{i}", "run_bash", cmd)]) for i in range(3)]
                      + [make_response(calls=[submit("s1", "done")])])
    ledger_path = tmp_path / "ledger.jsonl"
    with RunLedger(ledger_path) as ledger:
        result = AgentLoop(llm, _executor(tmp_path), ContextManager(keep_recent_observations=10, harness_variant=B0_PLUS),
                           ledger=ledger, harness_variant=B0_PLUS).run("t", "s", max_steps=8)
    assert result.status == p.STATUS_COMPLETED and result.protocol_errors == 0
    tool_messages = [m for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]
    contents = [m["content"] for m in tool_messages]
    assert "[SYSTEM ADVISORY]" not in contents[0] and "[SYSTEM ADVISORY]" not in contents[1]
    assert SYSTEM_ADVISORY.startswith("[SYSTEM ADVISORY]")
    assert "[SYSTEM ADVISORY]" in contents[2]  # só a 3ª idêntica recebe o aviso
    assert "[SYSTEM ADVISORY]" not in result.tool_events[2].observation  # evento e ledger intactos
    assert "[SYSTEM ADVISORY]" not in result.tool_events[2].raw_output
    finished = [e for e in map(json.loads, ledger_path.read_text(encoding="utf-8").splitlines())
                if e["event"] == "tool_finished"]
    assert all("ADVISORY" not in json.dumps(e) for e in finished)


def test_harness_variant_must_match_executor_and_context(tmp_path):
    with pytest.raises(ValueError):
        AgentLoop(ScriptedLLM([]), ToolExecutor(tmp_path), ContextManager(), harness_variant=B0_PLUS)


# --- (e) invariância do modo padrão b0 -------------------------------------------------


def test_b0_edit_file_has_no_fallback_and_keeps_plain_success_message(tmp_path):
    (tmp_path / "f.py").write_text("    if a:\n        b()\n", encoding="utf-8")
    ex = ToolExecutor(tmp_path)  # padrão b0
    miss = ex.edit_file("f.py", "if a:\n    b()\n", "if a:\n    c()\n")
    assert miss.ok is False and "not found" in miss.error  # sem fallback indentation_flexible
    (tmp_path / "g.py").write_text("x = 1\n", encoding="utf-8")
    ok = ex.edit_file("g.py", "x = 1\n", "x = 2\n")
    assert ok.ok is True and ok.output == "edited g.py"


def test_b0_run_bash_uses_head_tail_split_and_no_tail_marker(tmp_path):
    cmd = f'"{sys.executable}" -c "print(\'H\' * 1000); print(\'END\')"'
    llm = ScriptedLLM([make_response(calls=[call("r1", "run_bash", {"command": cmd})]),
                       make_response(calls=[submit("s1", "done")])])
    result = AgentLoop(llm, ToolExecutor(tmp_path, max_output_chars=300), ContextManager()).run("t", "s", max_steps=8)
    assert "tail_priority" not in result.tool_events[0].observation
    assert "caracteres omitidos ...]" in result.tool_events[0].observation


def test_b0_has_no_doom_loop_advisory_even_with_identical_calls(tmp_path):
    llm = ScriptedLLM([make_response(calls=[call(f"r{i}", "run_bash", {"command": "echo same"})]) for i in range(4)]
                      + [make_response(calls=[submit("s1", "done")])])
    AgentLoop(llm, ToolExecutor(tmp_path), ContextManager()).run("t", "s", max_steps=8)
    everything = json.dumps(llm.calls[-1]["messages"])
    assert "ADVISORY" not in everything


def test_b0_placeholder_is_the_configured_default(tmp_path):
    messages = [
        {"role": "system", "content": "s"}, {"role": "user", "content": "t"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "read_file", "arguments": '{"path": "a"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "x" * 500},
        {"role": "assistant", "content": "n", "tool_calls": []},
    ]
    out = ContextManager(keep_recent_observations=0).prepare(messages).messages
    assert out[3]["content"] == DEFAULT_PLACEHOLDER


def test_config_snapshot_records_variant_and_reference_provenance(tmp_path):
    loop_b0 = AgentLoop(ScriptedLLM([]), ToolExecutor(tmp_path), ContextManager())
    snap = loop_b0._config_snapshot(type("L", (), {"max_steps": 1, "max_tokens": None, "max_cost_usd": None})())
    assert snap["harness_variant"] == "b0"
    assert snap["reference_provenance"] == variants.REFERENCE_PROVENANCE


def test_reference_provenance_has_hashes_and_marks_codex_inconclusive():
    prov = variants.REFERENCE_PROVENANCE
    for key in ("pi_finecode", "opencode_smag_rex"):
        assert len(prov[key]["head"]) == 40 and all(c in "0123456789abcdef" for c in prov[key]["head"])
        assert prov[key]["status"] == "LOCAL_CHECKOUT_DIRTY"  # hash sozinho não reproduz o conteúdo lido
    assert prov["codex_cli"]["status"] == "INCONCLUSIVE_NOT_LOCATED"
    assert prov["codex_cli"]["head"] is None


# --- re-indentação relativa no estágio indentation_flexible (b0_plus) -------------------

FILE_NESTED_AT_8 = (
    "class C:\n"
    "    def run(self):\n"
    "        if x:\n"
    "            for i in y:\n"
    "                do(i)\n"
    "        return 0\n"
)


def test_indentation_flexible_shifts_new_text_when_old_and_new_are_both_misaligned(tmp_path):
    # Bloco aninhado no arquivo com recuo 8; o modelo enviou old e new com recuo 4 (os dois desalinhados).
    old = "    if x:\n        for i in y:\n            do(i)\n"
    new = "    if x:\n        for i in y:\n            do(i, 2)\n"
    updated, stage, err = _apply_edit_with_fallback(FILE_NESTED_AT_8, old, new, "f.py")
    assert err is None and stage == "indentation_flexible"
    assert updated == FILE_NESTED_AT_8.replace("                do(i)\n", "                do(i, 2)\n")
    # sub-bloco preservado: 'for' continua 4 espaços à direita de 'if'; 'do' continua 4 à direita de 'for'
    assert "        if x:\n            for i in y:\n                do(i, 2)\n" in updated


def test_indentation_flexible_does_not_double_indent_when_new_text_is_already_aligned(tmp_path):
    old = "    if x:\n        for i in y:\n            do(i)\n"  # desalinhado
    new = "        if x:\n            for i in y:\n                do(i, 2)\n"  # já no recuo do arquivo
    updated, stage, err = _apply_edit_with_fallback(FILE_NESTED_AT_8, old, new, "f.py")
    assert err is None and stage == "indentation_flexible"
    assert "                do(i, 2)\n" in updated and "                    do(i, 2)" not in updated
    assert "        if x:\n            for i in y:\n                do(i, 2)\n" in updated


def test_indentation_flexible_shifts_left_without_cutting_code():
    file_text = "def f():\n    if a:\n        b()\n"
    old = "        if a:\n            b()\n"  # desalinhado para a direita (recuo 8, arquivo tem 4)
    new = "        if a:\n            c()\n"
    updated, stage, _ = _apply_edit_with_fallback(file_text, old, new, "f.py")
    assert stage == "indentation_flexible"
    assert updated == "def f():\n    if a:\n        c()\n"


def test_reindent_never_removes_non_space_characters():
    from dv_harness.tool_executor import _reindent_block

    assert _reindent_block("  x\n y\n", 0) == " x\ny\n"  # mínimo 1 -> 0; só espaços saem, diferença relativa mantida
    assert _reindent_block("    a\n\n    b\n", 0) == "a\n\nb\n"  # linha vazia preservada como está


def test_reindent_keeps_empty_new_text_as_clean_removal():
    from dv_harness.tool_executor import _reindent_block

    assert _reindent_block("", 4) == ""


def test_indentation_flexible_removal_is_clean(tmp_path):
    text = "a = 1\n    b = 2\n    c = 3\nd = 4\n"
    updated, stage, err = _apply_edit_with_fallback(text, "b = 2\nc = 3\n", "", "f.py")
    assert err is None and updated == "a = 1\nd = 4\n"


def test_indentation_flexible_preserves_crlf_and_missing_final_newline(tmp_path):
    (tmp_path / "f.py").write_bytes(b"    def f():\r\n        return 1\r\n")
    result = _executor(tmp_path).edit_file("f.py", "def f():\n    return 1\n", "def f():\n    return 2\n")
    assert result.ok is True
    assert (tmp_path / "f.py").read_bytes() == b"    def f():\r\n        return 2\r\n"


def test_indentation_flexible_no_final_newline_stays_without_it(tmp_path):
    (tmp_path / "f.py").write_bytes(b"    def f():\n        return 1")
    result = _executor(tmp_path).edit_file("f.py", "def f():\n    return 1", "def f():\n    return 2")
    assert result.ok is True
    assert (tmp_path / "f.py").read_bytes() == b"    def f():\n        return 2"


def test_b0_does_not_reindent_in_indentation_flexible_mode(tmp_path):
    (tmp_path / "f.py").write_text(FILE_NESTED_AT_8, encoding="utf-8")
    miss = ToolExecutor(tmp_path).edit_file("f.py", "    if x:\n        for i in y:\n            do(i)\n",
                                            "    if x:\n        for i in y:\n            do(i, 2)\n")
    assert miss.ok is False and "not found" in miss.error  # b0 não tem o estágio de indentação


# --- motor nivelado, leitura com continuação, saída externa, marcador, schemas e kill ---------

import os  # noqa: E402
import re as _re  # noqa: E402
import subprocess as _subprocess  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from dv_harness import tool_executor as _te  # noqa: E402
from dv_harness import variants as _variants  # noqa: E402
from dv_harness.agent_loop import SUBMIT_SCHEMA as _SUBMIT_SCHEMA  # noqa: E402
from dv_harness.tool_executor import TOOL_SCHEMAS as _TOOL_SCHEMAS  # noqa: E402

_BS = chr(92)  # barra invertida, construída explicitamente


def _apply(text, old, new):
    return _apply_edit_with_fallback(text, old, new, "f.py")


def test_no_op_replacement_raises_value_error():
    with pytest.raises(ValueError, match="No changes applied"):
        _apply("a = 1\n", "a = 1\n", "a = 1\n")


def test_edit_file_reports_no_op_as_error_in_b0_plus(tmp_path):
    (tmp_path / "f.py").write_text("a = 1\n", encoding="utf-8")
    result = _executor(tmp_path).edit_file("f.py", "a = 1\n", "a = 1\n")
    assert result.ok is False and "No changes applied" in result.error
    assert (tmp_path / "f.py").read_text(encoding="utf-8") == "a = 1\n"


def test_b0_identical_edit_keeps_old_behaviour(tmp_path):  # invariância do b0
    (tmp_path / "f.py").write_text("a = 1\n", encoding="utf-8")
    result = ToolExecutor(tmp_path).edit_file("f.py", "a = 1\n", "a = 1\n")
    assert result.ok is True and result.output == "edited f.py"


def test_numbered_prefix_is_stripped_from_old_and_new():
    updated, _stage, err = _apply("def f():\n    return 1\n",
                                  "1\tdef f():\n2\t    return 1\n", "1\tdef f():\n2\t    return 2\n")
    assert err is None and updated == "def f():\n    return 2\n"


def test_numbered_prefix_kept_when_file_literally_contains_it():
    updated, _stage, err = _apply("3\tfoo\n", "3\tfoo\n", "3\tbar\n")
    assert err is None and updated == "3\tbar\n"


def test_numbered_prefix_not_stripped_when_lines_are_mixed():
    _updated, _stage, err = _apply("x = 1\ny = 2\n", "1\tx = 1\ny = 2\n", "x = 9\n")
    assert err is not None and "not found" in err.error


def test_trimmed_boundary_drops_extra_blank_edges():
    updated, stage, err = _apply("x = 1\ny = 2\n", "\nx = 1\n\n", "x = 9\n")
    assert err is None and stage == "trimmed_boundary"
    assert updated == "x = 9\ny = 2\n"


def test_escape_normalized_matches_double_escaped_block():
    updated, stage, err = _apply("a = 1\nb = 2\n", "a = 1" + _BS + "nb = 2" + _BS + "n", "a = 9\n")
    assert err is None and stage == "escape_normalized"
    assert updated == "a = 9\n"


def test_escape_normalized_unescapes_new_text_too():
    updated, stage, err = _apply("x = 1\n", "x = 1" + _BS + "n", "x = 2" + _BS + "n")
    assert err is None and stage == "escape_normalized"
    assert updated == "x = 2\n"


def test_unicode_curly_quotes_match_ascii_line():
    updated, stage, err = _apply('print("hello")\nkeep "ok"\n', "print(\u201chello\u201d)\n", 'print("bye")\n')
    assert err is None and stage == "unicode_normalized"
    assert updated == 'print("bye")\nkeep "ok"\n'


def test_unicode_nbsp_and_dashes_match():
    updated, stage, err = _apply("a =\u00a01\n", "a = 1\n", "a = 2\n")
    assert err is None and stage == "unicode_normalized" and updated == "a = 2\n"
    updated2, stage2, err2 = _apply("x = a\u2013b\n", "x = a-b\n", "x = c\n")
    assert err2 is None and stage2 == "unicode_normalized" and updated2 == "x = c\n"


def test_unicode_stage_preserves_untouched_line_bytes():
    updated, _stage, err = _apply("keep \u201cx\u201d\nprint(\u201chi\u201d)\n", 'print("hi")\n', 'print("bye")\n')
    assert err is None and updated == "keep \u201cx\u201d\nprint(\"bye\")\n"


_BLOCK_FILE = "def f():\n    a = 1\n    b = 2\n    c = 3\n    d = 4\n    e = 5\n    return a\n"


def test_block_anchor_accepts_block_with_one_divergent_middle_line():
    old = "def f():\n    a = 1\n    b = 2\n    c = 99 # agent comment\n    d = 4\n    e = 5\n    return a\n"
    updated, stage, err = _apply(_BLOCK_FILE, old, "def f():\n    return 0\n")
    assert err is None and stage == "block_anchor"
    assert updated == "def f():\n    return 0\n"


def test_block_anchor_rejects_similarity_below_threshold():
    # similaridade média das 5 linhas internas: (1 + 0 + 0 + 1 + 1) / 5 = 0.6 < 0.65
    old = "def f():\n    a = 1\n    zzzzzzzzzz\n    qqqqqqqqqq\n    d = 4\n    e = 5\n    return a\n"
    _updated, _stage, err = _apply(_BLOCK_FILE, old, "def f():\n    return 0\n")
    assert err is not None and "not found" in err.error


def test_block_anchor_rejects_ambiguous_blocks():
    block = "def f():\n    a = 1\n    b = 2\n    c = 3\n    return a\n"
    old = "def f():\n    a = 1\n    b = 22\n    c = 3\n    return a\n"
    updated, stage, err = _apply(block + "\n" + block, old, "def f():\n    return 0\n")
    assert updated is None and stage == "block_anchor"
    assert "ambiguous" in err.error and "2 matches" in err.error


def test_block_anchor_needs_at_least_three_lines():
    _updated, _stage, err = _apply("a\nb\n", "a\nX\n", "Y\n")
    assert err is not None and "not found" in err.error


def test_block_anchor_threshold_is_opencode_value():
    assert _te.BLOCK_ANCHOR_THRESHOLD == 0.65


def test_read_file_truncation_names_continuation_line(tmp_path):
    lines = [f"line {i:03d} " + "x" * 40 for i in range(1, 301)]
    (tmp_path / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    ex = _executor(tmp_path, max_output_chars=2000)
    result = ex.read_file("big.txt")
    assert result.truncated is True
    match = _re.search(r"showing lines 1-(\d+) of 300\. Use start_line=(\d+) to continue\.", result.output)
    assert match is not None
    shown_end = int(match.group(1))
    assert int(match.group(2)) == shown_end + 1
    follow = ex.read_file("big.txt", start_line=shown_end + 1)
    assert follow.output.split("\n")[0].startswith(f"{shown_end + 1}\t")


def test_read_file_b0_has_no_continuation_hint(tmp_path):  # invariância do b0
    lines = [f"line {i:03d} " + "x" * 40 for i in range(1, 301)]
    (tmp_path / "big.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = ToolExecutor(tmp_path, max_output_chars=2000).read_file("big.txt")
    assert "Truncated:" not in result.output and "caracteres omitidos" in result.output


def test_read_file_without_truncation_has_no_hint(tmp_path):
    (tmp_path / "small.txt").write_text("a\nb\n", encoding="utf-8")
    result = _executor(tmp_path).read_file("small.txt")
    assert result.truncated is False and "Truncated" not in result.output


def test_full_output_saved_outside_workspace_and_marker_points_to_it(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    logs = tmp_path / "bash_logs"
    ex = ToolExecutor(ws, max_output_chars=300, harness_variant=B0_PLUS, full_output_dir=logs)
    result = ex.run_bash(f'"{sys.executable}" -c "print(\'x\' * 2000)"')
    assert result.truncated is True and "[Full output saved to: " in result.output
    saved_path = _Path(result.output.split("[Full output saved to: ")[1].split("]")[0])
    assert saved_path.read_text(encoding="utf-8").count("x") == 2000
    assert saved_path.resolve().is_relative_to(logs.resolve())
    assert list(ws.iterdir()) == []  # o workspace não recebe nenhum arquivo
    assert result.raw_output.count("x") == 2000


def test_full_output_dir_inside_workspace_is_refused(tmp_path):
    with pytest.raises(ValueError):
        ToolExecutor(tmp_path, harness_variant=B0_PLUS, full_output_dir=tmp_path / "sub")


def test_b0_run_bash_never_saves_externally(tmp_path):  # invariância do b0
    logs = tmp_path / "logs"
    ex = ToolExecutor(tmp_path / "ws", max_output_chars=300, full_output_dir=logs)
    (tmp_path / "ws").mkdir()
    result = ex.run_bash(f'"{sys.executable}" -c "print(\'x\' * 2000)"')
    assert "Full output saved to" not in result.output and not logs.exists()


def test_run_bash_empty_output_gets_marker_in_b0_plus(tmp_path):
    cmd = f'"{sys.executable}" -c "pass"'
    llm = ScriptedLLM([make_response(calls=[call("r1", "run_bash", {"command": cmd})]),
                       make_response(calls=[submit("s1", "done")])])
    AgentLoop(llm, _executor(tmp_path), ContextManager(keep_recent_observations=10, harness_variant=B0_PLUS),
              harness_variant=B0_PLUS).run("t", "s", max_steps=8)
    tool_messages = [m for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]
    assert tool_messages[0]["content"] == "(no output)"


def test_run_bash_empty_output_stays_empty_in_b0(tmp_path):  # invariância do b0
    cmd = f'"{sys.executable}" -c "pass"'
    llm = ScriptedLLM([make_response(calls=[call("r1", "run_bash", {"command": cmd})]),
                       make_response(calls=[submit("s1", "done")])])
    AgentLoop(llm, ToolExecutor(tmp_path), ContextManager()).run("t", "s", max_steps=8)
    tool_messages = [m for m in llm.calls[-1]["messages"] if m.get("role") == "tool"]
    assert tool_messages[0]["content"] == ""


def test_b0_schemas_and_prompt_are_unchanged():
    assert _te.get_tool_schemas("b0") is _TOOL_SCHEMAS
    assert _variants.build_system_prompt("BASE", "b0") == "BASE"
    assert "WITHOUT the line-number prefix" not in json.dumps(_TOOL_SCHEMAS)


def test_b0_plus_schemas_carry_usage_rules_and_keep_parameters():
    enriched = _te.get_tool_schemas("b0_plus")
    descriptions = {s["function"]["name"]: s["function"]["description"] for s in enriched}
    assert "WITHOUT the line-number prefix" in descriptions["edit_file"]
    assert "prefer edit_file" in descriptions["write_file"]
    assert "cat, sed" in descriptions["run_bash"]
    assert "start_line" in descriptions["read_file"]
    for before, after in zip(_TOOL_SCHEMAS, enriched, strict=True):  # só a descrição muda
        assert before["function"]["name"] == after["function"]["name"]
        assert before["function"]["parameters"] == after["function"]["parameters"]


def test_b0_plus_prompt_adds_guidelines_on_top_of_base():
    text = _variants.build_system_prompt("BASE", "b0_plus")
    assert text.startswith("BASE") and "Tool guidelines" in text and "cat, sed or heredocs" in text


def test_agent_loop_sends_variant_prompt_and_schemas(tmp_path):
    llm = ScriptedLLM([make_response(calls=[submit("s", "x")])])
    AgentLoop(llm, _executor(tmp_path), ContextManager(harness_variant=B0_PLUS),
              harness_variant=B0_PLUS).run("t", "BASE", max_steps=3)
    assert "Tool guidelines" in llm.calls[0]["messages"][0]["content"]
    edit_desc = [t["function"]["description"] for t in llm.calls[0]["tools"] if t["function"]["name"] == "edit_file"]
    assert "WITHOUT the line-number prefix" in edit_desc[0]


def test_agent_loop_b0_sends_unchanged_prompt_and_schemas(tmp_path):  # invariância do b0
    llm = ScriptedLLM([make_response(calls=[submit("s", "x")])])
    AgentLoop(llm, ToolExecutor(tmp_path), ContextManager()).run("t", "BASE", max_steps=3)
    assert llm.calls[0]["messages"][0]["content"] == "BASE"
    assert llm.calls[0]["tools"] == [*_TOOL_SCHEMAS, _SUBMIT_SCHEMA]


def test_kill_group_uses_taskkill_tree_on_windows(monkeypatch):
    calls = []

    class FakeProc:
        pid = 4242

        def kill(self):
            calls.append(("kill",))

    monkeypatch.setattr(_te, "_is_posix", lambda: False)
    monkeypatch.setattr(_te.subprocess, "run", lambda argv, **kw: calls.append(("run", argv)))
    _te._kill_group(FakeProc())
    assert ("run", ["taskkill", "/F", "/T", "/PID", "4242"]) in calls
    assert ("kill",) in calls


class _Stream:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_communicate_after_kill_is_bounded_and_closes_pipes():
    assert _te.POST_KILL_TIMEOUT_S == 5

    class StuckProc:
        def __init__(self):
            self.stdout = _Stream()
            self.stderr = _Stream()

        def communicate(self, timeout=None):
            if timeout is None:
                raise AssertionError("unbounded communicate after kill")
            raise _subprocess.TimeoutExpired(cmd="x", timeout=timeout)

    proc = StuckProc()
    assert _te._communicate_after_kill(proc) == (b"", b"")
    assert proc.stdout.closed and proc.stderr.closed


def test_communicate_after_kill_returns_output_when_it_finishes():
    class DoneProc:
        stdout = None
        stderr = None

        def communicate(self, timeout=None):
            return (b"ok", b"")

    assert _te._communicate_after_kill(DoneProc()) == (b"ok", b"")


@pytest.mark.skipif(os.name != "posix", reason="grupo de processos é POSIX")
def test_timeout_with_grandchild_holding_pipes_returns_bounded(tmp_path):
    import time as _time

    ex = ToolExecutor(tmp_path)
    started = _time.monotonic()
    result = ex.run_bash("sleep 30 & sleep 30", timeout=1)
    assert result.timed_out is True
    assert _time.monotonic() - started < 20  # sem o limite, o neto segurando o pipe travaria aqui


# --- leitura somente-leitura do spill (b0_plus) e aviso de linha única longa -----------------

import os as _os_spill  # noqa: E402
from pathlib import Path as _Path_spill  # noqa: E402


def _spill_pair(tmp_path, limit=300):
    ws = tmp_path / "ws"
    ws.mkdir()
    spill = tmp_path / "bash_logs"
    ex = ToolExecutor(ws, max_output_chars=limit, harness_variant=B0_PLUS, full_output_dir=spill)
    return ws, spill, ex


def _saved_log_path(result):
    return _Path_spill(result.output.split("[Full output saved to: ")[1].split("]")[0])


def test_read_file_reads_full_spill_log_with_line_range(tmp_path):
    _ws, _spill, ex = _spill_pair(tmp_path)
    result = ex.run_bash(f'"{sys.executable}" -c "for i in range(1, 301): print(\'line\', i)"')
    assert result.truncated is True
    log = _saved_log_path(result)
    ranged = ex.read_file(str(log), start_line=3, end_line=5)
    assert ranged.ok is True
    assert "3\tline 3" in ranged.output and "5\tline 5" in ranged.output
    assert "6\tline 6" not in ranged.output
    tail = ex.read_file(str(log), start_line=299)
    assert tail.ok is True and "299\tline 299" in tail.output and "300\tline 300" in tail.output


def test_spill_is_readable_but_never_writable_or_editable(tmp_path):
    ws, _spill, ex = _spill_pair(tmp_path)
    result = ex.run_bash(f'"{sys.executable}" -c "print(\'x\' * 2000)"')
    log = _saved_log_path(result)
    before = log.read_bytes()
    written = ex.write_file(str(log), "tampered")
    assert written.ok is False and "escapes" in written.error
    edited = ex.edit_file(str(log), "x", "y")
    assert edited.ok is False and "escapes" in edited.error
    assert log.read_bytes() == before
    assert list(ws.iterdir()) == []


def test_write_into_spill_by_relative_traversal_is_refused(tmp_path):
    ws, spill, ex = _spill_pair(tmp_path)
    ex.run_bash(f'"{sys.executable}" -c "print(\'x\' * 2000)"')
    result = ex.write_file("../bash_logs/planted.txt", "payload")
    assert result.ok is False and "escapes" in result.error
    assert not (spill / "planted.txt").exists()


def test_path_traversal_out_of_spill_is_rejected(tmp_path):
    _ws, spill, ex = _spill_pair(tmp_path)
    ex.run_bash(f'"{sys.executable}" -c "print(\'x\' * 2000)"')
    (tmp_path / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    result = ex.read_file(str(spill / ".." / "secret.txt"))
    assert result.ok is False and "escapes" in result.error
    assert "TOP SECRET" not in result.output and "TOP SECRET" not in (result.error or "")


def test_symlink_inside_spill_is_not_readable(tmp_path):
    _ws, spill, ex = _spill_pair(tmp_path)
    spill.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("LINKED", encoding="utf-8")
    link = spill / "link.txt"
    try:
        _os_spill.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    result = ex.read_file(str(link))
    assert result.ok is False and "symlinks" in result.error
    assert "LINKED" not in result.output


def test_b0_refuses_reading_spill_and_files_outside_workspace(tmp_path):  # invariância do b0
    ws = tmp_path / "ws"
    ws.mkdir()
    spill = tmp_path / "bash_logs"
    spill.mkdir()
    (spill / "log.txt").write_text("LOG\n", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("OUT\n", encoding="utf-8")
    ex = ToolExecutor(ws, full_output_dir=spill)
    in_spill = ex.read_file(str(spill / "log.txt"))
    assert in_spill.ok is False and "escapes" in in_spill.error
    assert ex.read_file(str(outside)).ok is False


def test_single_long_line_gets_specific_warning_not_a_continuation(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "min.json").write_text('{"a":"' + "x" * 2000 + '"}\n', encoding="utf-8")
    ex = ToolExecutor(ws, max_output_chars=800, harness_variant=B0_PLUS)
    result = ex.read_file("min.json")
    assert result.truncated is True
    assert result.output.startswith('1\t{"a":"')
    assert "line 1 has" in result.output and "cut is inside this line" in result.output
    assert "Use start_line=" not in result.output and "start_line=2" not in result.output
    assert "run_bash" in result.output
    assert len(result.output) <= 800


def test_single_long_line_in_b0_keeps_the_original_marker(tmp_path):  # invariância do b0
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "min.json").write_text('{"a":"' + "x" * 2000 + '"}\n', encoding="utf-8")
    result = ToolExecutor(ws, max_output_chars=800).read_file("min.json")
    assert "caracteres omitidos" in result.output and "cut is inside this line" not in result.output


def test_cut_between_lines_keeps_the_continuation_hint(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "f.txt").write_text("a\nb\nc\n" + "y" * 2000 + "\n", encoding="utf-8")
    result = ToolExecutor(ws, max_output_chars=800, harness_variant=B0_PLUS).read_file("f.txt")
    assert _re.search(r"\[Truncated: showing lines 1-3 of \d+\. Use start_line=4 to continue\.\]", result.output)
    assert "cut is inside this line" not in result.output


# --- diagnóstico de trecho parecido, invalidação de leitura obsoleta e diretriz concisa -------

from dv_harness import tool_executor as _te2  # noqa: E402
from dv_harness.context_manager import _stale_marker  # noqa: E402

_SIMILAR_FILE = [f"v{i} = {i}" for i in range(30)]


def _write_lines(tmp_path, name, lines):
    (tmp_path / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_failed_edit_suggests_closest_window_with_five_lines_of_context(tmp_path):
    _write_lines(tmp_path, "f.py", _SIMILAR_FILE)
    old = "v15 = 15\nv16 = " + "X" * 14 + "\nv17 = 17\n"  # linha do meio muito diferente: block_anchor recusa
    result = _executor(tmp_path).edit_file("f.py", old, "v15 = 0\n")
    assert result.ok is False
    header = "old_text not found in f.py. Did you mean to match these actual lines in f.py?\n"
    assert result.error.startswith(header)
    window = result.error[len(header):]
    assert "v10 = 10" in window and "v22 = 22" in window  # ±5 linhas em torno da janela v15..v17
    assert "v9 = 9" not in window and "v23 = 23" not in window
    assert "v15 = 15" in window and "v17 = 17" in window


def test_closest_window_carries_no_line_number_prefix(tmp_path):
    _write_lines(tmp_path, "f.py", _SIMILAR_FILE)
    result = _executor(tmp_path).edit_file("f.py", "v15 = 15\nv16 = " + "X" * 14 + "\nv17 = 17\n", "v15 = 0\n")
    window = result.error.split("\n", 1)[1]
    assert not any(_re.match(r"^\d+\t", line) for line in window.splitlines())


def test_failed_edit_without_a_similar_window_does_not_invent_one(tmp_path):
    _write_lines(tmp_path, "f.py", _SIMILAR_FILE)
    result = _executor(tmp_path).edit_file("f.py", "alpha\nbeta\ngamma\n", "delta\n")
    assert result.error == "old_text not found in f.py"


def test_closest_match_threshold_is_point_six_at_the_boundary():
    below = "\n".join(["A1", "B1", "C1", "Y1", "Y2", "Y3"]) + "\n"  # razão 2*3/12 = 0.5
    assert _te2._closest_match_window(below, "A1\nB1\nC1\nX1\nX2\nX3\n") is None
    above = "\n".join(["A1", "B1", "C1", "D1", "Y2", "Y3"]) + "\n"  # razão 2*4/12 = 0.667
    assert _te2._closest_match_window(above, "A1\nB1\nC1\nD1\nX2\nX3\n") is not None


def test_failed_edit_notes_when_new_text_is_already_in_the_file(tmp_path):
    _write_lines(tmp_path, "f.py", ["a = 1", "b = 2"])
    result = _executor(tmp_path).edit_file("f.py", "a = 9\n", "b = 2\n")
    assert result.ok is False
    assert ("Note: new_text already appears in f.py; verify whether this edit was already applied."
            in result.error)


def test_no_note_when_new_text_is_absent_from_the_file(tmp_path):
    _write_lines(tmp_path, "f.py", ["a = 1", "b = 2"])
    result = _executor(tmp_path).edit_file("f.py", "a = 9\n", "c = 3\n")
    assert "Note:" not in result.error


def _tool_turn(call_id, name, args, content):
    return [
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]},
        {"role": "tool", "tool_call_id": call_id, "name": name, "content": content},
    ]


def _history(*turns):
    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TASK"}]
    for turn in turns:
        messages += turn
    return messages


def test_read_before_edit_is_invalidated_and_read_after_edit_is_kept():
    msgs = _history(
        _tool_turn("r1", "read_file", {"path": "a.py"}, "1\told = 1"),
        _tool_turn("e1", "edit_file", {"path": "a.py", "old_text": "old = 1", "new_text": "new = 1"},
                   "edited a.py\n--- a/a.py\n+++ b/a.py"),
        _tool_turn("r2", "read_file", {"path": "a.py"}, "1\tnew = 1"),
    )
    prepared = ContextManager(keep_recent_observations=10, harness_variant=B0_PLUS).prepare(msgs).messages
    assert prepared[3]["content"] == _stale_marker("a.py")  # leitura anterior à edição
    assert prepared[7]["content"] == "1\tnew = 1"  # leitura posterior à edição permanece
    assert prepared[5]["content"] == msgs[5]["content"]  # observação da própria edição intacta
    assert prepared[0] == msgs[0] and prepared[1] == msgs[1]  # prefixo estático intacto


def test_write_file_success_also_invalidates_earlier_reads():
    msgs = _history(
        _tool_turn("r1", "read_file", {"path": "a.py"}, "1\told"),
        _tool_turn("w1", "write_file", {"path": "a.py", "content": "new"}, "wrote 3 bytes to a.py"),
    )
    prepared = ContextManager(keep_recent_observations=10, harness_variant=B0_PLUS).prepare(msgs).messages
    assert prepared[3]["content"] == _stale_marker("a.py")


def test_path_forms_are_normalised_before_comparison():
    msgs = _history(
        _tool_turn("r1", "read_file", {"path": "./a.py"}, "1\told"),
        _tool_turn("e1", "edit_file", {"path": "sub/../a.py", "old_text": "o", "new_text": "n"}, "edited a.py"),
    )
    prepared = ContextManager(keep_recent_observations=10, harness_variant=B0_PLUS).prepare(msgs).messages
    assert prepared[3]["content"] == _stale_marker("./a.py")


def test_edit_of_another_file_does_not_invalidate_a_read():
    msgs = _history(
        _tool_turn("r1", "read_file", {"path": "a.py"}, "1\told"),
        _tool_turn("e1", "edit_file", {"path": "b.py", "old_text": "o", "new_text": "n"}, "edited b.py"),
    )
    prepared = ContextManager(keep_recent_observations=10, harness_variant=B0_PLUS).prepare(msgs).messages
    assert prepared[3]["content"] == "1\told"


def test_failed_edit_does_not_invalidate_a_read():
    msgs = _history(
        _tool_turn("r1", "read_file", {"path": "a.py"}, "1\told"),
        _tool_turn("e1", "edit_file", {"path": "a.py", "old_text": "x", "new_text": "y"},
                   "error: old_text not found in a.py"),
    )
    prepared = ContextManager(keep_recent_observations=10, harness_variant=B0_PLUS).prepare(msgs).messages
    assert prepared[3]["content"] == "1\told"


def test_b0_never_invalidates_reads():  # invariância do b0
    msgs = _history(
        _tool_turn("r1", "read_file", {"path": "a.py"}, "1\told = 1"),
        _tool_turn("e1", "edit_file", {"path": "a.py", "old_text": "old = 1", "new_text": "new = 1"},
                   "edited a.py"),
    )
    prepared = ContextManager(keep_recent_observations=10).prepare(msgs).messages
    assert prepared[3]["content"] == "1\told = 1"


def test_b0_edit_failure_message_is_the_short_original(tmp_path):  # invariância do b0
    _write_lines(tmp_path, "f.py", _SIMILAR_FILE)
    result = ToolExecutor(tmp_path).edit_file("f.py", "v15 = 15\nv16 = " + "X" * 14 + "\nv17 = 17\n", "v15 = 0\n")
    assert result.error == "old_text not found in f.py"
    _write_lines(tmp_path, "g.py", ["a = 1", "b = 2"])
    noted = ToolExecutor(tmp_path).edit_file("g.py", "a = 9\n", "b = 2\n")
    assert noted.error == "old_text not found in g.py"


def test_stale_marker_text_is_exact():
    assert _stale_marker("a.py") == (
        "[Stale observation invalidated: read_file('a.py') predates a subsequent edit_file/write_file "
        "to this file; rely on the unified diff or call read_file again.]"
    )


def test_b0_plus_guidelines_include_the_concise_old_text_rule():
    assert any("concise" in line and "old_text" in line for line in _variants.B0_PLUS_GUIDELINES)
