import unittest

import dv_ledgers as L

C = L.load_contracts()
SHA = "a" * 64


def evidence(**kw):
    rec = {"record_id": "E-1", "claim_id": "C-E1", "evidence_class": "NDV_EXPERIMENTAL_EVIDENCE",
           "evidence_level": "E2", "claim_state": "SUPPORTED_IN_SCOPE", "source_ref": "pilot-runs/x", "scope": "luna, synthetic"}
    rec.update(kw)
    return rec


def invocation(**kw):
    rec = {"invocation_id": "I-1", "treatment": "D0", "role": "executor", "task_id": "T1", "returncode": 0,
           "outcome": "COMPLETED", "counted_in_treatment": True,
           "usage": {"input_tokens": 10, "cached_input_tokens": 4, "cache_write_input_tokens": 0, "output_tokens": 3, "reasoning_output_tokens": 1},
           "wall_seconds": 2.5}
    rec.update(kw)
    return rec


def verification(**kw):
    rec = {"task_id": "T1", "executor_id": "CODEX-PLUS-GPT-5.6-LUNA", "verifier_id": "frozen-verifier-v1", "verifier_sha256": SHA,
           "verdict": "PASS", "task_satisfaction": True, "preservation_pass": True, "harness_valid": True, "executor_self_report_success": True}
    rec.update(kw)
    return rec


def profile(**kw):
    rec = {"capability_id": "LUNA_DIRECT", "can_change": ["retries"], "change_granularity": "PER_TASK", "fixed": ["model", "verifier"]}
    rec.update(kw)
    return rec


class EvidenceRecordTests(unittest.TestCase):
    def test_valid_supported_claim_passes(self):
        self.assertEqual(L.validate("evidence_record", evidence(), C), [])

    def test_literature_capped_at_e0(self):
        errs = L.validate("evidence_record", evidence(evidence_class="LITERATURE_EVIDENCE", evidence_level="E2", claim_state="UNTESTED"), C)
        self.assertTrue(any("capped at E0" in e for e in errs))

    def test_support_requires_experimental_e2_or_higher(self):
        errs = L.validate("evidence_record", evidence(evidence_level="E1"), C)
        self.assertTrue(any("E2 or higher" in e for e in errs))
        errs = L.validate("evidence_record", evidence(evidence_class="ENGINEERING_OBSERVATION", evidence_level="E0"), C)
        self.assertTrue(any("SUPPORTED_IN_SCOPE requires NDV_EXPERIMENTAL" in e for e in errs))

    def test_support_must_be_scoped(self):
        self.assertTrue(L.validate("evidence_record", evidence(scope=""), C))

    def test_vocabulary_enforced(self):
        self.assertTrue(L.validate("evidence_record", evidence(claim_state="PROVEN"), C))


class InvocationRecordTests(unittest.TestCase):
    def test_valid_passes(self):
        self.assertEqual(L.validate("invocation_record", invocation(), C), [])

    def test_failed_invocation_must_still_be_counted(self):
        errs = L.validate("invocation_record", invocation(counted_in_treatment=False, outcome="FAILED", returncode=1), C)
        self.assertTrue(any("counted in its treatment" in e for e in errs))

    def test_completed_requires_zero_returncode(self):
        self.assertTrue(L.validate("invocation_record", invocation(returncode=1), C))

    def test_negative_or_bool_tokens_rejected(self):
        bad = invocation(); bad["usage"]["output_tokens"] = -1
        self.assertTrue(L.validate("invocation_record", bad, C))
        bad = invocation(); bad["usage"]["input_tokens"] = True
        self.assertTrue(L.validate("invocation_record", bad, C))

    def test_missing_usage_component_rejected(self):
        bad = invocation(); del bad["usage"]["reasoning_output_tokens"]
        self.assertTrue(L.validate("invocation_record", bad, C))


class VerificationResultTests(unittest.TestCase):
    def test_pass_is_verified_solved(self):
        self.assertTrue(L.is_verified_solved(verification(), C))

    def test_executor_self_report_never_sufficient(self):
        vr = verification(verdict="FAIL", task_satisfaction=False, executor_self_report_success=True)
        self.assertFalse(L.is_verified_solved(vr, C))

    def test_pass_needs_all_three_conditions(self):
        self.assertTrue(L.validate("verification_result", verification(preservation_pass=False), C))

    def test_verifier_must_be_independent(self):
        errs = L.validate("verification_result", verification(verifier_id="CODEX-PLUS-GPT-5.6-LUNA"), C)
        self.assertTrue(any("independent" in e for e in errs))

    def test_sha_format(self):
        self.assertTrue(L.validate("verification_result", verification(verifier_sha256="XYZ"), C))

    def test_invalid_record_is_never_verified(self):
        self.assertFalse(L.is_verified_solved(verification(verifier_id="CODEX-PLUS-GPT-5.6-LUNA"), C))


class CapabilityProfileTests(unittest.TestCase):
    def test_valid_passes(self):
        self.assertEqual(L.validate("capability_profile", profile(), C), [])

    def test_dimension_cannot_be_both_changeable_and_fixed(self):
        self.assertTrue(L.validate("capability_profile", profile(fixed=["retries"]), C))

    def test_unknown_dimension_rejected(self):
        self.assertTrue(L.validate("capability_profile", profile(can_change=["vibes"]), C))


class TreatmentSummaryTests(unittest.TestCase):
    def test_summary_sums_components_including_failures(self):
        failed = invocation(invocation_id="I-2", outcome="TIMED_OUT", returncode=124)
        s = L.summarize_treatment("D0", [invocation(), failed], C)
        self.assertEqual(s["invocations"], 2)
        self.assertEqual(s["failed_invocations"], 1)
        self.assertEqual(s["usage_components"]["input_tokens"], 20)
        self.assertEqual(s["invocations_by_role"]["executor"], 2)
        self.assertEqual(s["total_tokens_definition"], "UNRESOLVED")

    def test_invalid_invocation_raises_not_dropped(self):
        with self.assertRaises(ValueError):
            L.summarize_treatment("D0", [invocation(counted_in_treatment=False)], C)

    def test_wrong_treatment_raises(self):
        with self.assertRaises(ValueError):
            L.summarize_treatment("D1", [invocation()], C)


if __name__ == "__main__":
    unittest.main()
