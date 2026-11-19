#!/usr/bin/env python3
"""DV claim experiment accounting report.

Summarizes normalized arm rows (baseline vs. treatment) using the metrics and
partitions of experiments/p1/run-accounting-contract-v1.json.

Rules enforced here:
- EXECUTOR_DONE != VERIFIED_SOLVED: only verified == VERIFIED_SOLVED counts as solved.
- FREE != ZERO_COST: missing telemetry is null and blocks the primary metric; it is never zero.
- Failure cost rule: tokens and USD of failed, inconclusive and pending rows are kept.
- No composite score. Output is descriptive; decision is always NOT_DECIDED.
- Pending verification blocks the primary metric until resolved.

Input schema: dv-claim-arm-rows-v1 (see docs/research/CLAIM-EXPERIMENT-RUNNER.md).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ACCOUNTING_REL = "experiments/p1/run-accounting-contract-v1.json"
ARMS = ("baseline", "treatment")
VERIFIED_STATES = {"VERIFIED_SOLVED", "NOT_SOLVED", "INCONCLUSIVE", "PENDING_VERIFICATION"}
NUMERIC_FIELDS = ("total_system_tokens", "usd", "wall_seconds")


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_rows(payload: dict[str, Any], partitions: list[str]) -> list[str]:
    errors = []
    if payload.get("schema_id") != "dv-claim-arm-rows-v1":
        errors.append("schema_id must be dv-claim-arm-rows-v1")
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        errors.append("rows must be a non-empty list")
        return errors
    seen = set()
    for i, row in enumerate(rows):
        where = f"row[{i}]"
        if row.get("arm") not in ARMS:
            errors.append(f"{where}.arm must be baseline|treatment")
        if not row.get("run_id"):
            errors.append(f"{where}.run_id required")
        elif (row["arm"], row["run_id"]) in seen:
            errors.append(f"{where} duplicate run_id {row['run_id']} in arm {row['arm']}")
        seen.add((row.get("arm"), row.get("run_id")))
        if row.get("verified") not in VERIFIED_STATES:
            errors.append(f"{where}.verified must be one of {sorted(VERIFIED_STATES)}")
        for field in NUMERIC_FIELDS:
            value = row.get(field)
            if value is not None and (not isinstance(value, (int, float)) or value < 0):
                errors.append(f"{where}.{field} must be a non-negative number or null")
        parts = row.get("partition_tokens", {}) or {}
        unknown = set(parts) - set(partitions)
        if unknown:
            errors.append(f"{where}.partition_tokens has keys outside the contract: {sorted(unknown)}")
    return errors


def summarize_arm(rows: list[dict[str, Any]], partitions: list[str]) -> dict[str, Any]:
    n = len(rows)
    verified = sum(r["verified"] == "VERIFIED_SOLVED" for r in rows)
    pending = sum(r["verified"] == "PENDING_VERIFICATION" for r in rows)
    inconclusive = sum(r["verified"] == "INCONCLUSIVE" for r in rows)
    not_solved = sum(r["verified"] == "NOT_SOLVED" for r in rows)

    tokens_missing = sum(r.get("total_system_tokens") is None for r in rows)
    usd_missing = sum(r.get("usd") is None for r in rows)
    wall_known = [r["wall_seconds"] for r in rows if r.get("wall_seconds") is not None]

    total_tokens = None if tokens_missing else sum(r["total_system_tokens"] for r in rows)
    total_usd = None if usd_missing else round(sum(r["usd"] for r in rows), 6)

    primary_reason = None
    if pending:
        primary_reason = f"{pending} run(s) PENDING_VERIFICATION; resolve verification first"
    elif tokens_missing:
        primary_reason = f"{tokens_missing} run(s) without total_system_tokens (telemetry missing, not zero)"
    elif verified == 0:
        primary_reason = "no VERIFIED_SOLVED runs; primary metric undefined"
    primary = None if primary_reason else round(total_tokens / verified, 4)
    usd_per_verified = None if (usd_missing or pending or verified == 0) else round(total_usd / verified, 6)

    partition_totals: dict[str, Any] = {}
    for part in partitions:
        vals = [(r.get("partition_tokens") or {}).get(part) for r in rows]
        if any(v is None for v in vals):
            partition_totals[part] = {"total": None, "rows_not_reported": sum(v is None for v in vals)}
        else:
            partition_totals[part] = {"total": sum(vals), "rows_not_reported": 0}

    return {
        "runs": n,
        "verified_solved": verified,
        "not_solved": not_solved,
        "inconclusive": inconclusive,
        "pending_verification": pending,
        "success_rate_verified": round(verified / n, 4) if n else None,
        "total_system_tokens": total_tokens,
        "primary_total_system_tokens_per_verified_solved_task": primary,
        "primary_blocked_reason": primary_reason,
        "total_monetary_usd": total_usd,
        "usd_per_verified_solved_task": usd_per_verified,
        "wall_seconds_median": round(statistics.median(wall_known), 3) if wall_known and len(wall_known) == n else None,
        "wall_seconds_rows_missing": n - len(wall_known),
        "partition_tokens": partition_totals,
    }


def compare(base: dict[str, Any], treat: dict[str, Any]) -> dict[str, Any]:
    b = base["primary_total_system_tokens_per_verified_solved_task"]
    t = treat["primary_total_system_tokens_per_verified_solved_task"]
    if b is None or t is None:
        reason = base["primary_blocked_reason"] or treat["primary_blocked_reason"]
        return {"treatment_over_baseline_primary_ratio": None, "reason": reason}
    return {"treatment_over_baseline_primary_ratio": round(t / b, 4), "reason": None}


def to_markdown(summary: dict[str, Any]) -> str:
    rows = [
        ("runs", "runs"), ("verified_solved", "verified solved"), ("success_rate_verified", "verified success rate"),
        ("inconclusive", "inconclusive"), ("pending_verification", "pending verification"),
        ("total_system_tokens", "total system tokens (incl. failures)"),
        ("primary_total_system_tokens_per_verified_solved_task", "TOTAL_SYSTEM_TOKENS / VERIFIED_SOLVED_TASK"),
        ("total_monetary_usd", "total USD (incl. failures)"),
        ("usd_per_verified_solved_task", "USD / VERIFIED_SOLVED_TASK"),
        ("wall_seconds_median", "wall seconds (median)"),
    ]
    b, t = summary["arms"]["baseline"], summary["arms"]["treatment"]
    lines = [
        f"## Accounting summary: {summary['claim_id']}",
        "",
        f"- **Status:** `{summary['status']}`  |  **Decision:** `{summary['decision']}`",
        f"- **Treatment / baseline primary ratio:** {summary['comparison']['treatment_over_baseline_primary_ratio']}",
        "",
        "| Metric | baseline | treatment |",
        "|---|---:|---:|",
    ]
    for key, label in rows:
        lines.append(f"| {label} | {b[key]} | {t[key]} |")
    blocked = [f"{a}: {summary['arms'][a]['primary_blocked_reason']}" for a in ARMS if summary["arms"][a]["primary_blocked_reason"]]
    if blocked:
        lines += ["", "Primary metric blocked: " + "; ".join(blocked)]
    lines += ["", "Descriptive only. No composite score, no promotion, no auto-merge."]
    return "\n".join(lines) + "\n"


def build_summary(payload: dict[str, Any], partitions: list[str]) -> dict[str, Any]:
    arms = {a: summarize_arm([r for r in payload["rows"] if r["arm"] == a], partitions) for a in ARMS}
    for a in ARMS:
        if arms[a]["runs"] == 0:
            raise SystemExit(f"arm {a} has no rows")
    return {
        "schema_id": "dv-claim-experiment-summary-v1",
        "claim_id": payload.get("claim_id"),
        "status": "DESCRIPTIVE_NO_PROMOTION",
        "decision": "NOT_DECIDED",
        "no_composite_score": True,
        "arms": arms,
        "comparison": compare(arms["baseline"], arms["treatment"]),
    }


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("rows", type=Path, help="dv-claim-arm-rows-v1 JSON")
    ap.add_argument("--out-dir", required=True, type=Path)
    opts = ap.parse_args(argv)

    partitions = load(root / ACCOUNTING_REL)["partitions"]
    payload = load(opts.rows)
    errors = validate_rows(payload, partitions)
    if errors:
        print("INVALID_ROWS")
        for e in errors:
            print(f"- {e}")
        return 2
    summary = build_summary(payload, partitions)

    opts.out_dir.mkdir(parents=True, exist_ok=True)
    targets = {opts.out_dir / "summary.json": json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
               opts.out_dir / "summary.md": to_markdown(summary)}
    for path in targets:
        if path.exists():
            raise SystemExit(f"refusing overwrite: {path}")
    for path, text in targets.items():
        path.write_text(text, encoding="utf-8", newline="\n")
    print(f"SUMMARY_WRITTEN {opts.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
