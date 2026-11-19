#!/usr/bin/env python3
"""DV claim experiment gate.

Validates a requested paired experiment (baseline vs. treatment for one claim)
before any execution is allowed. It never executes an arm and never changes
claim status.

REUSE map:
- claim status            <- claims/claims-v1.json (must be UNTESTED)
- treatment identity      <- experiments/p1/wp07-treatment-bindings-v1.json (must be BOUND)
- frozen design           <- experiments/p1/claim-experiment-registry-v1.json
- oracle/corpus pinning   <- sha256 pins declared in the registry
- fail-closed credentials <- same explicit-token pattern as tools/ndv_run_wp07_codex_single_hop.py

Modes:
- default (dry run): validate and plan; exit 0 for any valid request.
- --execute: exit 1 when a local GAP blocks the plan, exit 3 (UNKNOWN_EXTERNAL)
  when the credential env var is absent. Never prints the credential value.
- --verify-manifest PLAN: re-hash the oracle files recorded in a plan; exit 1 on drift.

Status vocabulary: PASS / GAP / UNKNOWN_EXTERNAL / NOT_APPLICABLE (per check).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CLAIMS_REL = "claims/claims-v1.json"
REGISTRY_REL = "experiments/p1/claim-experiment-registry-v1.json"
BINDINGS_REL = "experiments/p1/wp07-treatment-bindings-v1.json"
HARD_CEILING_USD = 10.00
DEFAULT_CREDENTIAL_ENV = "DV_EXECUTOR_API_KEY"

PASS, GAP, UNKNOWN, NA = "PASS", "GAP", "UNKNOWN_EXTERNAL", "NOT_APPLICABLE"
EXIT_OK, EXIT_BLOCKED, EXIT_INVALID, EXIT_UNKNOWN = 0, 1, 2, 3

CLAIM_RE = re.compile(r"^C-[A-Z][0-9]+$")
TREATMENT_RE = re.compile(r"^B[0-9]+$")
SLICE_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def check_pin(root: Path, entry: dict[str, Any]) -> tuple[str, str]:
    """Return (status, detail) for a {path, sha256} pin relative to root."""
    rel = entry.get("path", "")
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return GAP, f"pin escapes repository root: {rel}"
    if not target.is_file():
        return GAP, f"pinned file missing: {rel}"
    actual = sha256_file(target)
    if actual != entry.get("sha256"):
        return GAP, f"sha256 drift for {rel}: pinned {entry.get('sha256')}, actual {actual}"
    return PASS, f"sha256 matches for {rel}"


def validate_inputs(opts: argparse.Namespace) -> list[str]:
    errors = []
    if not CLAIM_RE.match(opts.claim_id or ""):
        errors.append("claim_id must match C-<LETTER><N>, e.g. C-R1")
    for label, value in (("baseline_setting", opts.baseline), ("treatment_setting", opts.treatment)):
        if not TREATMENT_RE.match(value or ""):
            errors.append(f"{label} must match B<N>, e.g. B0")
    if opts.baseline == opts.treatment:
        errors.append("baseline_setting and treatment_setting must differ")
    if not SLICE_RE.match(opts.corpus_slice or ""):
        errors.append("corpus_slice must be a frozen slice id (letters, digits, . _ -)")
    try:
        budget = float(opts.max_budget_usd)
        if not 0 < budget <= HARD_CEILING_USD:
            errors.append(f"max_budget_usd must be in (0, {HARD_CEILING_USD:.2f}]")
    except (TypeError, ValueError):
        errors.append("max_budget_usd must be a number")
    return errors


def evaluate(opts: argparse.Namespace, root: Path, env: dict[str, str]) -> dict[str, Any]:
    mode = "EXECUTE_REQUESTED" if opts.execute else "DRY_RUN"
    plan: dict[str, Any] = {
        "schema_id": "dv-claim-experiment-plan-v1",
        "generated_utc": utc_now(),
        "mode": mode,
        "request": {
            "claim_id": opts.claim_id,
            "baseline_setting": opts.baseline,
            "treatment_setting": opts.treatment,
            "corpus_slice": opts.corpus_slice,
            "max_budget_usd": opts.max_budget_usd,
            "hard_ceiling_usd": HARD_CEILING_USD,
        },
        "executed": False,
        "checks": [],
        "oracle_manifest": [],
        "planned_runs": None,
        "estimated_usd_total": None,
        "status": None,
    }
    checks = plan["checks"]

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"check": name, "status": status, "detail": detail})

    errors = validate_inputs(opts)
    if errors:
        plan["status"] = "INVALID_INPUT"
        add("input-validation", GAP, "; ".join(errors))
        return plan
    add("input-validation", PASS, "request well-formed")

    claims = load(root / CLAIMS_REL)["claims"]
    claim = next((c for c in claims if c["claim_id"] == opts.claim_id), None)
    if claim is None:
        add("claim-exists", GAP, f"{opts.claim_id} not found in {CLAIMS_REL}")
    elif claim["status"] != "UNTESTED":
        add("claim-exists", GAP, f"{opts.claim_id} status is {claim['status']}; tested or promoted claims need a new revision")
    else:
        add("claim-exists", PASS, f"{opts.claim_id} is UNTESTED at {claim['evidence_level']}")

    registry_path = root / REGISTRY_REL
    registry = load(registry_path) if registry_path.exists() else {"designs": {}}
    design = registry.get("designs", {}).get(opts.claim_id)
    if design is None:
        add("frozen-design", GAP, f"no frozen design registered for {opts.claim_id} in {REGISTRY_REL}")
    else:
        add("frozen-design", PASS, f"design found for {opts.claim_id}")

        slice_entry = design.get("corpus_slices", {}).get(opts.corpus_slice)
        if slice_entry is None:
            add("corpus-slice", GAP, f"slice {opts.corpus_slice} not frozen for {opts.claim_id}")
        else:
            status, detail = check_pin(root, slice_entry)
            add("corpus-slice", status, detail)

        for entry in design.get("verifier_files", []):
            status, detail = check_pin(root, entry)
            add("oracle-pin", status, detail)
            if status == PASS:
                plan["oracle_manifest"].append({"path": entry["path"], "sha256": entry["sha256"]})

        bindings_path = root / BINDINGS_REL
        treatments = load(bindings_path).get("treatments", {}) if bindings_path.exists() else {}
        for arm in (opts.baseline, opts.treatment):
            tdef = treatments.get(arm)
            if tdef is None:
                add(f"binding-{arm}", GAP, f"{arm} not defined in {BINDINGS_REL}")
            elif tdef.get("status") != "BOUND" or not tdef.get("bindings"):
                add(f"binding-{arm}", GAP, f"{arm} is {tdef.get('status')}; blocked cell is not a run")
            else:
                add(f"binding-{arm}", PASS, f"{arm} BOUND")

        runs = design.get("runs_per_arm")
        est = design.get("estimated_usd_per_run", {})
        if not isinstance(runs, int) or runs < 1:
            add("budget", GAP, "runs_per_arm missing or invalid in frozen design")
        elif not all(isinstance(est.get(a), (int, float)) and est[a] > 0 for a in (opts.baseline, opts.treatment)):
            add("budget", GAP, "estimated_usd_per_run missing for a requested arm; run blocked")
        else:
            total = runs * (est[opts.baseline] + est[opts.treatment])
            plan["planned_runs"] = 2 * runs
            plan["estimated_usd_total"] = round(total, 4)
            if total > float(opts.max_budget_usd):
                add("budget", GAP, f"estimated {total:.4f} USD exceeds cap {float(opts.max_budget_usd):.2f} USD")
            else:
                add("budget", PASS, f"estimated {total:.4f} USD within cap {float(opts.max_budget_usd):.2f} USD")

    if opts.execute:
        if env.get(opts.credential_env):
            add("credential", PASS, f"{opts.credential_env} present (value not recorded)")
        else:
            add("credential", UNKNOWN, f"{opts.credential_env} is not set; real execution refused")
    else:
        add("credential", NA, "dry run: no credential needed")

    gaps = [c for c in checks if c["status"] == GAP]
    unknown = [c for c in checks if c["status"] == UNKNOWN]
    if unknown:
        # Missing credential is reported as UNKNOWN_EXTERNAL even when local GAPs also exist.
        plan["status"] = UNKNOWN
    elif gaps:
        plan["status"] = "PLAN_BLOCKED_DRY_RUN" if not opts.execute else "BLOCKED_GAP"
    else:
        plan["status"] = "PLAN_VALID_DRY_RUN" if not opts.execute else "READY_FOR_EXECUTION"
    return plan


def exit_code(plan: dict[str, Any], execute: bool) -> int:
    if plan["status"] == "INVALID_INPUT":
        return EXIT_INVALID
    if not execute:
        return EXIT_OK
    if plan["status"] == UNKNOWN:
        return EXIT_UNKNOWN
    if plan["status"] == "BLOCKED_GAP":
        return EXIT_BLOCKED
    return EXIT_OK


def to_markdown(plan: dict[str, Any]) -> str:
    req = plan["request"]
    lines = [
        f"## Claim experiment plan: {req['claim_id']}",
        "",
        f"- **Status:** `{plan['status']}`  (mode: `{plan['mode']}`, executed: `{plan['executed']}`)",
        f"- **Arms:** baseline `{req['baseline_setting']}` vs treatment `{req['treatment_setting']}`",
        f"- **Corpus slice:** `{req['corpus_slice']}`",
        f"- **Budget:** cap {req['max_budget_usd']} USD (hard ceiling {req['hard_ceiling_usd']:.2f} USD)",
        f"- **Planned runs:** {plan['planned_runs'] if plan['planned_runs'] is not None else 'n/a'}",
        f"- **Estimated spend:** {plan['estimated_usd_total'] if plan['estimated_usd_total'] is not None else 'n/a'} USD",
        "",
        "| Check | Status | Detail |",
        "|---|---|---|",
    ]
    for c in plan["checks"]:
        lines.append(f"| {c['check']} | {c['status']} | {c['detail']} |")
    lines += ["", "No arm was executed. Verified success is not claimed by this plan."]
    return "\n".join(lines) + "\n"


def verify_manifest(plan_path: Path, root: Path) -> int:
    plan = load(plan_path)
    drift = []
    for entry in plan.get("oracle_manifest", []):
        status, detail = check_pin(root, entry)
        print(f"{status} {detail}")
        if status != PASS:
            drift.append(entry["path"])
    if drift:
        print(f"ORACLE_DRIFT {len(drift)} file(s): {', '.join(drift)}")
        return EXIT_BLOCKED
    print("ORACLE_ISOLATION=PASS")
    return EXIT_OK


def main(argv: list[str] | None = None, root: Path = ROOT, env: dict[str, str] | None = None) -> int:
    env = dict(os.environ) if env is None else env
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--claim-id")
    ap.add_argument("--baseline")
    ap.add_argument("--treatment")
    ap.add_argument("--corpus-slice")
    ap.add_argument("--max-budget-usd", default="2.00")
    ap.add_argument("--credential-env", default=DEFAULT_CREDENTIAL_ENV)
    ap.add_argument("--execute", action="store_true", help="request real execution (default is dry run)")
    ap.add_argument("--out", type=Path, help="plan JSON path (refuses overwrite)")
    ap.add_argument("--summary-md", type=Path, help="Markdown summary path (refuses overwrite)")
    ap.add_argument("--verify-manifest", type=Path, help="re-hash oracle files recorded in a plan")
    opts = ap.parse_args(argv)

    if opts.verify_manifest:
        return verify_manifest(opts.verify_manifest, root)

    plan = evaluate(opts, root, env)
    print(json.dumps({"status": plan["status"], "mode": plan["mode"], "executed": plan["executed"]}, sort_keys=True))
    for c in plan["checks"]:
        print(f"{c['status']:18} {c['check']:18} {c['detail']}")

    for path, payload in ((opts.out, json.dumps(plan, indent=2, ensure_ascii=False) + "\n"),
                          (opts.summary_md, to_markdown(plan))):
        if path is None:
            continue
        if path.exists():
            raise SystemExit(f"refusing overwrite: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8", newline="\n")

    github_output = env.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a", encoding="utf-8") as fh:
            fh.write(f"status={plan['status']}\n")

    return exit_code(plan, opts.execute)


if __name__ == "__main__":
    sys.exit(main())
