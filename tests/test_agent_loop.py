"""Testes do Módulo 4 (AgentLoop). Integração: LLM mockado, ToolExecutor e ContextManager reais.

Executar a partir da raiz do repositório:
    python -m pytest tests/test_agent_loop.py -v
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from dv_harness.agent_loop import (  # noqa: E402
    STATUS_BUDGET,
    STATUS_COMPLETED,
    STATUS_MAX_STEPS,
    STATUS_UNVERIFIABLE,
    AgentLoop,
)
from dv_harness.context_manager import DEFAULT_PLACEHOLDER, ContextManager  # noqa: E402
from dv_harness.llm_client import LLMResponse, ToolCall  # noqa: E402
from dv_harness.tool_executor import ToolExecutor  # noqa: E402


def resp(content=None, calls=(), prompt=10, completion=5, cost=0.01):
    return LLMResponse(
        content=content,
        tool_calls=tuple(calls),
        model="mock",
        prompt_tokens=prompt,
        completion_tokens=completion,
        total_tokens=prompt + completion,
        cost_usd=cost,
    )


def call(call_id, name, args: dict | str):
    arguments = args if isinstance(args, str) else json.dumps(args)
    return ToolCall(id=call_id, name=name, arguments=arguments)


class ScriptedLLM:
    """LLM mockado. ``script`` é uma lista de LLMResponse ou funções ``(messages) -> LLMResponse``.

    Após o fim do roteiro, repete o último item. Registra cada chamada recebida.
    """

    def __init__(self, script):
        self.script = list(script)
        self.calls: list[dict] = []

    def complete(self, messages, tools=None, model=None):
        self.calls.append({"messages": copy.deepcopy(list(messages)), "tools": tools})
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        return item(messages) if callable(item) else item


def make_loop(tmp_path, llm, keep_recent=10):
    return AgentLoop(llm, ToolExecutor(tmp_path), ContextManager(keep_recent_observations=keep_recent))


def tool_contents(messages):
    return [m["content"] for m in messages if m["role"] == "tool"]


# --- conclusão direta ------------------------------------------------------------


def test_direct_completion_without_tools(tmp_path):
    llm = ScriptedLLM([resp(content="Paris", prompt=20, completion=3, cost=0.002)])
    result = make_loop(tmp_path, llm).run("Capital of France?", "Be brief.")

    assert result.status == STATUS_COMPLETED
    assert result.final_answer == "Paris"
    assert result.steps == 1
    assert result.total_prompt_tokens == 20
    assert result.total_completion_tokens == 3
    assert result.total_tokens == 23
    assert result.total_cost_usd == pytest.approx(0.002)
    assert result.chars_saved_by_masking == 0
    assert [m["role"] for m in result.messages] == ["system", "user", "assistant"]
    assert llm.calls[0]["tools"] is not None  # TOOL_SCHEMAS sempre enviado


# --- ciclo multi-turno real ----------------------------------------------------------


def test_multi_turn_read_edit_bash_then_answer_on_real_files(tmp_path):
    target = tmp_path / "app.py"
    target.write_text("def f():\n    return 1\n", encoding="utf-8")

    seen_at_bash_step = {}

    def step_bash(messages):
        seen_at_bash_step["obs"] = tool_contents(messages)
        return resp(calls=[call("c3", "run_bash", {"command": "cat app.py"})])

    llm = ScriptedLLM([
        resp(calls=[call("c1", "read_file", {"path": "app.py"})]),
        resp(calls=[call("c2", "edit_file", {"path": "app.py", "old_text": "return 1", "new_text": "return 2"})]),
        step_bash,
        resp(content="Changed return value to 2.", cost=0.01),
    ])
    result = make_loop(tmp_path, llm).run("Change f to return 2.", "You edit code.")

    assert result.status == STATUS_COMPLETED
    assert result.final_answer == "Changed return value to 2."
    assert result.steps == 4
    assert target.read_text(encoding="utf-8") == "def f():\n    return 2\n"
    # Observações reais chegaram ao modelo: leitura numerada, edição confirmada, saída do bash.
    obs = seen_at_bash_step["obs"]
    assert "1\tdef f():" in obs[0]
    assert obs[1] == "edited app.py"
    bash_obs = next(m["content"] for m in result.messages if m.get("tool_call_id") == "c3")
    assert "return 2" in bash_obs
    # Mensagens de tool carregam o tool_call_id correspondente.
    assert [m["tool_call_id"] for m in result.messages if m["role"] == "tool"] == ["c1", "c2", "c3"]


# --- recuperação de erros ----------------------------------------------------------


def test_unknown_tool_and_invalid_json_are_observations_not_crashes(tmp_path):
    llm = ScriptedLLM([
        resp(calls=[call("u1", "delete_everything", {})]),
        resp(calls=[call("j1", "read_file", "{not json")]),
        resp(calls=[call("a1", "edit_file", {"path": "x.txt"})]),  # argumentos faltando
        resp(content="recovered"),
    ])
    result = make_loop(tmp_path, llm).run("t", "s")

    obs = tool_contents(result.messages)
    assert result.status == STATUS_COMPLETED
    assert result.final_answer == "recovered"
    assert "unknown tool 'delete_everything'" in obs[0]
    assert "invalid JSON arguments for read_file" in obs[1]
    assert "invalid arguments for edit_file" in obs[2]
    # O modelo recebeu os erros como observação no passo seguinte.
    assert any("unknown tool" in m.get("content", "") for m in llm.calls[1]["messages"] if m["role"] == "tool")


def test_tool_exception_becomes_observation(tmp_path):
    # Timeout inválido (<= 0) faz run_bash levantar ValueError. O loop não pode cair.
    llm = ScriptedLLM([
        resp(calls=[call("b1", "run_bash", {"command": "echo x", "timeout": 0})]),
        resp(content="ok"),
    ])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.status == STATUS_COMPLETED
    assert "run_bash failed: ValueError" in tool_contents(result.messages)[0]


def test_failing_bash_command_is_observation_with_exit_code_in_text(tmp_path):
    llm = ScriptedLLM([
        resp(calls=[call("b1", "run_bash", {"command": "echo boom; exit 4"})]),
        resp(content="done"),
    ])
    result = make_loop(tmp_path, llm).run("t", "s")
    obs = tool_contents(result.messages)[0]
    assert obs.startswith("error: exit code 4") and "boom" in obs


# --- limites -----------------------------------------------------------------------


def _always_bash(messages):
    return resp(calls=[call("loop", "run_bash", {"command": "echo x"})], prompt=10, completion=10, cost=0.1)


def test_max_steps_stops_exactly_at_limit(tmp_path):
    llm = ScriptedLLM([_always_bash])
    result = make_loop(tmp_path, llm).run("t", "s", max_steps=3)
    assert result.status == STATUS_MAX_STEPS
    assert result.steps == 3
    assert len(llm.calls) == 3


def test_max_steps_one_still_runs_one_step(tmp_path):
    llm = ScriptedLLM([_always_bash])
    result = make_loop(tmp_path, llm).run("t", "s", max_steps=1)
    assert result.status == STATUS_MAX_STEPS and result.steps == 1


def test_max_tokens_stops_at_first_turn_reaching_budget(tmp_path):
    # cada passo: 60 + 40 = 100 tokens. Limite 250 -> para no 3º passo (300 >= 250).
    llm = ScriptedLLM([lambda m: resp(calls=[call("t", "run_bash", {"command": "echo x"})], prompt=60, completion=40, cost=0.01)])
    result = make_loop(tmp_path, llm).run("t", "s", max_tokens=250, max_steps=50)
    assert result.status == STATUS_BUDGET
    assert result.steps == 3
    assert result.total_tokens == 300
    assert len(llm.calls) == 3


def test_max_tokens_exact_boundary_stops_on_equal(tmp_path):
    llm = ScriptedLLM([lambda m: resp(calls=[call("t", "run_bash", {"command": "echo x"})], prompt=60, completion=40)])
    result = make_loop(tmp_path, llm).run("t", "s", max_tokens=100, max_steps=50)
    assert result.status == STATUS_BUDGET and result.steps == 1


def test_max_cost_stops_when_accumulated_cost_reaches_budget(tmp_path):
    # custo por passo 0.4. Limite 1.0 -> 0.4, 0.8, 1.2: para no 3º passo.
    llm = ScriptedLLM([lambda m: resp(calls=[call("t", "run_bash", {"command": "echo x"})], cost=0.4)])
    result = make_loop(tmp_path, llm).run("t", "s", max_cost_usd=1.0, max_steps=50)
    assert result.status == STATUS_BUDGET
    assert result.steps == 3
    assert result.total_cost_usd == pytest.approx(1.2)


def test_limits_must_be_positive(tmp_path):
    loop = make_loop(tmp_path, ScriptedLLM([resp(content="x")]))
    with pytest.raises(ValueError):
        loop.run("t", "s", max_steps=0)
    with pytest.raises(ValueError):
        loop.run("t", "s", max_tokens=0)
    with pytest.raises(ValueError):
        loop.run("t", "s", max_cost_usd=0)


# --- custo desconhecido -------------------------------------------------------------


def test_unknown_cost_is_none_not_zero(tmp_path):
    llm = ScriptedLLM([resp(content="done", cost=None)])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.total_cost_usd is None
    assert result.status == STATUS_COMPLETED


def test_unknown_cost_after_known_cost_makes_total_none(tmp_path):
    llm = ScriptedLLM([
        resp(calls=[call("t", "run_bash", {"command": "echo x"})], cost=0.5),
        resp(content="done", cost=None),
    ])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.total_cost_usd is None


def test_unknown_cost_with_cost_budget_is_unverifiable_not_passed(tmp_path):
    llm = ScriptedLLM([
        resp(calls=[call("t", "run_bash", {"command": "echo x"})], cost=None),
        resp(content="never reached"),
    ])
    result = make_loop(tmp_path, llm).run("t", "s", max_cost_usd=1.0)
    assert result.status == STATUS_UNVERIFIABLE
    assert result.total_cost_usd is None
    assert result.steps == 1
    assert len(llm.calls) == 1


def test_unknown_cost_without_cost_budget_does_not_stop_loop(tmp_path):
    llm = ScriptedLLM([
        resp(calls=[call("t", "run_bash", {"command": "echo x"})], cost=None),
        resp(content="finished"),
    ])
    result = make_loop(tmp_path, llm).run("t", "s")
    assert result.status == STATUS_COMPLETED and result.total_cost_usd is None


# --- masking acumulado ----------------------------------------------------------------


def test_chars_saved_by_masking_accumulates_across_calls(tmp_path):
    # Janela 0: toda observação antiga é mascarada. obs1 (longa) é mascarada nas chamadas 2 e 3.
    # obs2 ("ok") é curta e nunca é mascarada.
    llm = ScriptedLLM([
        resp(calls=[call("b1", "run_bash", {"command": "python3 -c 'print(\"x\" * 600)'"})]),
        resp(calls=[call("b2", "run_bash", {"command": "echo ok"})]),
        resp(content="done"),
    ])
    result = make_loop(tmp_path, llm, keep_recent=0).run("t", "s")

    long_obs = tool_contents(result.messages)[0]
    assert len(long_obs) > len(DEFAULT_PLACEHOLDER)
    per_call_saving = len(long_obs) - len(DEFAULT_PLACEHOLDER)
    assert result.chars_saved_by_masking == 2 * per_call_saving
    # O modelo, na 3ª chamada, viu o placeholder na observação longa antiga.
    third_call_tools = tool_contents(llm.calls[2]["messages"])
    assert third_call_tools[0] == DEFAULT_PLACEHOLDER
    assert third_call_tools[1] == "ok\n"


def test_audit_log_keeps_full_observations(tmp_path):
    llm = ScriptedLLM([
        resp(calls=[call("b1", "run_bash", {"command": "python3 -c 'print(\"y\" * 800)'"})]),
        resp(calls=[call("b2", "run_bash", {"command": "echo next"})]),
        resp(content="done"),
    ])
    result = make_loop(tmp_path, llm, keep_recent=0).run("t", "s")
    assert len(tool_contents(result.messages)[0]) > len(DEFAULT_PLACEHOLDER)


def test_no_masking_when_window_covers_all(tmp_path):
    llm = ScriptedLLM([
        resp(calls=[call("b1", "run_bash", {"command": "python3 -c 'print(\"z\" * 600)'"})]),
        resp(content="done"),
    ])
    result = make_loop(tmp_path, llm, keep_recent=10).run("t", "s")
    assert result.chars_saved_by_masking == 0
