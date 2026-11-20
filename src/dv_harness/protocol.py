"""Protocolo p1 v2: constantes, mapeamento de estados e enums do contrato.

Fonte normativa: ``experiments/p1/run-accounting-contract-v2.json``. O v1
(``run-accounting-contract-v1.json``) permanece intacto e não é editado.

Duas camadas de estado:
- ``status``: estado de término do executor (``AgentLoop``). Não é evidência de sucesso.
- ``outcome``: ``SOLVED`` | ``FAILURE`` | ``INCONCLUSIVE``, atribuído somente a partir do
  veredito de um verificador independente. ``EXECUTOR_DONE != VERIFIED_SOLVED``.

Este módulo não chama LLM nem ferramentas. Só define vocabulário e a função pura
``outcome_for``, para que a tabela da especificação seja testada diretamente.
"""

from __future__ import annotations

CONTRACT_ID = "p1-run-accounting-v2"
METRIC_NAME = "CORE_TOKENS_PARTIAL"

# Partições explícitas do contrato. Toda partição aparece em AgentResult.partitions,
# com valor medido ou None (e motivo em unmeasured).
PARTITIONS: tuple[str, ...] = (
    "routing",
    "task_shaping",
    "planning",
    "context_input",
    "execution",
    "escalation_handoff",
    "verification",
    "retry_replanning",
)

# Camada 1: status do executor.
STATUS_COMPLETED = "completed"  # submissão explícita via ferramenta submit
STATUS_MAX_STEPS = "max_steps_reached"
STATUS_BUDGET = "budget_exceeded"
STATUS_UNVERIFIABLE = "budget_unverifiable"
STATUS_PROVIDER_ERROR = "provider_error"
STATUS_ERROR_LIMIT = "error_limit_reached"  # extensão de v2: teto de erros consecutivos de formato

EXECUTOR_STATUSES: frozenset[str] = frozenset({
    STATUS_COMPLETED,
    STATUS_MAX_STEPS,
    STATUS_BUDGET,
    STATUS_UNVERIFIABLE,
    STATUS_PROVIDER_ERROR,
    STATUS_ERROR_LIMIT,
})

# Camada 2: resultado do run, a partir do veredito do verificador.
OUTCOME_SOLVED = "SOLVED"
OUTCOME_FAILURE = "FAILURE"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"

# cost_basis: distingue custo real de preço zero da tabela e de custo desconhecido.
COST_BASIS_PRICED = "priced_by_table"
COST_BASIS_ZERO = "priced_zero_by_table"
COST_BASIS_UNKNOWN = "unknown"


def cost_basis_for(cost_usd: float | None) -> str:
    """Base do custo de uma chamada. ``None`` é desconhecido; ``0.0`` vindo da tabela é zero de tabela."""
    if cost_usd is None:
        return COST_BASIS_UNKNOWN
    return COST_BASIS_ZERO if cost_usd == 0.0 else COST_BASIS_PRICED


def run_cost_basis(call_bases: list[str]) -> str:
    """Base do custo de um run, a partir das bases de cada chamada.

    - Qualquer ``unknown`` torna o run ``unknown``.
    - Todas ``priced_zero_by_table`` torna o run ``priced_zero_by_table``.
    - Caso contrário ``priced_by_table``. Chamadas zero contribuem 0 ao total e
      são contadas à parte em ``zero_priced_calls``.
    - Run sem chamadas é ``unknown``, porque nada foi medido.
    """
    if not call_bases or COST_BASIS_UNKNOWN in call_bases:
        return COST_BASIS_UNKNOWN
    if all(b == COST_BASIS_ZERO for b in call_bases):
        return COST_BASIS_ZERO
    return COST_BASIS_PRICED


def outcome_for(status: str, verdict: str | None) -> str:
    """Mapeia (status do executor, veredito do verificador) para o outcome do run.

    Tabela da especificação §1.3, com a extensão ``error_limit_reached``, que segue a
    regra de ``completed`` e ``max_steps_reached``:

    - ``budget_unverifiable`` e ``provider_error`` são sempre ``INCONCLUSIVE``.
    - Sem veredito, o resultado é ``INCONCLUSIVE``. O status nunca vira evidência.
    - Com veredito, ``PASS`` vira ``SOLVED`` e ``FAIL`` vira ``FAILURE``.
    """
    if status not in EXECUTOR_STATUSES:
        raise ValueError(f"unknown executor status: {status!r}")
    if verdict not in (None, VERDICT_PASS, VERDICT_FAIL):
        raise ValueError(f"unknown verdict: {verdict!r}")
    if status in (STATUS_UNVERIFIABLE, STATUS_PROVIDER_ERROR):
        return OUTCOME_INCONCLUSIVE
    if verdict is None:
        return OUTCOME_INCONCLUSIVE
    return OUTCOME_SOLVED if verdict == VERDICT_PASS else OUTCOME_FAILURE


# Motivos padronizados para campos não medidos (I6: nunca 0, 0.0 ou "" como sentinela).
UNMEASURED_REASONS: dict[str, str] = {
    "verification": "outside core: no independent verifier attached; executor status is not verification",
    "routing": "outside core: single model configured, no routing layer",
    "planning": "outside core: no planning layer",
    "task_shaping": "outside core: no task shaping layer",
    "context_input": "not separable: prompt tokens of each call cover context and instructions together",
    "escalation_handoff": "outside core: no escalation layer",
    "retry_replanning": "outside core: no agent-level replanning; provider-level retries are in provider_attempts",
    "outcome": "no independent verifier attached in core; outcome is assigned by the evaluator",
    "pricing_date": "not exposed by litellm; pricing_source records only the litellm version",
    "run_identity": "not supplied by caller (run_id is None)",
    "verifier_cost": "outside core: no verifier",
}
