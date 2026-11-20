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


def test_stage_indentation_flexible_matches_wrong_indentation_and_writes_new_text_verbatim():
    text = "    if a:\n        b()\n"
    updated, stage, err = _apply_edit_with_fallback(text, "if a:\n    b()\n", "if a:\n    c()\n", "f.py")
    assert err is None and stage == "indentation_flexible"
    assert updated == "if a:\n    c()\n"  # sem re-indentação: o texto novo é gravado como veio


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


def test_doom_loop_fires_exactly_on_third_identical_call():
    tracker = DoomLoopTracker(threshold=3)
    results = [tracker.observe("run_bash", '{"command": "ls"}') for _ in range(5)]
    assert results == [False, False, True, False, False]  # uma vez por sequência


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
