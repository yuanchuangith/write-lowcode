"""Synthetic snapshot comparisons; no business IO or installed plugin access."""
from __future__ import annotations

import copy
import json
import unittest

from tests.helpers import baseline, relation
from gxp_editor.baseline_comparison import (
    BaselineComparisonError, compare_baseline, draft_diagnostics, require_baseline_match,
)
from gxp_editor.hashing import design_content_hash, relation_snapshot_hash


class BaselineComparisonTests(unittest.TestCase):
    def setUp(self):
        self.baseline = baseline(data='{"标题":"正文🙂"}')
        self.baseline["relations"] = [relation()]
        self.current = copy.deepcopy(self.baseline)
        self.relations = copy.deepcopy(self.baseline["relations"])
        self.artifact = {"design_id": self.baseline["id"], "ref_id": self.baseline["ref_id"],
                         "old_content_hash": self.baseline["content_hash"],
                         "old_relation_hash": relation_snapshot_hash(self.relations)}

    def compare(self, **kwargs):
        return compare_baseline(self.baseline, self.current, self.relations,
                                session_id="synthetic-session", backup_id="synthetic-backup", **kwargs)

    def test_identical_snapshot_matches_without_mutating_inputs(self):
        before = copy.deepcopy((self.baseline, self.current, self.relations))
        result = self.compare()
        self.assertEqual(result["status"], "match")
        self.assertEqual(result["change_categories"], [])
        self.assertEqual(result["evidence_gaps"], [])
        self.assertEqual(result["comparison_schema_version"], 1)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["write_executed"])
        self.assertEqual((self.baseline, self.current, self.relations), before)
        require_baseline_match(result)

    def test_timestamp_only_is_stale_and_keeps_the_existing_cas(self):
        self.current["last_modification_time"] = "2026-10-27 12:00:00.001"
        result = self.compare()
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["change_categories"], ["revision_only_changed"])
        self.assertFalse(result["diagnostics"]["payload_changed"])
        self.assertNotEqual(result["expected"]["content_hash"], result["current"]["content_hash"])
        self.assertEqual(result["minimum_next_step"]["tool"], "inspect_session_cancellation")

    def test_each_code_or_canvas_field_change_is_explained_without_returning_bodies(self):
        for field in ("data", "js_code", "csharp_code"):
            with self.subTest(field=field):
                self.current = copy.deepcopy(self.baseline)
                self.current[field] += "敏感正文不应返回\n"
                result = self.compare()
                self.assertEqual(result["status"], "stale")
                self.assertIn("draft_content_changed", result["change_categories"])
                self.assertEqual(result["diagnostics"]["changed_fields"],
                                 {key: key == field for key in ("data", "js_code", "csharp_code")})
                self.assertNotIn("敏感正文不应返回", json.dumps(result, ensure_ascii=False))
                self.assertEqual(result["current"]["content_hash"], design_content_hash(self.current))

    def test_relation_order_reuses_the_existing_stable_hash(self):
        self.baseline["relations"].append(relation(id="relation-2", action_data_id="action-2"))
        self.relations = list(reversed(copy.deepcopy(self.baseline["relations"])))
        self.assertEqual(self.compare()["status"], "match")

    def test_relation_add_remove_modify_soft_delete_and_audit_changes_are_stale(self):
        variations = [[], self.relations + [relation(id="relation-2")],
                      [{**self.relations[0], "data": "changed"}],
                      [{**self.relations[0], "isDeleted": 1}],
                      [{**self.relations[0], "lastModificationTime": "2026-10-27"}]]
        for changed in variations:
            with self.subTest(relations=changed):
                self.relations = changed
                result = self.compare(include_relation_differences=True)
                self.assertEqual(result["status"], "stale")
                self.assertEqual(result["change_categories"], ["draft_relations_changed"])
                self.assertGreater(result["diagnostics"]["relations"]["changed_count"], 0)
                with self.assertRaises(BaselineComparisonError) as caught:
                    require_baseline_match(result)
                self.assertEqual(caught.exception.code, "draft_relations_changed")

    def test_identity_published_and_deleted_state_changes_are_stale(self):
        for key, value, category in (("id", "other-design", "identity_changed"),
                                     ("ref_id", "other-ref", "identity_changed"),
                                     ("is_publish", 1, "publication_state_changed"),
                                     ("isDeleted", 1, "deletion_state_changed")):
            with self.subTest(key=key):
                self.current = copy.deepcopy(self.baseline)
                self.current[key] = value
                self.relations = [{**self.baseline["relations"][0], "design_id": self.current["id"]}]
                result = self.compare()
                self.assertEqual(result["status"], "stale")
                self.assertIn(category, result["change_categories"])

    def test_incomplete_relations_and_database_read_failure_are_unknown(self):
        result = self.compare(relations_complete=False)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("current_relations_incomplete", result["evidence_gaps"])
        result = self.compare(read_error="database_read_timeout")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("database_read_timeout", result["evidence_gaps"])

    def test_missing_current_draft_does_not_claim_equality_or_guide_automatic_cancellation(self):
        self.current = None
        result = self.compare()
        self.assertEqual(result["status"], "unknown")
        self.assertIn("current_draft_unavailable", result["evidence_gaps"])
        self.assertEqual(result["minimum_next_step"]["tool"], "find_edit_nodes")

    def test_missing_or_malformed_target_state_is_unknown(self):
        for key in ("is_publish", "isDeleted"):
            with self.subTest(key=key):
                self.current = copy.deepcopy(self.baseline)
                del self.current[key]
                self.assertEqual(self.compare()["status"], "unknown")
                self.current[key] = "unreadable"
                self.assertEqual(self.compare()["status"], "unknown")

    def test_current_partial_relation_and_wrong_ownership_are_unknown(self):
        for rows in ([{"id": "relation-1"}], [{**self.relations[0], "design_id": "other"}],
                     self.relations * 2):
            with self.subTest(rows=rows):
                self.relations = rows
                self.assertEqual(self.compare()["status"], "unknown")

    def test_validated_artifact_does_not_require_full_baseline_or_diagnostic_fields(self):
        for corrupt in (None, {"invalid": "baseline"}, {**self.baseline, "data": "damaged"}):
            with self.subTest(baseline=corrupt):
                result = compare_baseline(corrupt, self.current, self.relations, artifact=self.artifact)
                self.assertEqual(result["status"], "match")
                self.assertFalse(result["diagnostics"]["available"])

    def test_artifact_old_hashes_take_precedence_over_a_different_full_baseline(self):
        self.baseline = baseline(data="an unrelated baseline")
        self.baseline["relations"] = []
        self.assertEqual(self.compare(artifact=self.artifact)["status"], "match")

    def test_old_relation_hash_fallback_requires_matching_baseline_content(self):
        del self.artifact["old_relation_hash"]
        self.assertEqual(self.compare(artifact=self.artifact)["status"], "match")
        self.baseline = baseline(data="different baseline")
        self.baseline["relations"] = self.relations
        result = self.compare(artifact=self.artifact)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("baseline_relations_unavailable", result["evidence_gaps"])

    def test_candidate_relation_hash_is_never_treated_as_old_relation_evidence(self):
        del self.artifact["old_relation_hash"]
        self.artifact["expected_relation_hash"] = relation_snapshot_hash(self.relations)
        result = compare_baseline(None, self.current, self.relations, artifact=self.artifact)
        self.assertEqual(result["status"], "unknown")
        self.assertIn("baseline_relations_unavailable", result["evidence_gaps"])

    def test_missing_hash_identity_and_corrupt_baseline_do_not_match(self):
        for key in ("content_hash", "id", "ref_id", "relations"):
            with self.subTest(key=key):
                old = self.baseline.pop(key)
                self.assertEqual(self.compare()["status"], "unknown")
                self.baseline[key] = old
        self.baseline["data"] = "changed without rehash"
        self.assertEqual(self.compare()["status"], "unknown")

    def test_diagnostic_hashes_preserve_missing_null_empty_case_whitespace_and_list_order(self):
        values = [{}, {"data": None}, {"data": ""}, {"data": "A"}, {"data": "a"},
                  {"data": "a "}, {"data": "a\n"}, {"data": [1, 2]}, {"data": [2, 1]}]
        summaries = [draft_diagnostics(value)["payload_sha256"] for value in values]
        self.assertEqual(len(set(summaries)), len(values))
        self.assertFalse(draft_diagnostics({})["fields"]["data"]["present"])
        self.assertTrue(draft_diagnostics({"data": None})["fields"]["data"]["present"])

    def test_unknown_errors_and_stale_errors_have_structured_details(self):
        self.current["js_code"] += " changed"
        result = self.compare()
        with self.assertRaises(BaselineComparisonError) as caught:
            require_baseline_match(result)
        self.assertEqual(caught.exception.code, "draft_baseline_changed")
        self.assertIs(caught.exception.details, result)
        self.assertEqual(caught.exception.details["session_id"], "synthetic-session")
        self.assertEqual(caught.exception.details["backup_id"], "synthetic-backup")
        result = self.compare(read_error="permission_denied")
        with self.assertRaises(BaselineComparisonError) as caught:
            require_baseline_match(result)
        self.assertEqual(caught.exception.code, "baseline_evidence_unavailable")

    def test_read_error_is_a_fixed_reason_code_without_raw_error_text(self):
        result = self.compare(read_error="SELECT secret FROM data; password=hidden")
        self.assertEqual(result["evidence_gaps"], ["current_snapshot_unavailable"])
        self.assertNotIn("password", json.dumps(result))

    def test_relation_difference_explanation_is_bounded(self):
        self.relations = [relation(id=f"new-{index}") for index in range(40)]
        result = self.compare(include_relation_differences=True)
        details = result["diagnostics"]["relations"]
        self.assertEqual(details["changed_count"], 41)
        self.assertEqual(len(details["items"]), 32)
        self.assertTrue(details["truncated"])

    def test_ordinary_aba_changes_revision_but_exact_snapshot_restore_cannot_prove_history(self):
        self.current["last_modification_time"] = "2026-10-27 12:00:01.000"
        self.assertEqual(self.compare()["change_categories"], ["revision_only_changed"])
        self.current = copy.deepcopy(self.baseline)
        result = self.compare()
        self.assertEqual(result["status"], "match")
        self.assertNotIn("execution_outcome", result)
        self.assertNotIn("committed", result)


if __name__ == "__main__":
    unittest.main()
