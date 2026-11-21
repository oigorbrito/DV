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


# --- amarração wp08-binding-v1 (pc1-pc6) e pins de código/dataset -------------------------------
# Pins dos artefatos desta amarração. Mudança em qualquer um exige nova versão (AGENTS.md, regra 3).
BINDING = P1 / "wp08-binding-v1.json"
MANIFEST = P1 / "wp08-tasks" / "screening-n10-manifest-v1.json"
REPORT = P1 / "wp08-integrity-report-v1.json"
ANALYSIS = ROOT / "src" / "dv_harness" / "ablation_analysis.py"
BINDING_SHA256 = "208f8623eb824ee3e5ff647538aa91124a313d6ab7d6431c8b5c8977c83fe5fd"
MANIFEST_SHA256 = "e361befd750cf48e70463a34718613521900a050b2a19431cfcf8f2c0115ae21"
REPORT_SHA256 = "e24f9079e764066eaaef02535844d7631d2bc1442da9fedee93d13492772608f"
DATASET_SHA256 = "a14461c8c24960108601c7983d8e21d28a28303ed618fa827cabea3b6af4ecc5"
ANALYSIS_CODE_SHA256 = "16f7a44aae8e713119363106672d0e6f983bfb697f8d54b898f678a405a8b488"
TASK_IDS = ["f1-greet", "f1-clamp", "f2-pricing", "f2-grades", "f3-percent", "f3-bytes", "f4-leap",
            "f4-roman", "f5-money", "f6-cli-config"]
FILLED = {"PASSED", "FROZEN", "FILLED"}


def _binding():
    return _load(BINDING)["preconditions"]


def test_binding_and_its_inputs_are_pinned_byte_for_byte():
    assert _sha(BINDING) == BINDING_SHA256
    assert _sha(MANIFEST) == MANIFEST_SHA256
    assert _sha(REPORT) == REPORT_SHA256


def test_binding_identity_points_to_the_frozen_protocol_and_contract_v4():
    data = _load(BINDING)
    assert data["binding_id"] == "wp08-binding-v1"
    assert data["binds_protocol"] == "wp08-b0-plus-laddering-ablation-v1"
    assert data["binds_protocol_sha256"] == WP08_SHA256
    assert data["contract_id"] == "p1-run-accounting-v4"
    assert data["contract_sha256"] == V4_SHA256
    assert data["anchored_commit"] == "7a99cf8"
    assert data["status"] == "FROZEN_BINDING_AWAITING_PC5_PROBE"


def test_pc6_analysis_code_hash_matches_the_module_and_the_pin():
    from dv_harness import ablation_analysis as aa

    assert aa.analysis_code_sha256(ANALYSIS) == ANALYSIS_CODE_SHA256
    assert _binding()["pc6_analysis_code_frozen"]["analysis_code_sha256"] == ANALYSIS_CODE_SHA256


def test_pc1_integrity_report_passed_for_all_tasks_with_no_violations():
    report = _load(REPORT)
    assert report["passed"] is True
    assert report["dataset_sha256"] == DATASET_SHA256
    assert [t["task_id"] for t in report["task_reports"]] == TASK_IDS
    assert all(t["passed"] and t["violations"] == [] for t in report["task_reports"])
    pc1 = _binding()["pc1_offline_task_integrity_gate"]
    assert pc1["status"] == "PASSED" and pc1["integrity_gate_passed"] is True
    assert pc1["report_sha256"] == REPORT_SHA256 and pc1["task_count"] == len(TASK_IDS)


def test_pc2_dataset_sha256_is_recomputed_from_the_pinned_manifest_offline():
    from dv_harness import batch
    from dv_harness.task_integrity import dataset_fingerprint

    suite_id, tasks, file_sha = batch.load_manifest(MANIFEST)  # sem oráculo: só definição de tarefa
    assert suite_id == "screening-n10" and file_sha == MANIFEST_SHA256
    assert [t.task_id for t in tasks] == TASK_IDS
    assert dataset_fingerprint(tasks) == DATASET_SHA256
    pc2 = _binding()["pc2_dataset_sha256_frozen"]
    assert pc2["status"] == "FROZEN" and pc2["dataset_sha256"] == DATASET_SHA256
    assert pc2["tasks_source_sha256"] == MANIFEST_SHA256 and pc2["task_ids"] == TASK_IDS


def test_pc3_models_are_priced_in_the_litellm_table_with_the_recorded_values():
    import litellm

    pc3 = _binding()["pc3_model_bindings"]
    assert pc3["status"] == "FILLED"
    assert pc3["cost_share_cap_sum"] == pytest.approx(1.0)
    assert pc3["pricing_source"].startswith("litellm==1.110.1")
    for rung in ("rung_0", "rung_1"):
        model = pc3[rung]["model_identity"]
        entry = litellm.model_cost[model]  # KeyError = modelo sem preço cadastrado
        assert entry["input_cost_per_token"] == pc3[rung]["price_usd_per_token"]["input"]
        assert entry["output_cost_per_token"] == pc3[rung]["price_usd_per_token"]["output"]
        assert entry["supports_function_calling"] is True
        assert pc3[rung]["reasoning_effort"] is None


def test_pc4_budget_and_rotation_arithmetic():
    pc4 = _binding()["pc4_global_budget"]
    assert pc4["status"] == "FILLED"
    assert pc4["repetitions_n"] == 4 and pc4["repetitions_n"] % 4 == 0
    cycle = pc4["rotation_cycle"]
    arms = {"A_B0", "B_B0_PLUS", "C_SOLO_R1", "D_DV_LADDERING"}
    assert len(cycle) == 4 and all(set(row) == arms for row in cycle)
    assert all({row[pos] for row in cycle} == arms for pos in range(4))  # quadrado latino completo
    assert pc4["batch_budget_usd"] == pytest.approx(pc4["task_count"] * pc4["per_task_budget_usd"])
    assert pc4["task_count"] == len(TASK_IDS)
    assert pc4["arm_budgets"]["D_DV_LADDERING"]["policy_global_max_cost_usd"] == pc4["per_task_budget_usd"]


def test_only_pc5_is_pending_and_the_pc4_budget_incompatibility_is_recorded_open():
    pre = _binding()
    assert pre["pc5_capability_probe"]["status"] == "PENDING_LIVE_PROBE"
    assert pre["pc5_capability_probe"]["probe_executed"] is False
    for key in ("pc1_offline_task_integrity_gate", "pc2_dataset_sha256_frozen", "pc3_model_bindings",
                "pc4_global_budget", "pc6_analysis_code_frozen"):
        assert pre[key]["status"] in FILLED, key
    # Além do pc5, o binding registra uma incompatibilidade de orçamento. Ela não é escondida.
    assert pre["pc4_global_budget"]["known_incompatibility"]["status"] == "OPEN_DECISION_REQUIRED"
    assert _load(BINDING)["open_items_before_live_run"] == ["pc5_capability_probe",
                                                            "known_incompatibility (pc4)"]


def test_wp08_preconditions_in_the_frozen_protocol_are_still_unfilled():
    # O wp08 é imutável: os valores ficam no binding, e o protocolo continua mostrando o estado original.
    pre = _load(P1 / "wp08-b0-plus-laddering-ablation-v1.json")["preconditions"]
    assert pre["pc1_offline_task_integrity_gate"]["status"] == "NOT_RUN"
    assert pre["pc2_dataset_sha256_frozen"]["value"] is None
    assert pre["pc6_analysis_code_frozen"]["analysis_code_sha256"] is None
