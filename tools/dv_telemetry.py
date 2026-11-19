#!/usr/bin/env python3
"""Minimal telemetry store: one JSON file per invocation, inside the run directory.

Layout:
    <run_dir>/telemetry/<invocation_id>.json

Each record is validated against the invocation_record contract (tools/dv_ledgers.py) before it is
written. Existing records are never overwritten. A run with no records is reported as UNMEASURED,
never as zero cost.

This module records and reads evidence only. It starts no process and defines no runtime.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import dv_ledgers as L

SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SUBDIR = "telemetry"


def _telemetry_dir(run_dir: Path) -> Path:
    return Path(run_dir) / SUBDIR


def write_invocation(run_dir: Path, record: dict[str, Any], contracts: dict[str, Any] | None = None) -> Path:
    """Validate and store one invocation record. Refuses invalid records, unsafe ids and overwrites."""
    contracts = contracts or L.load_contracts()
    errors = L.validate("invocation_record", record, contracts)
    if errors:
        raise ValueError(f"invalid invocation record: {errors}")
    inv_id = record["invocation_id"]
    if not SAFE_ID.match(inv_id):
        raise ValueError(f"unsafe invocation_id for a file name: {inv_id!r}")
    d = _telemetry_dir(run_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{inv_id}.json"
    if path.exists():
        raise FileExistsError(f"telemetry record already exists; refusing overwrite: {path}")
    path.write_text(json.dumps(record, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def read_run(run_dir: Path, contracts: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Load and validate every invocation record of a run, in a stable order."""
    contracts = contracts or L.load_contracts()
    d = _telemetry_dir(run_dir)
    if not d.is_dir():
        return []
    records = []
    for path in sorted(d.glob("*.json")):
        rec = json.loads(path.read_text(encoding="utf-8"))
        errors = L.validate("invocation_record", rec, contracts)
        if errors:
            raise ValueError(f"{path.name} is invalid: {errors}")
        records.append(rec)
    return records


def summarize_run(run_dir: Path, treatment: str, contracts: dict[str, Any] | None = None) -> dict[str, Any]:
    """Summarize one treatment of a run. No records means UNMEASURED, not zero."""
    records = [r for r in read_run(run_dir, contracts) if r["treatment"] == treatment]
    if not records:
        return {"treatment": treatment, "status": "UNMEASURED", "invocations": 0}
    summary = L.summarize_treatment(treatment, records, contracts)
    summary["status"] = "MEASURED"
    return summary
