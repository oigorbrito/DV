"""Protocolo p1 v2: mapeamento de outcome, cost_basis, e conformidade entre contrato JSON e código.

Executar: python -m pytest tests/test_protocol.py -v
"""

from __future__ import annotations

import hashlib
import itertools
import json

import pytest

from support import ROOT  # noqa: F401  (ajusta sys.path)
from dv_harness import protocol as p

V1_PATH = ROOT / "experiments" / "p1" / "run-accounting-contract-v1.json"
V2_PATH = ROOT / "experiments" / "p1" / "run-accounting-contract-v2.json"
# Hash do v1 no commit e826f8e. Qualquer mudança no v1 falha este teste.
V1_SHA256 = "3f1366846c095f3630e2f4bbaf0eb2555d8281fce8f1d9882f2952130813d86b"
V3_PATH = ROOT / "experiments" / "p1" / "run-accounting-contract-v3.json"
# Hash do v2 no commit 8cc338d (antes do v3). Qualquer mudança no v2 falha este teste.
V2_SHA256 = "922f9a544468d69a60649fd59ff785e46b60d348815a9ce4e13419002d5e5d36"
# Hash do v3 no commit 7298715 (contrato vigente). Qualquer mudança no v3 falha este teste.
V3_SHA256 = "6f50e1187d1f9e9c9c268122532e58dda451f55ec538581fa860fb33301fef94"


def test_v1_contract_is_unchanged():
    assert hashlib.sha256(V1_PATH.read_bytes()).hexdigest() == V1_SHA256


def test_v2_contract_is_unchanged():
    assert hashlib.sha256(V2_PATH.read_bytes()).hexdigest() == V2_SHA256


def test_v2_contract_parses_and_names_v1_as_superseded():
    doc = json.loads(V2_PATH.read_text(encoding="utf-8"))
    assert doc["contract_id"] == p.CONTRACT_ID_V2
    assert doc["supersedes"] == "p1-run-accounting-v1"


def test_v2_states_match_code():
    doc = json.loads(V2_PATH.read_text(encoding="utf-8"))
    layer1 = set(doc["state_model"]["layer_1_executor_status"]["states"])
    assert layer1 == set(p.EXECUTOR_STATUSES)
    assert set(doc["state_model"]["layer_2_outcome"]["values"]) == {p.OUTCOME_SOLVED, p.OUTCOME_FAILURE, p.OUTCOME_INCONCLUSIVE}


def test_v2_partitions_match_code_and_are_eight():
    doc = json.loads(V2_PATH.read_text(encoding="utf-8"))
    assert tuple(doc["partitions"]) == p.PARTITIONS
    assert len(p.PARTITIONS) == 8


def test_v2_metric_and_cost_basis_match_code():
    doc = json.loads(V2_PATH.read_text(encoding="utf-8"))
    assert doc["metric_v2"]["name"] == p.METRIC_NAME == "CORE_TOKENS_PARTIAL"
    assert tuple(doc["cost_basis"]["values"]) == (p.COST_BASIS_PRICED, p.COST_BASIS_ZERO, p.COST_BASIS_UNKNOWN)


def test_every_partition_has_a_status_in_contract():
    doc = json.loads(V2_PATH.read_text(encoding="utf-8"))
    assert set(doc["partition_status_v2"]) == set(p.PARTITIONS)


@pytest.mark.parametrize("status,verdict", list(itertools.product(sorted(p.EXECUTOR_STATUSES), [None, "PASS", "FAIL"])))
def test_outcome_table_exhaustive(status, verdict):
    expected = {
        (True, None): p.OUTCOME_INCONCLUSIVE,
        (True, "PASS"): p.OUTCOME_SOLVED,
        (True, "FAIL"): p.OUTCOME_FAILURE,
    }
    fixed = status in (p.STATUS_UNVERIFIABLE, p.STATUS_PROVIDER_ERROR)
    if fixed:
        assert p.outcome_for(status, verdict) == p.OUTCOME_INCONCLUSIVE
    else:
        assert p.outcome_for(status, verdict) == expected[(True, verdict)]


def test_executor_done_is_not_solved_without_verdict():
    assert p.outcome_for(p.STATUS_COMPLETED, None) == p.OUTCOME_INCONCLUSIVE


def test_outcome_rejects_unknown_inputs():
    with pytest.raises(ValueError):
        p.outcome_for("done", None)
    with pytest.raises(ValueError):
        p.outcome_for(p.STATUS_COMPLETED, "MAYBE")


@pytest.mark.parametrize("cost,basis", [(None, "unknown"), (0.0, "priced_zero_by_table"), (0.5, "priced_by_table")])
def test_cost_basis_for(cost, basis):
    assert p.cost_basis_for(cost) == basis


@pytest.mark.parametrize("bases,expected", [
    ([], "unknown"),
    (["priced_by_table", "unknown"], "unknown"),
    (["priced_zero_by_table", "priced_zero_by_table"], "priced_zero_by_table"),
    (["priced_zero_by_table", "priced_by_table"], "priced_by_table"),
    (["priced_by_table"], "priced_by_table"),
])
def test_run_cost_basis(bases, expected):
    assert p.run_cost_basis(bases) == expected


def test_unmeasured_reasons_are_non_empty_strings():
    for key, reason in p.UNMEASURED_REASONS.items():
        assert isinstance(reason, str) and reason.strip(), key


# --- contrato v3: baldes de tokens, classificação de falhas, false_finish --------


def _v3():
    return json.loads(V3_PATH.read_text(encoding="utf-8"))


def test_v3_contract_is_unchanged():
    assert hashlib.sha256(V3_PATH.read_bytes()).hexdigest() == V3_SHA256


def test_v3_contract_names_v2_as_superseded_and_matches_code_id():
    doc = _v3()
    assert doc["contract_id"] == p.CONTRACT_ID == "p1-run-accounting-v3"
    assert doc["supersedes"] == p.CONTRACT_ID_V2


def test_v3_states_partitions_and_cost_basis_match_code():
    doc = _v3()
    assert set(doc["state_model"]["layer_1_executor_status"]["states"]) == set(p.EXECUTOR_STATUSES)
    assert tuple(doc["partitions"]) == p.PARTITIONS
    assert tuple(doc["cost_basis"]["values"]) == (p.COST_BASIS_PRICED, p.COST_BASIS_ZERO, p.COST_BASIS_UNKNOWN)


def test_v3_declares_the_four_token_buckets_and_nothing_else():
    buckets = _v3()["token_buckets"]
    assert tuple(k for k in buckets if k in p.TOKEN_BUCKETS) == p.TOKEN_BUCKETS
    assert "uncached_input_tokens" in buckets and "reasoning_tokens" in buckets


def test_v3_declares_failure_classes_and_false_finish():
    doc = _v3()
    assert set(doc["tool_failure_classification"]) >= {"OK", "DOMAIN_FAILURE", "PROTOCOL_ERROR"}
    assert "false_finish_rate" in doc["false_finish"]
    assert set(doc["infra_telemetry"]["environment"]) == {"cpus", "memory", "pids_limit", "timeout_s", "network", "image"}


def test_failure_kinds_are_the_three_documented_classes():
    assert {"OK", "DOMAIN_FAILURE", "PROTOCOL_ERROR"} == p.FAILURE_KINDS


@pytest.mark.parametrize("status,outcome,expected", [
    ("completed", "FAILURE", True),
    ("completed", "SOLVED", False),
    ("completed", "INCONCLUSIVE", False),
    ("completed", None, False),
    ("max_steps_reached", "FAILURE", False),
    ("error_limit_reached", "FAILURE", False),
])
def test_is_false_finish(status, outcome, expected):
    assert p.is_false_finish(status, outcome) is expected


def test_false_finish_rate_uses_only_verified_completed_runs():
    runs = [
        ("completed", "FAILURE"),       # false finish
        ("completed", "SOLVED"),
        ("completed", "INCONCLUSIVE"),  # sem veredito: fora do denominador
        ("completed", None),            # sem veredito: fora do denominador
        ("max_steps_reached", "FAILURE"),  # não é término declarado: fora
    ]
    assert p.false_finish_rate(runs) == pytest.approx(0.5)


def test_false_finish_rate_is_none_not_zero_without_denominator():
    assert p.false_finish_rate([]) is None
    assert p.false_finish_rate([("max_steps_reached", "FAILURE"), ("completed", None)]) is None
