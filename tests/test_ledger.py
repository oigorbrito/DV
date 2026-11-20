"""Ledger incremental (I1, D7). Executar: python -m pytest tests/test_ledger.py -v"""

from __future__ import annotations

import pytest

from support import ROOT  # noqa: F401
from dv_harness.ledger import RunLedger, read_events, reconcile


def test_each_append_is_durable_before_close(tmp_path):
    path = tmp_path / "run.jsonl"
    ledger = RunLedger(path)
    ledger.append("call_started", {"call_index": 1})
    # Ainda aberto: a linha já deve estar no disco (flush + fsync por evento).
    assert path.read_text(encoding="utf-8").count("\n") == 1
    ledger.append("call_finished", {"call_index": 1, "record": {"total_tokens": 7}})
    assert path.read_text(encoding="utf-8").count("\n") == 2
    ledger.close()


def test_reconcile_reports_in_flight_call(tmp_path):
    path = tmp_path / "run.jsonl"
    with RunLedger(path) as ledger:
        ledger.append("call_started", {"call_index": 1})
        ledger.append("call_finished", {"call_index": 1, "record": {"total_tokens": 7}})
        ledger.append("call_started", {"call_index": 2})  # interrompido aqui
    summary = reconcile(path)
    assert summary["calls_started"] == 2
    assert summary["calls_finished"] == 1
    assert summary["in_flight_call_indices"] == [2]
    assert summary["finished_records"][1] == {"total_tokens": 7}


def test_reconcile_no_in_flight_when_complete(tmp_path):
    path = tmp_path / "run.jsonl"
    with RunLedger(path) as ledger:
        ledger.append("call_started", {"call_index": 1})
        ledger.append("call_finished", {"call_index": 1, "record": {}})
    assert reconcile(path)["in_flight_call_indices"] == []


def test_invalid_line_is_reported_with_location(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text('{"event": "ok"}\n{not json\n', encoding="utf-8")
    with pytest.raises(ValueError, match="bad.jsonl:2"):
        reconcile(path)


def test_events_carry_schema_and_sorted_keys(tmp_path):
    path = tmp_path / "run.jsonl"
    with RunLedger(path) as ledger:
        ledger.append("run_started", {"b": 1, "a": 2})
    line = path.read_text(encoding="utf-8").strip()
    assert line.index('"a"') < line.index('"b"')
    assert '"schema": "dv-run-ledger-v2"' in line


def test_every_append_is_fsynced(tmp_path, monkeypatch):
    # Garantia de durabilidade (I1/D7): cada evento passa por fsync antes de a execução prosseguir.
    import os as _os

    synced: list[int] = []
    real_fsync = _os.fsync

    def spy(fd):
        synced.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr("dv_harness.ledger.os.fsync", spy)
    with RunLedger(tmp_path / "run.jsonl") as ledger:
        ledger.append("call_started", {"call_index": 1})
        ledger.append("call_finished", {"call_index": 1, "record": {}})
    assert len(synced) == 2


def test_close_is_idempotent_and_blank_lines_are_skipped(tmp_path):
    path = tmp_path / "run.jsonl"
    ledger = RunLedger(path)
    ledger.append("call_started", {"call_index": 1})
    ledger.close()
    ledger.close()  # não levanta
    path.write_text(path.read_text(encoding="utf-8") + "\n\n", encoding="utf-8")
    events = list(read_events(path))
    assert len(events) == 1 and events[0]["event"] == "call_started"
