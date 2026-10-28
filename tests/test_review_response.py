"""Transport projections must preserve complete bound evidence and page boundaries."""
from __future__ import annotations

import copy
import json
import unittest

from tests.helpers import transaction
from gxp_editor.review_response import evidence_summary, review_page, review_response


class ReviewResponseTests(unittest.TestCase):
    def setUp(self):
        self.review = transaction()["business_review"]

    def test_summary_keeps_identity_and_prioritizes_failed_issues_without_mutating_report(self):
        self.review["changes"] = [{"id": str(i), "status": "unverified", "impact": "unknown"} for i in range(8)]
        self.review["changes"].append({"id": "failed", "status": "failed", "impact": "reason" * 200})
        original = copy.deepcopy(self.review)
        summary = review_response(self.review, "plan-1")
        self.assertNotIn("review_report", summary)
        self.assertEqual(summary["report_sha256"], self.review["review_sha256"])
        self.assertEqual(summary["review_issues"][0]["id"], "failed")
        self.assertEqual(len(summary["review_issues"]), 5)
        self.assertEqual(summary["additional_issue_count"], 4)
        self.assertEqual(len(summary["review_issues"][0]["reason"]), 400)
        self.assertEqual(self.review, original)
        self.assertEqual(review_response(self.review, "plan-1", "full")["review_report"], original)

    def test_sections_paginate_complete_records_and_filter_exact_ids(self):
        rows = [{"id": str(i), "before": "value" * i, "after": {"items": [i]}} for i in range(23)]
        self.review["changes"] = rows
        first = review_page(self.review, "changes", limit=10)
        self.assertEqual(first["items"], rows[:10])
        self.assertEqual(first["next_offset"], 10)
        self.assertEqual(review_page(self.review, "changes", offset=20)["items"], rows[20:])
        self.assertIsNone(review_page(self.review, "changes", offset=20)["next_offset"])
        self.assertEqual(review_page(self.review, "changes", item_id="7")["items"], [rows[7]])
        self.assertEqual(review_page(self.review, "changes", item_id="missing")["items"], [])

    def test_page_budget_never_silently_truncates_a_record(self):
        self.review["changes"] = [{"id": "a", "data": "x" * 40000}, {"id": "b", "data": "x" * 40000}]
        page = review_page(self.review, "changes", max_output_bytes=65536)
        self.assertEqual(page["next_offset"], 1)
        self.assertEqual(page["items"], self.review["changes"][:1])
        self.review["changes"][0]["data"] *= 2
        page = review_page(self.review, "changes", max_output_bytes=65536)
        self.assertEqual(page["items"], [])
        self.assertEqual(page["oversized_item"]["id"], "a")
        self.assertIn("response_detail='full'", page["hint"])

    def test_full_rows_respect_explicit_budget_and_can_continue_by_owned_field_read(self):
        self.review["changes"] = [{"id": "large", "before": "中文" * 3000}]
        original = copy.deepcopy(self.review)
        page = review_page(self.review, "changes", detail="full", max_output_bytes=4096,
                           plan_id="plan", conversation_id="owner")
        self.assertEqual([], page["items"])
        self.assertFalse(page["complete"])
        self.assertIsNone(page["next_offset"])
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 4096)
        descriptor = page["next_read"]
        self.assertEqual("read_plan_review", descriptor["tool"])
        summary = review_page(self.review, **descriptor["arguments"])
        pointer = summary["items"][0]["fields"][0]["field_path"]
        first = review_page(self.review, "changes", item_id="large", field_path=pointer,
            max_output_bytes=4096, plan_id="plan", conversation_id="owner")
        descriptor = first["evidence_gaps"][0]["next_read"]
        self.assertEqual("owner", descriptor["arguments"]["conversation_id"])
        second = review_page(self.review, **descriptor["arguments"])
        self.assertEqual(first["value_sha256"], second["value_sha256"])
        self.assertEqual(first["next_offset"], second["offset"])
        self.assertLessEqual(len(json.dumps(second, ensure_ascii=False).encode()), 4096)
        self.assertEqual(original, self.review)

    def test_transport_metadata_counts_large_lists_but_full_evidence_stays_intact(self):
        evidence = {"valid": True, "checks": [{"huge": "x" * 1000}] * 1000, "warning": "x" * 2000}
        summary = evidence_summary(evidence)
        self.assertTrue(summary["valid"])
        self.assertEqual(summary["checks_count"], 1000)
        self.assertEqual(len(summary["warning"]), 1000)
        self.assertEqual(len(evidence["checks"]), 1000)
        self.assertLess(len(json.dumps(summary)), 1200)

    def test_invalid_requests_fail_without_a_partial_projection(self):
        for arguments in ({"limit": True}, {"offset": -1}, {"limit": 21}, {"section": "unknown"}):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                review_page(self.review, **arguments)
        with self.assertRaises(ValueError):
            review_response(self.review, "plan", "invalid")


if __name__ == "__main__":
    unittest.main()
