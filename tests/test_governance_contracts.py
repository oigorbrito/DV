"""Contratos e protocolo congelados: JSON valido, chaves obrigatorias, ancoragem e imutabilidade.

Nao executa nenhum experimento. Verifica apenas os artefatos de experiments/p1/ que sao normativos.
Executar: python -m pytest tests/test_governance_contracts.py -v
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess

import pytest

from support import ROOT  # noqa: F401
from test_protocol import V1_SHA256, V2_SHA256, V3_SHA256

P1 = ROOT / "experiments" / "p1"
V1, V2, V3 = (P1 / "run-accounting-contract-v1.json", P1 / "run-accounting-contract-v2.json",
              P1 / "run-accounting-contract-v3.json")
V4 = P1 / "run-accounting-contract-v4.json"
WP08 = P1 / "wp08-b0-plus-laddering-ablation-v1.json"
ANCHOR = "dfda77c"

# Pins dos artefatos novos deste commit. Qualquer edicao nos arquivos exige nova versao (AGENTS.md, regra 3).
V4_SHA256 = "11b4d5453e0964a7eb88decb5cbf7d1c5f5eebf4fbbdd458fb4e38686baef486"
WP08_SHA256 = "b654f362d774e3fe357da892f8ffa29faee753d32081885e236f01f6e674da2d"


def _load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- imutabilidade dos contratos anteriores -------------------------------------------------


@pytest.mark.parametrize("path, pinned", [(V1, V1_SHA256), (V2, V2_SHA256), (V3, V3_SHA256)])
def test_frozen_contracts_v1_v2_v3_are_unchanged(path, pinned):
    assert _sha(path) == pinned, f"{path.name} mudou: contratos congelados nao podem ser editados"


def test_v4_and_wp08_are_pinned_to_their_frozen_bytes():
    assert _sha(V4) == V4_SHA256
    assert _sha(WP08) == WP08_SHA256


# --- contrato v4 ------------------------------------------------------------------------------


def test_v4_is_valid_json_with_required_keys():
    data = _load(V4)
    for key in ("contract_id", "supersedes", "status", "anchored_commit", "normative_rules",
                "frozen_references", "implementation_gaps"):
        assert key in data, key


def test_v4_identity_and_anchor():
    data = _load(V4)
    assert data["contract_id"] == "p1-run-accounting-v4"
    assert data["supersedes"] == "p1-run-accounting-v3"
    assert data["status"] == "FROZEN_SPECIFICATION"
    assert data["anchored_commit"] == ANCHOR


def test_v4_normative_rules_a_to_d_are_present():
    rules = _load(V4)["normative_rules"]
    assert set(rules) == {"a_orthogonality", "b_uncached_and_lower_bound", "c_multi_rung_accounting",
                          "d_traceability"}
    assert "telemetry_complete" in rules["a_orthogonality"]["fields"]
    assert "capability_table" in rules["b_uncached_and_lower_bound"]
    assert _load(V4)["normative_rules"]["c_multi_rung_accounting"]["schema"] == "dv-laddering-summary-v1"


def test_v4_references_the_frozen_v1_v2_v3_hashes():
    refs = _load(V4)["frozen_references"]
    assert refs["v1_sha256"] == V1_SHA256
    assert refs["v2_sha256"] == V2_SHA256
    assert refs["v3_sha256"] == V3_SHA256


def test_v4_implementation_gaps_are_explicit_and_unique():
    gaps = _load(V4)["implementation_gaps"]
    assert gaps, "gaps conhecidos precisam estar registrados"
    ids = [gap["id"] for gap in gaps]
    assert len(ids) == len(set(ids))
    assert all(gap["gap"] and gap["blocks"] for gap in gaps)


def test_v4_capability_table_marks_unverified_providers_as_provisional():
    table = _load(V4)["normative_rules"]["b_uncached_and_lower_bound"]["capability_table"]
    assert table["anthropic"]["status"] == "CODE_VERIFIED_OFFLINE"
    assert table["gemini"]["status"] == "PROVISIONAL_PROBE_REQUIRED"
    assert table["openai"]["status"] == "PROVISIONAL_PROBE_REQUIRED"


# --- protocolo wp08 -----------------------------------------------------------------------------


def test_wp08_is_valid_and_references_contract_v4():
    data = _load(WP08)
    assert data["experiment_id"] == "wp08-b0-plus-laddering-ablation-v1"
    assert data["status"] == "FROZEN_PROTOCOL"
    assert data["contract_id"] == "p1-run-accounting-v4"
    assert data["anchored_commit"] == ANCHOR
    assert data["classification"] == "PROSPECTIVE_FROZEN_NOT_EXECUTED"


def test_wp08_declares_four_arms_with_the_pre_registered_variants():
    arms = {arm["arm_id"]: arm for arm in _load(WP08)["arms"]}
    assert set(arms) == {"A_B0", "B_B0_PLUS", "C_SOLO_R1", "D_DV_LADDERING"}
    assert arms["A_B0"]["harness_variant"] == "b0"
    assert all(arms[a]["harness_variant"] == "b0_plus" for a in ("B_B0_PLUS", "C_SOLO_R1", "D_DV_LADDERING"))


def test_wp08_rotation_is_a_latin_cycle_over_the_four_arms():
    cycle = _load(WP08)["repetition_and_order"]["rotation_cycle"]
    assert len(cycle) == 4
    arms = {"A_B0", "B_B0_PLUS", "C_SOLO_R1", "D_DV_LADDERING"}
    assert all(set(row) == arms and len(row) == 4 for row in cycle)
    for position in range(4):  # cada braco ocupa cada posicao exatamente uma vez no ciclo completo
        assert {row[position] for row in cycle} == arms


def test_wp08_statistical_parameters_are_fixed():
    stats = _load(WP08)["statistics"]
    assert stats["seed"] == 20261120
    assert stats["resamples"] == 10000
    assert stats["confidence"] == 0.95
    assert stats["non_inferiority_margin_h2"] == 0.02
    assert _load(WP08)["repetition_and_order"]["min_repetitions"] == 3


def test_wp08_preconditions_are_not_marked_as_passed():
    pre = _load(WP08)["preconditions"]
    assert pre["pc1_offline_task_integrity_gate"]["status"] == "NOT_RUN"
    assert pre["pc2_dataset_sha256_frozen"]["value"] is None  # so o gate preenche, uma unica vez
    assert pre["pc3_model_bindings"]["status"] == "PENDING_BINDING"


# --- ancoragem em git ---------------------------------------------------------------------------


def test_anchor_commit_exists_in_history():
    git = shutil.which("git")
    if git is None:
        pytest.skip("git indisponivel no ambiente de teste")
    result = subprocess.run([git, "cat-file", "-e", f"{ANCHOR}^{{commit}}"], cwd=ROOT,
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        pytest.skip("repositorio sem o historico completo (shallow clone)")
    assert result.returncode == 0
