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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

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


class ContextManager:
    """Aplica Observation Masking a uma lista de mensagens.

    ``keep_recent_observations``: quantas observações ``tool`` mais recentes mantêm o conteúdo
    original. As demais são mascaradas. ``0`` mascara todas.
    """

    def __init__(
        self,
        keep_recent_observations: int = DEFAULT_KEEP_RECENT_OBSERVATIONS,
        placeholder: str = DEFAULT_PLACEHOLDER,
    ) -> None:
        if not isinstance(keep_recent_observations, int) or isinstance(keep_recent_observations, bool):
            raise TypeError("keep_recent_observations must be an int")
        if keep_recent_observations < 0:
            raise ValueError("keep_recent_observations must be >= 0")
        self.keep_recent_observations = keep_recent_observations
        self.placeholder = placeholder

    def prepare(self, messages: Sequence[Message]) -> PreparedContext:
        """Devolve cópia preparada para envio ao LLM. ``messages`` não é alterado."""
        observation_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        keep_from = max(0, len(observation_indices) - self.keep_recent_observations)
        to_mask = set(observation_indices[:keep_from])

        placeholder_len = len(self.placeholder)
        prepared: list[dict[str, Any]] = []
        chars_before = 0
        chars_after = 0
        masked = 0
        for i, msg in enumerate(messages):
            new_msg = copy.deepcopy(dict(msg))
            if i in to_mask:
                original_len = _content_len(msg.get("content"))
                # Só mascara quando a troca economiza caracteres. Observação já menor que o
                # placeholder fica como está, para não aumentar o contexto enviado.
                if original_len > placeholder_len:
                    new_msg["content"] = self.placeholder
                    chars_before += original_len
                    chars_after += placeholder_len
                    masked += 1
            prepared.append(new_msg)

        stats = CompactionStats(
            total_observations=len(observation_indices),
            masked_observations=masked,
            chars_before=chars_before,
            chars_after=chars_after,
        )
        return PreparedContext(messages=prepared, stats=stats)
