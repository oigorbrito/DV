#!/usr/bin/env python3
"""Luna D0 (LUNA_DIRECT_VERIFY) planner: dry-run only.

Produces a manifest of what the D0 treatment WOULD run for admitted tasks. It never starts a
process. Execution (R2.2) is intentionally not implemented here.

Refuses (exit 2) when:
  - the Luna protocol is not the frozen D0 definition (one executor, zero planner, zero retry/repair);
  - the D0 budget is not frozen (per-invocation ceilings are required by the protocol release gate).

Plans only tasks whose admission record is schema ndv-p1-s2-admission-record-v2 with status ADMITTED_FROZEN.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROTOCOL = ROOT / "experiments" / "luna-only" / "luna-composition-protocol-v1.json"
DEFAULT_BUDGET = ROOT / "experiments" / "luna-only" / "luna-d0-budget-v1.json"
MANIFEST_SCHEMA = "dv-luna-d0-dry-run-manifest-v1"
ADMISSION_SCHEMA = "ndv-p1-s2-admission-record-v2"


class Refusal(ValueError):
    """The runner refuses to plan. The message names the failed gate."""


def check_protocol(protocol: dict[str, Any]) -> dict[str, Any]:
    if protocol.get("status") != "PROSPECTIVE_FROZEN_NOT_EXECUTED":
        raise Refusal("protocol status must be PROSPECTIVE_FROZEN_NOT_EXECUTED")
    if protocol.get("treatment_execution") != "NOT_EXECUTED" or protocol.get("holdout_access") != "NONE":
        raise Refusal("protocol must record NOT_EXECUTED and holdout_access NONE")
    d0 = (protocol.get("treatments") or {}).get("D0")
    if not isinstance(d0, dict):
        raise Refusal("protocol has no D0 treatment")
    inv = d0.get("invocations") or {}
    if inv.get("planner") != 0 or inv.get("executor") != 1:
        raise Refusal("D0 must have exactly 0 planner and 1 executor invocations")
    if d0.get("retry_limit") != 0 or d0.get("repair_limit") != 0:
        raise Refusal("D0 must have retry_limit 0 and repair_limit 0")
    if (protocol.get("controls") or {}).get("dynamic_model_routing") is not False:
        raise Refusal("dynamic model routing must be disabled")
    return d0


def check_budget(budget: dict[str, Any]) -> dict[str, int]:
    if budget.get("status") != "FROZEN":
        raise Refusal(f"D0 budget is {budget.get('status')!r}, not FROZEN; the protocol requires frozen ceilings")
    exec_ms, ver_ms = budget.get("executor_timeout_ms"), budget.get("verification_timeout_ms")
    for name, v in (("executor_timeout_ms", exec_ms), ("verification_timeout_ms", ver_ms)):
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise Refusal(f"{name} must be a positive integer in a FROZEN budget")
    if budget.get("retry_limit") != 0 or budget.get("repair_limit") != 0:
        raise Refusal("D0 budget must keep retry_limit and repair_limit at 0")
    return {"executor_timeout_ms": exec_ms, "verification_timeout_ms": ver_ms}


def admitted_tasks(admission_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only records that are schema-correct and ADMITTED_FROZEN. Everything else is excluded and counted."""
    out = []
    for rec in admission_records:
        if rec.get("schema_id") == ADMISSION_SCHEMA and rec.get("status") == "ADMITTED_FROZEN":
            out.append({"task_id": rec["candidate_id"], "admission_schema": ADMISSION_SCHEMA})
    return out


def plan(protocol: dict[str, Any], budget: dict[str, Any], admission_records: list[dict[str, Any]]) -> dict[str, Any]:
    check_protocol(protocol)
    ceilings = check_budget(budget)
    tasks = admitted_tasks(admission_records)
    planned = []
    for t in tasks:
        planned.append({
            "task_id": t["task_id"],
            "treatment": "D0",
            "invocations": [{"index": 1, "role": "executor", "timeout_ms": ceilings["executor_timeout_ms"]}],
            "verifier": {"required": True, "independent_of_executor": True, "timeout_ms": ceilings["verification_timeout_ms"]},
            "retry_limit": 0,
            "repair_limit": 0,
        })
    return {
        "schema_id": MANIFEST_SCHEMA,
        "status": "PLANNED_NOT_EXECUTED" if planned else "NO_ADMITTED_TASKS",
        "treatment": "D0",
        "planned_tasks": len(planned),
        "excluded_records": len(admission_records) - len(tasks),
        "planned": planned,
        "execution": "NOT_IMPLEMENTED (dry-run only; see docs/ROADMAP.md R2.2)",
        "treatment_execution": "NOT_EXECUTED",
        "holdout_access": "NONE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _load_admissions(directory: Path) -> list[dict[str, Any]]:
    records = []
    for path in sorted(directory.glob("*.json")):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    ap.add_argument("--budget", type=Path, default=DEFAULT_BUDGET)
    ap.add_argument("--admission-dir", type=Path, required=True, help="directory with admission record JSON files")
    ap.add_argument("--out", type=Path, help="write the manifest here (must not exist)")
    args = ap.parse_args()
    try:
        manifest = plan(
            json.loads(args.protocol.read_text(encoding="utf-8")),
            json.loads(args.budget.read_text(encoding="utf-8")),
            _load_admissions(args.admission_dir),
        )
    except (Refusal, OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "REFUSED", "reason": str(exc)}, indent=2))
        return 2
    text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    if args.out:
        if args.out.exists():
            print(json.dumps({"status": "REFUSED", "reason": "output exists; refusing overwrite"}, indent=2))
            return 2
        args.out.write_text(text, encoding="utf-8")
    print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
