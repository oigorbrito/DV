import contextlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dv_claim_experiment_report as rep  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
PARTS = json.loads((REPO / rep.ACCOUNTING_REL).read_text(encoding="utf-8"))["partitions"]


def row(arm, run_id, verified="VERIFIED_SOLVED", tokens=1000, usd=0.01, wall=10.0, parts=None):
    return {"arm": arm, "run_id": run_id, "verified": verified, "total_system_tokens": tokens,
            "usd": usd, "wall_seconds": wall, "partition_tokens": parts or {}}


def payload(rows):
    return {"schema_id": "dv-claim-arm-rows-v1", "claim_id": "C-R1", "rows": rows}


class Summary(unittest.TestCase):
    def test_primary_metric_counts_failures_in_numerator(self):
        rows = [row("baseline", "b1", tokens=1000), row("baseline", "b2", tokens=1000),
                row("baseline", "b3", verified="NOT_SOLVED", tokens=2000)]
        rows += [row("treatment", "t1", tokens=500), row("treatment", "t2", tokens=500)]
        s = rep.build_summary(payload(rows), PARTS)
        b = s["arms"]["baseline"]
        self.assertEqual(b["verified_solved"], 2)
        self.assertEqual(b["total_system_tokens"], 4000)
        self.assertEqual(b["primary_total_system_tokens_per_verified_solved_task"], 2000.0)
        self.assertEqual(s["arms"]["treatment"]["primary_total_system_tokens_per_verified_solved_task"], 500.0)
        self.assertEqual(s["comparison"]["treatment_over_baseline_primary_ratio"], 0.25)

    def test_missing_tokens_blocks_primary_and_is_never_zero(self):
        rows = [row("baseline", "b1"), row("baseline", "b2", tokens=None)]
        rows += [row("treatment", "t1"), row("treatment", "t2")]
        s = rep.build_summary(payload(rows), PARTS)
        b = s["arms"]["baseline"]
        self.assertIsNone(b["total_system_tokens"])
        self.assertIsNone(b["primary_total_system_tokens_per_verified_solved_task"])
        self.assertIn("telemetry missing", b["primary_blocked_reason"])
        self.assertIsNone(s["comparison"]["treatment_over_baseline_primary_ratio"])

    def test_pending_verification_blocks_primary(self):
        rows = [row("baseline", "b1", verified="PENDING_VERIFICATION"), row("baseline", "b2")]
        rows += [row("treatment", "t1"), row("treatment", "t2")]
        s = rep.build_summary(payload(rows), PARTS)
        self.assertIsNone(s["arms"]["baseline"]["primary_total_system_tokens_per_verified_solved_task"])
        self.assertIn("PENDING_VERIFICATION", s["arms"]["baseline"]["primary_blocked_reason"])

    def test_zero_verified_is_undefined_not_zero(self):
        rows = [row("baseline", "b1", verified="NOT_SOLVED"), row("baseline", "b2", verified="INCONCLUSIVE")]
        rows += [row("treatment", "t1"), row("treatment", "t2")]
        s = rep.build_summary(payload(rows), PARTS)
        self.assertIsNone(s["arms"]["baseline"]["primary_total_system_tokens_per_verified_solved_task"])
        self.assertEqual(s["arms"]["baseline"]["inconclusive"], 1)
        self.assertEqual(s["arms"]["baseline"]["total_system_tokens"], 2000)

    def test_partitions_not_reported_are_flagged(self):
        rows = [row("baseline", "b1", parts={"execution": 700}), row("baseline", "b2")]
        rows += [row("treatment", "t1"), row("treatment", "t2")]
        s = rep.build_summary(payload(rows), PARTS)
        ex = s["arms"]["baseline"]["partition_tokens"]["execution"]
        self.assertIsNone(ex["total"])
        self.assertEqual(ex["rows_not_reported"], 1)

    def test_usd_per_verified_and_no_decision(self):
        rows = [row("baseline", "b1", usd=0.5), row("baseline", "b2", usd=0.5)]
        rows += [row("treatment", "t1", usd=0.1), row("treatment", "t2", usd=0.1)]
        s = rep.build_summary(payload(rows), PARTS)
        self.assertEqual(s["arms"]["baseline"]["usd_per_verified_solved_task"], 0.5)
        self.assertEqual(s["decision"], "NOT_DECIDED")
        self.assertTrue(s["no_composite_score"])


class Validation(unittest.TestCase):
    def base_rows(self):
        return [row("baseline", "b1"), row("treatment", "t1")]

    def test_valid_payload_has_no_errors(self):
        self.assertEqual(rep.validate_rows(payload(self.base_rows()), PARTS), [])

    def test_unknown_partition_is_rejected(self):
        rows = [row("baseline", "b1", parts={"made_up": 1}), row("treatment", "t1")]
        self.assertTrue(any("outside the contract" in e for e in rep.validate_rows(payload(rows), PARTS)))

    def test_bad_verified_state_is_rejected(self):
        rows = [row("baseline", "b1", verified="DONE"), row("treatment", "t1")]
        self.assertTrue(any("verified must be" in e for e in rep.validate_rows(payload(rows), PARTS)))

    def test_duplicate_run_id_is_rejected(self):
        rows = [row("baseline", "b1"), row("baseline", "b1"), row("treatment", "t1")]
        self.assertTrue(any("duplicate" in e for e in rep.validate_rows(payload(rows), PARTS)))

    def test_empty_arm_is_rejected_by_build(self):
        with self.assertRaises(SystemExit):
            rep.build_summary(payload([row("baseline", "b1")]), PARTS)


class Cli(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dv-report-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_json_and_markdown_and_refuses_overwrite(self):
        rows_path = self.tmp / "rows.json"
        rows_path.write_text(json.dumps(payload(self.base_rows_ok())), encoding="utf-8")
        out = self.tmp / "out"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rep.main([str(rows_path), "--out-dir", str(out)], root=REPO), 0)
        self.assertTrue((out / "summary.json").exists())
        self.assertIn("Descriptive only", (out / "summary.md").read_text(encoding="utf-8"))
        with self.assertRaises(SystemExit):
            rep.main([str(rows_path), "--out-dir", str(out)], root=REPO)

    def test_invalid_rows_exit_two(self):
        rows_path = self.tmp / "rows.json"
        rows_path.write_text(json.dumps({"schema_id": "wrong", "rows": []}), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(rep.main([str(rows_path), "--out-dir", str(self.tmp / "o")], root=REPO), 2)

    def base_rows_ok(self):
        return [row("baseline", "b1"), row("baseline", "b2"), row("treatment", "t1"), row("treatment", "t2")]


if __name__ == "__main__":
    unittest.main()
