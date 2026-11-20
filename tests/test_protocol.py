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


def test_v1_contract_is_unchanged():
    assert hashlib.sha256(V1_PATH.read_bytes()).hexdigest() == V1_SHA256


def test_v2_contract_parses_and_names_v1_as_superseded():
    doc = json.loads(V2_PATH.read_text(encoding="utf-8"))
    assert doc["contract_id"] == p.CONTRACT_ID
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
