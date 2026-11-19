import json
import tempfile
import unittest
from pathlib import Path

import dv_telemetry as T


def invocation(inv_id="I-1", treatment="D0", outcome="COMPLETED", returncode=0, **kw):
    rec = {
        "invocation_id": inv_id, "treatment": treatment, "role": "executor", "task_id": "T1",
        "returncode": returncode, "outcome": outcome, "counted_in_treatment": True,
        "usage": {"input_tokens": 10, "cached_input_tokens": 4, "cache_write_input_tokens": 0,
                  "output_tokens": 3, "reasoning_output_tokens": 1},
        "wall_seconds": 2.0,
    }
    rec.update(kw)
    return rec


class TelemetryStoreTests(unittest.TestCase):
    def test_write_then_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            path = T.write_invocation(run, invocation("I-1"))
            self.assertTrue(path.is_file())
            recs = T.read_run(run)
            self.assertEqual([r["invocation_id"] for r in recs], ["I-1"])

    def test_overwrite_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            T.write_invocation(Path(tmp), invocation("I-1"))
            with self.assertRaises(FileExistsError):
                T.write_invocation(Path(tmp), invocation("I-1"))

    def test_invalid_record_is_refused_and_nothing_is_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = invocation("I-2", counted_in_treatment=False)
            with self.assertRaises(ValueError):
                T.write_invocation(Path(tmp), bad)
            self.assertFalse((Path(tmp) / T.SUBDIR).exists())

    def test_unsafe_invocation_id_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for bad_id in ["../escape", "a/b", ".hidden", "x" * 200]:
                with self.assertRaises(ValueError):
                    T.write_invocation(Path(tmp), invocation(bad_id))

    def test_corrupted_file_on_disk_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            T.write_invocation(run, invocation("I-1"))
            (run / T.SUBDIR / "I-1.json").write_text(json.dumps({"invocation_id": "I-1"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                T.read_run(run)

    def test_empty_run_is_unmeasured_not_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = T.summarize_run(Path(tmp), "D0")
            self.assertEqual(s["status"], "UNMEASURED")
            self.assertNotIn("usage_components", s)

    def test_summary_counts_failed_invocations(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            T.write_invocation(run, invocation("I-1"))
            T.write_invocation(run, invocation("I-2", outcome="TIMED_OUT", returncode=124))
            s = T.summarize_run(run, "D0")
            self.assertEqual(s["status"], "MEASURED")
            self.assertEqual(s["invocations"], 2)
            self.assertEqual(s["failed_invocations"], 1)
            self.assertEqual(s["usage_components"]["input_tokens"], 20)

    def test_other_treatments_are_not_mixed_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            T.write_invocation(run, invocation("I-1", treatment="D0"))
            T.write_invocation(run, invocation("I-2", treatment="D1"))
            s = T.summarize_run(run, "D0")
            self.assertEqual(s["invocations"], 1)


if __name__ == "__main__":
    unittest.main()
