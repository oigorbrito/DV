#!/usr/bin/env python3
"""Minimal DV evidence contracts: validation and per-treatment cost summaries.

Field lists and vocabularies come from experiments/schemas/dv-evidence-contracts-v1.json.
This module adds only the semantic rules that a field list cannot express. It defines no runtime,
router, orchestrator, memory or controller (see docs/ARCHITECTURE-GOVERNANCE.md).
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONTRACTS = ROOT / "experiments" / "schemas" / "dv-evidence-contracts-v1.json"
USAGE_COMPONENTS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def load_contracts(path: Path = DEFAULT_CONTRACTS) -> dict[str, Any]:
    contracts = json.loads(Path(path).read_text(encoding="utf-8"))
    if contracts.get("schema_id") != "dv-evidence-contracts-v1":
        raise ValueError("unexpected contracts schema_id")
    return contracts


def _check_fields(record: Any, contract: dict[str, Any]) -> list[str]:
    if not isinstance(record, dict):
        return ["record must be a JSON object"]
    errors = [f"missing required field: {f}" for f in contract["required"] if f not in record]
    for field, kind in contract.get("types", {}).items():
        if field not in record:
            continue
        value = record[field]
        ok = {
            "string": isinstance(value, str) and value.strip() != "",
            "boolean": isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool) and value >= 0,
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "sha256": isinstance(value, str) and bool(SHA256_RE.match(value)),
        }.get(kind, False)
        if not ok:
            errors.append(f"field {field} must be {kind}")
    for field, allowed in contract.get("enums", {}).items():
        if field in record:
            values = record[field] if isinstance(record[field], list) else [record[field]]
            for v in values:
                if v not in allowed:
                    errors.append(f"field {field} has value outside vocabulary: {v!r}")
    return errors


def validate_evidence_record(rec: Any, contracts: dict[str, Any]) -> list[str]:
    c = contracts["evidence_record"]
    errors = _check_fields(rec, c)
    if errors:
        return errors
    # Literature motivates or challenges claims; it is never NDV experimental confirmation.
    if rec["evidence_class"] == "LITERATURE_EVIDENCE" and rec["evidence_level"] != "E0":
        errors.append("LITERATURE_EVIDENCE is capped at E0")
    if rec["evidence_class"] == "ENGINEERING_OBSERVATION" and rec["evidence_level"] not in ("E0", "E1"):
        errors.append("ENGINEERING_OBSERVATION is capped at E1")
    # Support or refutation is scoped and requires controlled NDV experimental evidence (E2 or higher).
    if rec["claim_state"] in ("SUPPORTED_IN_SCOPE", "REFUTED_IN_SCOPE"):
        if rec["evidence_class"] != "NDV_EXPERIMENTAL_EVIDENCE":
            errors.append(f"{rec['claim_state']} requires NDV_EXPERIMENTAL_EVIDENCE")
        if rec["evidence_level"] not in ("E2", "E3", "E4"):
            errors.append(f"{rec['claim_state']} requires evidence level E2 or higher")
    return errors


def validate_invocation_record(rec: Any, contracts: dict[str, Any]) -> list[str]:
    c = contracts["invocation_record"]
    errors = _check_fields(rec, c)
    if errors:
        return errors
    # Failed, timed-out and blocked invocations are retained in the treatment total.
    if rec["counted_in_treatment"] is not True:
        errors.append("every invocation must be counted in its treatment (failures included)")
    if rec["outcome"] == "COMPLETED" and rec["returncode"] != 0:
        errors.append("COMPLETED invocation must have returncode 0")
    usage = rec["usage"]
    for field in c["usage_required"]:
        v = usage.get(field)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            errors.append(f"usage.{field} must be a non-negative integer")
    return errors


def validate_verification_result(rec: Any, contracts: dict[str, Any]) -> list[str]:
    c = contracts["verification_result"]
    errors = _check_fields(rec, c)
    if errors:
        return errors
    # Independent verification: the verifier must not be the executor under test.
    if rec["verifier_id"] == rec["executor_id"]:
        errors.append("verifier must be independent of the executor")
    # A PASS requires all three verified_solved conditions. Executor self-report is never sufficient.
    if rec["verdict"] == "PASS" and not (rec["task_satisfaction"] and rec["preservation_pass"] and rec["harness_valid"]):
        errors.append("PASS requires task_satisfaction, preservation_pass and harness_valid")
    return errors


def validate_capability_profile(rec: Any, contracts: dict[str, Any]) -> list[str]:
    c = contracts["capability_profile"]
    errors = _check_fields(rec, c)
    if errors:
        return errors
    if set(rec["can_change"]) & set(rec["fixed"]):
        errors.append("a dimension cannot be both changeable and fixed")
    unknown = (set(rec["can_change"]) | set(rec["fixed"])) - set(c["enums"]["can_change"])
    if unknown:
        errors.append(f"unknown dimensions: {sorted(unknown)}")
    return errors


VALIDATORS = {
    "evidence_record": validate_evidence_record,
    "invocation_record": validate_invocation_record,
    "verification_result": validate_verification_result,
    "capability_profile": validate_capability_profile,
}


def validate(kind: str, rec: Any, contracts: dict[str, Any] | None = None) -> list[str]:
    if kind not in VALIDATORS:
        raise ValueError(f"unknown contract kind: {kind}")
    return VALIDATORS[kind](rec, contracts or load_contracts())


def is_verified_solved(vr: dict[str, Any], contracts: dict[str, Any] | None = None) -> bool:
    """A result counts as verified-solved only when it validates and the independent verifier said PASS.

    executor_self_report_success is deliberately not consulted.
    """
    if validate("verification_result", vr, contracts):
        return False
    return vr["verdict"] == "PASS"


def summarize_treatment(treatment: str, invocations: list[dict[str, Any]], contracts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Aggregate every invocation of one treatment. Invalid records raise, so nothing is silently dropped."""
    contracts = contracts or load_contracts()
    counts = {"planner": 0, "executor": 0, "verifier": 0}
    totals = {k: 0 for k in USAGE_COMPONENTS}
    failed = 0
    wall = 0.0
    for i, inv in enumerate(invocations, 1):
        errors = validate("invocation_record", inv, contracts)
        if errors:
            raise ValueError(f"invocation {i} invalid: {errors}")
        if inv["treatment"] != treatment:
            raise ValueError(f"invocation {i} belongs to treatment {inv['treatment']}, not {treatment}")
        counts[inv["role"]] += 1
        if inv["outcome"] != "COMPLETED":
            failed += 1
        for k in USAGE_COMPONENTS:
            totals[k] += inv["usage"][k]
        wall += float(inv.get("wall_seconds", 0) or 0)
    return {
        "treatment": treatment,
        "invocations": len(invocations),
        "invocations_by_role": counts,
        "failed_invocations": failed,
        "usage_components": totals,
        # Single total intentionally absent: cached-input semantics are unresolved (docs/ROADMAP.md R1.2).
        "total_tokens_definition": "UNRESOLVED",
        "wall_seconds": round(wall, 3),
    }


def _main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate", help="validate one JSON record against a contract kind")
    v.add_argument("kind", choices=sorted(VALIDATORS))
    v.add_argument("file", type=Path)
    args = ap.parse_args()
    errors = validate(args.kind, json.loads(args.file.read_text(encoding="utf-8")))
    print(json.dumps({"kind": args.kind, "valid": not errors, "errors": errors}, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(_main())
