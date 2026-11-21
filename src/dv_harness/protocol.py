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

from collections.abc import Iterable
from typing import Any

CONTRACT_ID = "p1-run-accounting-v4"  # contrato vigente (ver experiments/p1/run-accounting-contract-v4.json)
CONTRACT_ID_V3 = "p1-run-accounting-v3"  # superado por v4; mantido para evidência anterior
CONTRACT_ID_V2 = "p1-run-accounting-v2"  # mantido sem alteração para evidência anterior
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

# Classificação de cada evento de ferramenta (v3). Não são estados do run.
FAILURE_OK = "OK"  # ferramenta executou com ok=True
# Erro esperado do conteúdo ou do ambiente de trabalho (ex.: exit_code != 0 em run_bash, arquivo
# ausente, edit ambíguo, timeout). É observação de depuração: não conta para error_limit.
FAILURE_DOMAIN = "DOMAIN_FAILURE"
# Violação de protocolo ou de contrato do harness (texto sem tool_call, JSON inválido, argumentos
# malformados, ferramenta inexistente, submit malformado, exceção interna). Conta para error_limit.
FAILURE_PROTOCOL = "PROTOCOL_ERROR"
FAILURE_KINDS: frozenset[str] = frozenset({FAILURE_OK, FAILURE_DOMAIN, FAILURE_PROTOCOL})

# Quatro baldes de tokens de entrada/saída (v3). Cada um é None quando o provedor não expõe.
# cached/cache_read e reasoning são subcampos: nunca somados ao total (sem dupla contagem).
TOKEN_BUCKETS: tuple[str, ...] = (
    "uncached_input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
)

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


# v4, regra a: telemetria é uma dimensão independente do veredito do oráculo.
# Baldes obrigatórios por chamada (contrato v4, required_buckets).
TELEMETRY_REQUIRED_BUCKETS: tuple[str, ...] = ("prompt_tokens", "completion_tokens", "total_tokens", "cost_usd")
COST_ACCOUNTING_MEASURED = "MEASURED"
COST_ACCOUNTING_INCONCLUSIVE = "INCONCLUSIVE"


def telemetry_complete(calls: Iterable[Any]) -> bool:
    """True quando todos os baldes obrigatórios de todas as chamadas foram medidos.

    Uma chamada sem os quatro baldes, ou com tentativa cujo uso não foi medido (``attempt_usage_complete``
    falso), torna o run incompleto. Zero chamadas é não medido, pela mesma regra de ``run_cost_basis``.
    Esta função nunca lê o veredito do oráculo: é ortogonal ao outcome.
    """
    materialized = list(calls)
    if not materialized:
        return False
    return all(
        all(getattr(c, bucket) is not None for bucket in TELEMETRY_REQUIRED_BUCKETS)
        and bool(c.attempt_usage_complete)
        for c in materialized
    )


def cost_accounting_status(complete: bool) -> str:
    """MEASURED quando a telemetria está completa; INCONCLUSIVE caso contrário (nunca descartado)."""
    return COST_ACCOUNTING_MEASURED if complete else COST_ACCOUNTING_INCONCLUSIVE


def is_false_finish(status: str, outcome: str | None) -> bool:
    """Término falso: o executor declarou ``completed`` e o verificador deu ``FAILURE``."""
    return status == STATUS_COMPLETED and outcome == OUTCOME_FAILURE


def false_finish_rate(runs: Iterable[tuple[str, str | None]]) -> float | None:
    """Fração de términos falsos entre runs ``completed`` com outcome verificado (SOLVED ou FAILURE).

    Runs ``completed`` sem veredito (``None`` ou ``INCONCLUSIVE``) ficam fora do denominador e
    devem ser reportados à parte. Sem denominador, o resultado é ``None``, nunca 0.
    """
    verified = [o for s, o in runs if s == STATUS_COMPLETED and o in (OUTCOME_SOLVED, OUTCOME_FAILURE)]
    if not verified:
        return None
    return sum(1 for o in verified if o == OUTCOME_FAILURE) / len(verified)


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
