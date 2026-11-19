import contextlib
import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dv_claim_experiment_gate as gate  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
SECRET = "SECRET-VALUE-DO-NOT-PRINT"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2), encoding="utf-8")


class GateFixture(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="dv-gate-"))
        self.slice_bytes = b'{"tasks": ["t1", "t2"]}'
        self.oracle_bytes = b"def oracle(): return True\n"
        (self.root / "experiments/p1/slices").mkdir(parents=True)
        (self.root / "experiments/p1/slices/s1.json").write_bytes(self.slice_bytes)
        (self.root / "tools").mkdir()
        (self.root / "tools/oracle.py").write_bytes(self.oracle_bytes)
        self.write_claims(status="UNTESTED")
        self.write_bindings(b1_status="BOUND")
        self.write_registry(with_design=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def write_claims(self, status="UNTESTED", claim="C-R1"):
        write_json(self.root / gate.CLAIMS_REL, {"claims": [
            {"claim_id": claim, "status": status, "evidence_level": "E0"},
            {"claim_id": "C-X9", "status": "TESTED", "evidence_level": "E2"},
        ]})

    def write_bindings(self, b1_status="BOUND"):
        ok = {"status": "BOUND", "bindings": {"executor": "frozen-binding-b0"}}
        write_json(self.root / gate.BINDINGS_REL, {"treatments": {
            "B0": ok,
            "B1": ({"status": "BOUND", "bindings": {"executor": "frozen-binding-b1"}}
                   if b1_status == "BOUND" else {"status": "UNBOUND", "bindings": {}}),
        }})

    def write_registry(self, with_design=True, est_b0=0.5, est_b1=0.1, runs=2, slice_sha=None):
        designs = {}
        if with_design:
            designs["C-R1"] = {
                "corpus_slices": {"s1": {"path": "experiments/p1/slices/s1.json",
                                         "sha256": slice_sha or sha(self.slice_bytes)}},
                "verifier_files": [{"path": "tools/oracle.py", "sha256": sha(self.oracle_bytes)}],
                "runs_per_arm": runs,
                "estimated_usd_per_run": {"B0": est_b0, "B1": est_b1},
            }
        write_json(self.root / gate.REGISTRY_REL, {"designs": designs})

    def run_gate(self, *extra, env=None, claim="C-R1", baseline="B0", treatment="B1",
                 corpus="s1", budget="2.00"):
        argv = ["--claim-id", claim, "--baseline", baseline, "--treatment", treatment,
                "--corpus-slice", corpus, "--max-budget-usd", budget, *extra]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gate.main(argv, root=self.root, env=env or {})
        return code, out.getvalue()


class InputValidation(GateFixture):
    def test_bad_claim_id_is_invalid(self):
        code, out = self.run_gate(claim="R1")
        self.assertEqual(code, gate.EXIT_INVALID)
        self.assertIn("INVALID_INPUT", out)

    def test_same_arms_is_invalid(self):
        code, _ = self.run_gate(baseline="B1", treatment="B1")
        self.assertEqual(code, gate.EXIT_INVALID)

    def test_budget_above_hard_ceiling_is_invalid(self):
        code, _ = self.run_gate(budget="50")
        self.assertEqual(code, gate.EXIT_INVALID)

    def test_non_numeric_budget_is_invalid(self):
        code, _ = self.run_gate(budget="abc")
        self.assertEqual(code, gate.EXIT_INVALID)


class DryRun(GateFixture):
    def test_valid_request_plans_without_executing(self):
        out_path = self.root / "plan.json"
        code, _ = self.run_gate("--out", str(out_path))
        self.assertEqual(code, gate.EXIT_OK)
        plan = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertEqual(plan["status"], "PLAN_VALID_DRY_RUN")
        self.assertFalse(plan["executed"])
        self.assertEqual(plan["planned_runs"], 4)
        self.assertAlmostEqual(plan["estimated_usd_total"], 1.2)
        self.assertEqual(plan["oracle_manifest"][0]["path"], "tools/oracle.py")

    def test_missing_design_is_blocked_but_dry_run_exits_zero(self):
        self.write_registry(with_design=False)
        code, out = self.run_gate()
        self.assertEqual(code, gate.EXIT_OK)
        self.assertIn("PLAN_BLOCKED_DRY_RUN", out)
        self.assertIn("no frozen design", out)

    def test_repository_state_has_no_frozen_designs(self):
        """Documents current truth: the real registry is empty, so nothing can execute."""
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gate.main(["--claim-id", "C-R1", "--baseline", "B0", "--treatment", "B1",
                              "--corpus-slice", "s1"], root=REPO, env={})
        self.assertEqual(code, gate.EXIT_OK)
        self.assertIn("PLAN_BLOCKED_DRY_RUN", out.getvalue())
        self.assertIn("no frozen design", out.getvalue())


class ExecuteMode(GateFixture):
    def test_unbound_treatment_blocks_execution(self):
        self.write_bindings(b1_status="UNBOUND")
        code, out = self.run_gate("--execute", env={gate.DEFAULT_CREDENTIAL_ENV: SECRET})
        self.assertEqual(code, gate.EXIT_BLOCKED)
        self.assertIn("UNBOUND", out)
        self.assertIn("blocked cell is not a run", out)

    def test_budget_over_cap_blocks_execution(self):
        self.write_registry(est_b0=5.0, est_b1=5.0, runs=2)
        code, out = self.run_gate("--execute", env={gate.DEFAULT_CREDENTIAL_ENV: SECRET}, budget="2.00")
        self.assertEqual(code, gate.EXIT_BLOCKED)
        self.assertIn("exceeds cap", out)

    def test_missing_estimate_blocks_execution(self):
        self.write_registry(est_b1=None)
        code, _ = self.run_gate("--execute", env={gate.DEFAULT_CREDENTIAL_ENV: SECRET})
        self.assertEqual(code, gate.EXIT_BLOCKED)

    def test_missing_credential_is_unknown_external_and_never_prints_secret(self):
        code, out = self.run_gate("--execute", env={})
        self.assertEqual(code, gate.EXIT_UNKNOWN)
        self.assertIn("UNKNOWN_EXTERNAL", out)
        self.assertIn("real execution refused", out)
        self.assertNotIn(SECRET, out)

    def test_missing_credential_wins_over_local_gap(self):
        self.write_bindings(b1_status="UNBOUND")
        code, out = self.run_gate("--execute", env={})
        self.assertEqual(code, gate.EXIT_UNKNOWN)
        self.assertIn("UNKNOWN_EXTERNAL", out.splitlines()[0])

    def test_credential_present_is_ready_but_never_executed(self):
        out_path = self.root / "plan.json"
        code, out = self.run_gate("--execute", "--out", str(out_path),
                                  env={gate.DEFAULT_CREDENTIAL_ENV: SECRET})
        self.assertEqual(code, gate.EXIT_OK)
        self.assertIn("READY_FOR_EXECUTION", out)
        self.assertNotIn(SECRET, out)
        plan = json.loads(out_path.read_text(encoding="utf-8"))
        self.assertFalse(plan["executed"])
        self.assertNotIn(SECRET, out_path.read_text(encoding="utf-8"))


class Integrity(GateFixture):
    def test_claim_already_tested_is_blocked(self):
        self.write_claims(status="TESTED")
        code, out = self.run_gate("--execute", env={gate.DEFAULT_CREDENTIAL_ENV: SECRET})
        self.assertEqual(code, gate.EXIT_BLOCKED)
        self.assertIn("new revision", out)

    def test_corpus_slice_drift_blocks(self):
        (self.root / "experiments/p1/slices/s1.json").write_bytes(b"changed")
        code, out = self.run_gate("--execute", env={gate.DEFAULT_CREDENTIAL_ENV: SECRET})
        self.assertEqual(code, gate.EXIT_BLOCKED)
        self.assertIn("sha256 drift", out)

    def test_unknown_corpus_slice_blocks(self):
        code, out = self.run_gate("--execute", corpus="s9", env={gate.DEFAULT_CREDENTIAL_ENV: SECRET})
        self.assertEqual(code, gate.EXIT_BLOCKED)
        self.assertIn("not frozen", out)

    def test_verify_manifest_detects_oracle_drift(self):
        out_path = self.root / "plan.json"
        self.run_gate("--out", str(out_path))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(gate.main(["--verify-manifest", str(out_path)], root=self.root), 0)
        self.assertIn("ORACLE_ISOLATION=PASS", buf.getvalue())

        (self.root / "tools/oracle.py").write_bytes(b"tampered")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = gate.main(["--verify-manifest", str(out_path)], root=self.root)
        self.assertEqual(code, gate.EXIT_BLOCKED)
        self.assertIn("ORACLE_DRIFT", buf.getvalue())

    def test_refuses_overwrite(self):
        out_path = self.root / "plan.json"
        out_path.write_text("{}", encoding="utf-8")
        with self.assertRaises(SystemExit):
            self.run_gate("--out", str(out_path))

    def test_github_output_receives_status(self):
        gh_out = self.root / "gh_output"
        self.run_gate(env={"GITHUB_OUTPUT": str(gh_out)})
        self.assertIn("status=PLAN_VALID_DRY_RUN", gh_out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
