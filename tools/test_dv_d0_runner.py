import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dv_d0_runner as R

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = json.loads((ROOT / "experiments/luna-only/luna-composition-protocol-v1.json").read_text())
TEMPLATE_BUDGET = json.loads((ROOT / "experiments/luna-only/luna-d0-budget-v1.json").read_text())
FROZEN_BUDGET = dict(TEMPLATE_BUDGET, status="FROZEN", executor_timeout_ms=1800000, verification_timeout_ms=1800000, frozen_at="TEST")


def admission(cid, status="ADMITTED_FROZEN", schema="ndv-p1-s2-admission-record-v2"):
    return {"schema_id": schema, "status": status, "candidate_id": cid}


class D0RunnerTests(unittest.TestCase):
    def test_template_budget_refuses(self):
        with self.assertRaisesRegex(R.Refusal, "not FROZEN"):
            R.plan(PROTOCOL, TEMPLATE_BUDGET, [admission("c1")])

    def test_protocol_with_planner_invocation_refuses(self):
        bad = json.loads(json.dumps(PROTOCOL))
        bad["treatments"]["D0"]["invocations"]["planner"] = 1
        with self.assertRaisesRegex(R.Refusal, "0 planner"):
            R.plan(bad, FROZEN_BUDGET, [])

    def test_protocol_not_frozen_refuses(self):
        bad = dict(PROTOCOL, status="DRAFT")
        with self.assertRaises(R.Refusal):
            R.plan(bad, FROZEN_BUDGET, [])

    def test_no_admitted_tasks_yields_empty_plan(self):
        m = R.plan(PROTOCOL, FROZEN_BUDGET, [])
        self.assertEqual(m["status"], "NO_ADMITTED_TASKS")
        self.assertEqual(m["planned_tasks"], 0)
        self.assertEqual(m["treatment_execution"], "NOT_EXECUTED")

    def test_only_schema_correct_admitted_records_planned(self):
        recs = [admission("ok"), admission("screen", status="SCREENING"), admission("wrong", schema="other-v1")]
        m = R.plan(PROTOCOL, FROZEN_BUDGET, recs)
        self.assertEqual([p["task_id"] for p in m["planned"]], ["ok"])
        self.assertEqual(m["excluded_records"], 2)

    def test_planned_task_is_exactly_one_executor_and_no_retry(self):
        m = R.plan(PROTOCOL, FROZEN_BUDGET, [admission("ok")])
        p = m["planned"][0]
        self.assertEqual([i["role"] for i in p["invocations"]], ["executor"])
        self.assertEqual(p["retry_limit"], 0)
        self.assertEqual(p["repair_limit"], 0)
        self.assertTrue(p["verifier"]["independent_of_executor"])

    def test_never_starts_a_process(self):
        with mock.patch.object(subprocess, "run", side_effect=AssertionError("process started")), \
             mock.patch.object(subprocess, "Popen", side_effect=AssertionError("process started")):
            R.plan(PROTOCOL, FROZEN_BUDGET, [admission("ok")])

    def test_cli_refuses_with_default_template_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            import sys
            argv = ["prog", "--admission-dir", tmp]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(R.main(), 2)

    def test_cli_writes_manifest_only_once(self):
        import sys
        with tempfile.TemporaryDirectory() as tmp:
            budget = Path(tmp) / "b.json"; budget.write_text(json.dumps(FROZEN_BUDGET))
            adm = Path(tmp) / "adm"; adm.mkdir(); (adm / "a.json").write_text(json.dumps(admission("ok")))
            out = Path(tmp) / "m.json"
            argv = ["prog", "--budget", str(budget), "--admission-dir", str(adm), "--out", str(out)]
            with mock.patch.object(sys, "argv", argv):
                self.assertEqual(R.main(), 0)
                self.assertEqual(json.loads(out.read_text())["planned_tasks"], 1)
                self.assertEqual(R.main(), 2)  # second run refuses to overwrite


if __name__ == "__main__":
    unittest.main()
