"""ContextManager: Observation Masking sem chamadas extras ao LLM (Módulo 3).

Mantém a lista linear de mensagens e produz uma cópia preparada para o ``LLMClient``.
Observações (``role="tool"``) fora da janela das ``keep_recent_observations`` mais recentes
têm o ``content`` trocado por um placeholder, desde que a troca economize caracteres.
Observações antigas já menores que o placeholder permanecem como estão, para não aumentar
o contexto. ``tool_call_id`` e demais campos são preservados para não quebrar o protocolo
de tool calling da API.

Preservados sempre intactos: mensagens ``system``, todas as mensagens ``user`` e todas as
mensagens ``assistant`` (raciocínio e ``tool_calls``).

Não há sumarização por LLM, memória persistente, roteamento nem orquestração. O log
original nunca é alterado. ``prepare`` devolve cópias profundas.

Limitação: a ferramenta de texto é mascarada por tamanho e posição, não por relevância.
Uma observação antiga que seja necessária mais tarde não é recuperada automaticamente.

Atribuições empíricas
---------------------
- Estratégia de Observation Masking: ``JetBrains-Research/the-complexity-trap``
  (https://github.com/JetBrains-Research/the-complexity-trap), apresentado no workshop
  DL4Code do NeurIPS 2025, e paper "The Complexity Trap: Simple Observation Masking Is as
  Efficient as LLM Summarization for Agent Context Management" (arXiv 2508.21433).
  Verificado no código: ``LastNObservations`` em ``sweagent/agent/history_processors.py``
  mantém as últimas ``n`` observações e a primeira (instance template). O placeholder do
  código é ``Old environment output: (N lines omitted)``. Este módulo usa um placeholder
  configurável, com padrão ``[Previous observation omitted for brevity]``.
- O README afirma redução de custo "around 50%" sem degradação significativa. Essa é uma
  afirmação geral do README, sem modelo, benchmark nem valor de M associados. Não foi
  reproduzida aqui. O valor de M não está definido no README nem no código lido, e não é
  usado.
"""

from __future__ import annotations

import copy
import json
import posixpath
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from dv_harness.variants import VARIANT_B0_PLUS, check_variant

DEFAULT_KEEP_RECENT_OBSERVATIONS = 10
DEFAULT_PLACEHOLDER = "[Previous observation omitted for brevity]"

Message = Mapping[str, Any]


@dataclass(frozen=True)
class CompactionStats:
    """Estatísticas de uma preparação de contexto.

    ``chars_before`` e ``chars_after`` contam só as observações efetivamente mascaradas.
    Observação antiga já menor que o placeholder não é mascarada e não entra nesses totais.
    """

    total_observations: int
    masked_observations: int
    chars_before: int
    chars_after: int

    @property
    def chars_saved(self) -> int:
        return self.chars_before - self.chars_after


@dataclass(frozen=True)
class PreparedContext:
    messages: list[dict[str, Any]]
    stats: CompactionStats


def _content_len(content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content)
    return len(json.dumps(content, ensure_ascii=False, default=str))


def _calls_by_id(messages: Sequence[Message]) -> dict[str, tuple[str, str]]:
    """Mapeia ``tool_call_id`` -> (nome, argumentos) a partir das mensagens ``assistant``."""
    found: dict[str, tuple[str, str]] = {}
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            found[str(call.get("id"))] = (str(fn.get("name", "")), str(fn.get("arguments", "")))
    return found


def _line_count(content: Any) -> int:
    text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)
    return len(text.splitlines())


def _informative_placeholder(name: str, arguments: str, content: Any) -> str:
    """Placeholder determinístico (b0_plus). Depende só de nome, argumentos e contagem de linhas."""
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    if name == "read_file":
        target = f"read_file('{args.get('path', '')}')"
    elif name == "run_bash":
        target = f"run_bash('{str(args.get('command', ''))[:60]}')"
    else:
        target = name or "unknown"
    return f"[Previous observation omitted for brevity: {target}, {_line_count(content)} lines]"


def _norm_path(path: str) -> str:
    """Forma canônica de um caminho para comparar leituras e escritas: barras normalizadas, sem ``./`` nem ``..``."""
    return posixpath.normpath(path.replace("\\", "/"))


def _path_argument(arguments: str) -> str | None:
    try:
        args = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError:
        return None
    path = args.get("path") if isinstance(args, dict) else None
    return path if isinstance(path, str) else None


def _stale_read_indices(messages: Sequence[Message], calls: dict[str, tuple[str, str]]) -> dict[int, str]:
    """Observações ``read_file`` que antecedem uma edição ou escrita bem-sucedida do mesmo caminho.

    Devolve ``{índice: caminho_como_pedido}``. Sucesso é o prefixo que a própria ferramenta grava
    (``edited `` ou ``wrote ``). Falha começa com ``error:`` e não invalida nada. Leituras posteriores à
    última alteração do arquivo não aparecem aqui.
    """
    mutations: list[tuple[int, str]] = []
    reads: list[tuple[int, str, str]] = []
    for index, msg in enumerate(messages):
        content = msg.get("content")
        if msg.get("role") != "tool" or not isinstance(content, str):
            continue
        name, arguments = calls.get(str(msg.get("tool_call_id")), (str(msg.get("name", "")), ""))
        path = _path_argument(arguments)
        if path is None:
            continue
        if name in ("edit_file", "write_file") and content.startswith(("edited ", "wrote ")):
            mutations.append((index, _norm_path(path)))
        elif name == "read_file" and not content.startswith("error:"):
            reads.append((index, _norm_path(path), path))
    return {
        index: requested
        for index, norm, requested in reads
        if any(mut_index > index and mut_path == norm for mut_index, mut_path in mutations)
    }


def _stale_marker(requested: str) -> str:
    return (
        f"[Stale observation invalidated: read_file('{requested}') predates a subsequent edit_file/write_file "
        "to this file; rely on the unified diff or call read_file again.]"
    )


class ContextManager:
    """Aplica Observation Masking a uma lista de mensagens.

    ``keep_recent_observations``: quantas observações ``tool`` mais recentes mantêm o conteúdo
    original. As demais são mascaradas. ``0`` mascara todas.
    """

    def __init__(
        self,
        keep_recent_observations: int = DEFAULT_KEEP_RECENT_OBSERVATIONS,
        placeholder: str = DEFAULT_PLACEHOLDER,
        harness_variant: str = "b0",
    ) -> None:
        if not isinstance(keep_recent_observations, int) or isinstance(keep_recent_observations, bool):
            raise TypeError("keep_recent_observations must be an int")
        if keep_recent_observations < 0:
            raise ValueError("keep_recent_observations must be >= 0")
        self.keep_recent_observations = keep_recent_observations
        self.placeholder = placeholder
        self.harness_variant = check_variant(harness_variant)

    def prepare(self, messages: Sequence[Message]) -> PreparedContext:
        """Devolve cópia preparada para envio ao LLM. ``messages`` não é alterado."""
        observation_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        keep_from = max(0, len(observation_indices) - self.keep_recent_observations)
        to_mask = set(observation_indices[:keep_from])

        calls = _calls_by_id(messages)  # o placeholder do b0 não o consulta; a invalidação é que é gateada
        stale = _stale_read_indices(messages, calls) if self.harness_variant == VARIANT_B0_PLUS else {}
        prepared: list[dict[str, Any]] = []
        chars_before = 0
        chars_after = 0
        masked = 0
        for i, msg in enumerate(messages):
            new_msg = copy.deepcopy(dict(msg))
            if i in stale:  # leitura anterior a uma edição: não é mais verdade, vale sempre
                original_len = _content_len(msg.get("content"))
                marker = _stale_marker(stale[i])
                new_msg["content"] = marker
                chars_before += original_len
                chars_after += len(marker)
                masked += 1
            elif i in to_mask:
                original_len = _content_len(msg.get("content"))
                placeholder = self._placeholder_for(msg, calls)
                # Só mascara quando a troca economiza caracteres. Observação já menor que o
                # placeholder fica como está, para não aumentar o contexto enviado.
                if original_len > len(placeholder):
                    new_msg["content"] = placeholder
                    chars_before += original_len
                    chars_after += len(placeholder)
                    masked += 1
            prepared.append(new_msg)

        stats = CompactionStats(
            total_observations=len(observation_indices),
            masked_observations=masked,
            chars_before=chars_before,
            chars_after=chars_after,
        )
        return PreparedContext(messages=prepared, stats=stats)

    def _placeholder_for(self, msg: Message, calls: dict[str, tuple[str, str]]) -> str:
        """Placeholder da observação: o configurado (b0) ou o informativo (b0_plus)."""
        if self.harness_variant != VARIANT_B0_PLUS:
            return self.placeholder
        name, arguments = calls.get(str(msg.get("tool_call_id")), (str(msg.get("name", "")), ""))
        return _informative_placeholder(name, arguments, msg.get("content"))
