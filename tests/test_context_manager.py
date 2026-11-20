"""Testes do Módulo 3 (ContextManager, Observation Masking). Sem chamada de API.

Executar a partir da raiz do repositório:
    python -m pytest tests/test_context_manager.py -v
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dv_harness.context_manager import (  # noqa: E402
    DEFAULT_KEEP_RECENT_OBSERVATIONS,
    DEFAULT_PLACEHOLDER,
    ContextManager,
)

LONG = "x" * 500  # observação longa, bem maior que o placeholder


def _history(n_observations: int, obs_content=LONG):
    """system + user inicial + (assistant com tool_call + tool) * n."""
    msgs = [
        {"role": "system", "content": "S" * 500},  # longo: exercita a proteção de tamanho
        {"role": "user", "content": "Fix the bug in foo.py."},
    ]
    for i in range(n_observations):
        call_id = f"call_{i}"
        msgs.append(
            {
                "role": "assistant",
                "content": f"reasoning {i} " + "r" * 500,  # longo: exercita a proteção de assistant
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "run_bash", "arguments": f'{{"command": "step {i}"}}'},
                    }
                ],
            }
        )
        msgs.append({"role": "tool", "tool_call_id": call_id, "name": "run_bash", "content": f"{obs_content}{i}"})
    return msgs


def test_default_window_is_ten():
    assert DEFAULT_KEEP_RECENT_OBSERVATIONS == 10
    assert ContextManager().keep_recent_observations == 10


def test_system_and_initial_user_prompt_are_preserved():
    msgs = _history(15)
    out = ContextManager(keep_recent_observations=2).prepare(msgs).messages
    assert out[0] == msgs[0]
    assert out[1] == msgs[1]


def test_all_assistant_messages_and_tool_calls_are_untouched():
    msgs = _history(15)
    out = ContextManager(keep_recent_observations=2).prepare(msgs).messages
    for original, prepared in zip(msgs, out):
        if original["role"] == "assistant":
            assert prepared == original


def test_last_n_observations_are_kept_verbatim():
    msgs = _history(12)
    out = ContextManager(keep_recent_observations=3).prepare(msgs).messages
    tools_in = [m for m in msgs if m["role"] == "tool"]
    tools_out = [m for m in out if m["role"] == "tool"]
    assert [m["content"] for m in tools_out[-3:]] == [m["content"] for m in tools_in[-3:]]


def test_older_observations_are_masked_with_placeholder():
    msgs = _history(12)
    out = ContextManager(keep_recent_observations=3).prepare(msgs).messages
    tools_out = [m for m in out if m["role"] == "tool"]
    assert all(m["content"] == DEFAULT_PLACEHOLDER for m in tools_out[:-3])


def test_masking_keeps_tool_call_id_and_metadata():
    msgs = _history(5)
    out = ContextManager(keep_recent_observations=1).prepare(msgs).messages
    tools_in = [m for m in msgs if m["role"] == "tool"]
    tools_out = [m for m in out if m["role"] == "tool"]
    for before, after in zip(tools_in, tools_out):
        assert after["tool_call_id"] == before["tool_call_id"]
        assert after["name"] == before["name"]
        assert set(after) == set(before)
    # Protocolo: cada tool_call_id do assistant continua tendo sua resposta tool.
    call_ids = {tc["id"] for m in out if m["role"] == "assistant" for tc in m.get("tool_calls", [])}
    response_ids = {m["tool_call_id"] for m in out if m["role"] == "tool"}
    assert call_ids == response_ids


def test_original_history_is_not_mutated():
    msgs = _history(12)
    snapshot = copy.deepcopy(msgs)
    ContextManager(keep_recent_observations=2).prepare(msgs)
    assert msgs == snapshot


def test_prepared_output_does_not_alias_input():
    msgs = _history(4)
    out = ContextManager(keep_recent_observations=10).prepare(msgs).messages
    out[2]["tool_calls"][0]["function"]["name"] = "changed"
    out[-1]["content"] = "changed"
    assert msgs[2]["tool_calls"][0]["function"]["name"] == "run_bash"
    assert msgs[-1]["content"] != "changed"


def test_stats_count_masked_and_saved_characters():
    msgs = _history(6)  # 6 observações de ~503 caracteres
    result = ContextManager(keep_recent_observations=2).prepare(msgs)
    s = result.stats
    assert s.total_observations == 6
    assert s.masked_observations == 4
    masked_original = [m for m in msgs if m["role"] == "tool"][:4]
    assert s.chars_before == sum(len(m["content"]) for m in masked_original)
    assert s.chars_after == 4 * len(DEFAULT_PLACEHOLDER)
    assert s.chars_saved == s.chars_before - s.chars_after
    assert s.chars_saved > 0


def test_saved_characters_match_actual_length_difference():
    msgs = _history(8)
    result = ContextManager(keep_recent_observations=3).prepare(msgs)
    before_total = sum(len(m.get("content") or "") for m in msgs)
    after_total = sum(len(m.get("content") or "") for m in result.messages)
    assert before_total - after_total == result.stats.chars_saved


def test_short_old_observation_is_not_masked_so_context_never_grows():
    short = "ok"  # menor que o placeholder
    msgs = _history(3, obs_content=short)
    result = ContextManager(keep_recent_observations=0).prepare(msgs)
    assert result.stats.masked_observations == 0
    assert result.stats.chars_saved == 0
    original = [m["content"] for m in msgs if m["role"] == "tool"]
    assert [m["content"] for m in result.messages if m["role"] == "tool"] == original


def test_window_larger_than_history_masks_nothing():
    msgs = _history(3)
    result = ContextManager(keep_recent_observations=50).prepare(msgs)
    assert result.stats.masked_observations == 0
    assert result.messages == msgs


def test_zero_window_masks_all_long_observations():
    msgs = _history(4)
    result = ContextManager(keep_recent_observations=0).prepare(msgs)
    assert result.stats.masked_observations == 4
    assert all(m["content"] == DEFAULT_PLACEHOLDER for m in result.messages if m["role"] == "tool")


def test_custom_placeholder_is_used():
    msgs = _history(3)
    result = ContextManager(keep_recent_observations=1, placeholder="[gone]").prepare(msgs)
    tools = [m for m in result.messages if m["role"] == "tool"]
    assert tools[0]["content"] == "[gone]"


def test_no_observations_is_a_noop():
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    result = ContextManager().prepare(msgs)
    assert result.messages == msgs
    assert result.stats.total_observations == 0
    assert result.stats.chars_saved == 0


def test_non_string_content_is_measured_and_masked():
    msgs = _history(2)
    msgs[3]["content"] = [{"type": "text", "text": "y" * 300}]
    result = ContextManager(keep_recent_observations=1).prepare(msgs)
    assert result.stats.masked_observations == 1
    assert result.stats.chars_before > 300


def test_invalid_window_is_rejected():
    with pytest.raises(ValueError):
        ContextManager(keep_recent_observations=-1)
    with pytest.raises(TypeError):
        ContextManager(keep_recent_observations=2.5)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        ContextManager(keep_recent_observations=True)  # type: ignore[arg-type]
