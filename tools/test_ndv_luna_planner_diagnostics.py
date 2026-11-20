"""Tests for the bounded planner failure categories (issue #5).

These tests exercise only the pure classifier over executor output. They never run the planner.
"""
import json
import unittest

import ndv_qualify_luna_composition_planner as pq


def valid_plan(task_id="T1"):
    return {
        "schema_id": "ndv-luna-decomposition-plan-v1",
        "task_id": task_id,
        "objective": "Do task",
        "constraints": ["preserve behavior"],
        "steps": [
            {"id": "S1", "objective": "Inspect", "dependencies": [], "expected_artifact": "notes", "verification": "read file"},
            {"id": "S2", "objective": "Integrate", "dependencies": ["S1"], "expected_artifact": "candidate", "verification": "run check"},
        ],
        "final_verification": "run frozen verifier",
    }


def agent_message(text):
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


class PlannerFailureCategoryTests(unittest.TestCase):
    def test_one_valid_plan_has_no_category(self):
        out = agent_message(json.dumps(valid_plan()))
        self.assertIsNone(pq.classify_plan_failure(out, "T1"))

    def test_malformed_jsonl_is_categorized(self):
        out = agent_message(json.dumps(valid_plan())) + "\n{not json"
        self.assertEqual(pq.classify_plan_failure(out, "T1"), "MALFORMED_JSONL")

    def test_zero_agent_messages_is_no_valid_plan(self):
        out = json.dumps({"type": "thread.started", "thread_id": "t"})
        self.assertEqual(pq.classify_plan_failure(out, "T1"), "NO_VALID_PLAN")

    def test_markdown_fenced_plan_is_no_valid_plan_not_schema_invalid(self):
        # Non-JSON text is not a candidate plan at all, so it is NO_VALID_PLAN.
        text = "```json\n" + json.dumps(valid_plan()) + "\n```"
        self.assertEqual(pq.classify_plan_failure(agent_message(text), "T1"), "NO_VALID_PLAN")

    def test_json_object_failing_schema_is_plan_schema_invalid(self):
        bad = valid_plan()
        bad["extra_field"] = 1
        self.assertEqual(pq.classify_plan_failure(agent_message(json.dumps(bad)), "T1"), "PLAN_SCHEMA_INVALID")

    def test_wrong_task_id_is_plan_schema_invalid(self):
        self.assertEqual(pq.classify_plan_failure(agent_message(json.dumps(valid_plan("OTHER"))), "T1"), "PLAN_SCHEMA_INVALID")

    def test_multiple_valid_plans_is_categorized(self):
        msg = agent_message(json.dumps(valid_plan()))
        self.assertEqual(pq.classify_plan_failure(msg + "\n" + msg, "T1"), "MULTIPLE_VALID_PLANS")

    def test_unexpected_input_fails_closed_to_unknown(self):
        self.assertEqual(pq.classify_plan_failure(None, "T1"), "UNKNOWN_PLAN_INVALID")

    def test_categories_are_bounded_vocabulary(self):
        self.assertEqual(
            set(pq.PLAN_FAILURE_CATEGORIES),
            {"MALFORMED_JSONL", "NO_VALID_PLAN", "MULTIPLE_VALID_PLANS", "PLAN_SCHEMA_INVALID", "UNKNOWN_PLAN_INVALID"},
        )

    def test_extract_plan_messages_unchanged(self):
        msg = agent_message(json.dumps(valid_plan()))
        self.assertEqual(pq.extract_plan(msg, "T1")["task_id"], "T1")
        with self.assertRaisesRegex(ValueError, "observed 0"):
            pq.extract_plan("", "T1")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            pq.extract_plan(msg + "\n" + msg, "T1")
        with self.assertRaisesRegex(ValueError, "malformed Codex JSONL at line 1"):
            pq.extract_plan("{bad", "T1")


if __name__ == "__main__":
    unittest.main()
