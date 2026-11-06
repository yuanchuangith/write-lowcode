from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from gxp_editor.hashing import sha256_json
from gxp_editor.logic_review import normalize_review_spec
from gxp_editor.plan_revision import operation_page, revise_operations, revise_review_spec


def operation(value="old", node="node"):
    return {"operation": "set_parameter", "action_group_key": "group", "target_node_key": node,
            "expected_element_key": "SetVariable", "path": "/inputParams/value",
            "expected_old_value": {"value": None, "label": "original"}, "value": value}


def replacement(operations, index, value):
    return {"index": index, "expected_operation_sha256": sha256_json(operations[index]), "operation": value}


def requirement(identifier="rule", text="Keep the declared field value"):
    return {"id": identifier, "text": text, "source_text": "User requested the declared field value",
            "targets": [{"group_key": "group", "node_key": "node"}]}


def assertion(identifier="check", value=None):
    return {"id": identifier, "requirement_ids": ["rule"],
            "target": {"group_key": "group", "node_key": "node", "element_key": "SetVariable"},
            "path": "/inputParams/value", "operator": "equals", "value": value}


def review_spec():
    return {"schema_version": 1, "revision": "user-1", "requirements": [requirement()],
            "canvas_assertions": [assertion()], "scope_policy": "strict",
            "scenarios": {"schema_version": 1, "cases": [{"id": "scenario", "requirement_ids": ["rule"],
                "environment": {"fields": {"field": {"value": None}}},
                "steps": [{"kind": "call_group", "group_key": "group", "args": [None]}],
                "expect": {"fields": {"field": {"value": None}}, "last_return": None}}]}}


class PlanOperationRevisionTests(unittest.TestCase):
    def test_replacements_guard_complete_original_entries_and_keep_order_and_nulls(self):
        original = [operation("first", "a"), operation("second", "b"), operation("third", "c")]
        before = deepcopy(original)
        update = [replacement(original, 2, operation(None, "c")), replacement(original, 0, operation("new", "a"))]
        revised = revise_operations(original, update)
        self.assertEqual([item["value"] for item in revised], ["new", "second", None])
        self.assertEqual([item["target_node_key"] for item in revised], ["a", "b", "c"])
        revised[1]["expected_old_value"]["label"] = "mutated"
        revised[0]["value"] = "different"
        self.assertEqual(original, before)
        self.assertEqual(update[1]["operation"]["value"], "new")

    def test_review_only_revision_still_returns_an_independent_complete_list(self):
        original = [operation(None)]
        for updates in (None, []):
            with self.subTest(updates=updates):
                revised = revise_operations(original, updates)
                self.assertEqual(revised, original)
                self.assertIsNot(revised, original)
                self.assertIsNot(revised[0], original[0])

    def test_replacement_shape_indexes_and_hashes_fail_closed(self):
        original = [operation()]
        valid = replacement(original, 0, operation("new"))
        cases = [
            {"index": 0}, {**valid, "extra": True}, {**valid, "index": True},
            {**valid, "index": -1}, {**valid, "index": 1},
            {**valid, "expected_operation_sha256": "0" * 64},
            {**valid, "expected_operation_sha256": "not-a-hash"},
            {**valid, "operation": None}, {**valid, "operation": []},
        ]
        for invalid in cases:
            with self.subTest(invalid_fields=list(invalid)):
                with self.assertRaises(ValueError):
                    revise_operations(original, [invalid])
        with self.assertRaises(ValueError):
            revise_operations(original, [valid, valid])
        with self.assertRaises(ValueError):
            revise_operations(original, valid)

    def test_non_json_and_non_finite_values_are_not_silently_normalized(self):
        for invalid in (operation(float("nan")), operation((1, 2)), operation({1: "bad"})):
            with self.subTest(value_type=type(invalid["value"]).__name__):
                with self.assertRaises(ValueError):
                    revise_operations([invalid])
        with self.assertRaises(ValueError):
            revise_operations([operation()], [replacement([operation()], 0, operation(float("inf")))])

    def test_combined_revisions_enforce_utf8_byte_budget_not_individual_sizes(self):
        original = [operation("a", "a"), operation("b", "b")]
        updated = [operation("中" * 60, "a"), operation("中" * 60, "b")]
        budget = len(json.dumps(original, ensure_ascii=False, separators=(",", ":")).encode()) + 220
        with patch("gxp_editor.plan_revision.MAX_OPERATION_BYTES", budget):
            # Either replacement fits by itself; the complete final list does not.
            revise_operations(original, [replacement(original, 0, updated[0])])
            revise_operations(original, [replacement(original, 1, updated[1])])
            with self.assertRaises(ValueError):
                revise_operations(original, [replacement(original, 0, updated[0]), replacement(original, 1, updated[1])])


class PlanOperationReadTests(unittest.TestCase):
    def test_summary_contains_identity_and_full_operation_digest_without_values(self):
        original = [operation({"private": "NEVER-IN-SUMMARY"}), operation("second", "other")]
        page = operation_page(original, "plan", limit=1)
        self.assertEqual(page["items"][0]["operation_sha256"], sha256_json(original[0]))
        self.assertEqual(page["items"][0]["target_node_key"], "node")
        self.assertEqual(page["items"][0]["index"], 0)
        self.assertEqual(page["next_offset"], 1)
        self.assertNotIn("operation", page["items"][0])
        self.assertNotIn("NEVER-IN-SUMMARY", json.dumps(page))

    def test_full_small_operation_is_exact_and_independent(self):
        original = [operation(None)]
        page = operation_page(original, "plan", detail="full")
        row = page["items"][0]
        self.assertTrue(row["operation_complete"])
        self.assertEqual(row["operation"], original[0])
        row["operation"]["expected_old_value"]["label"] = "changed"
        self.assertEqual(original[0]["expected_old_value"]["label"], "original")

    def test_large_full_operation_uses_bounded_exact_fragments_and_callable_hints(self):
        original = [operation({"content": "中🙂\\\n" * 3000})]
        maximum = 4096
        page = operation_page(original, "plan", detail="full", max_output_bytes=maximum, conversation_id="owner")
        self.assertFalse(page["complete"])
        self.assertNotIn("operation", page["items"][0])
        self.assertFalse(page["items"][0]["operation_complete"])
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), maximum)
        hint = page["next_read"]
        self.assertEqual(hint["tool"], "read_plan_review")
        self.assertEqual(hint["arguments"]["conversation_id"], "owner")
        self.assertEqual(hint["arguments"]["field_path"], "/operation")
        fragments = []
        while hint is not None:
            arguments = {key: value for key, value in hint["arguments"].items() if key != "section"}
            piece = operation_page(original, **arguments)
            self.assertLessEqual(len(json.dumps(piece, ensure_ascii=False).encode()), maximum)
            self.assertFalse(piece["complete"])
            self.assertNotIn("value", piece)
            fragments.append(piece["text"])
            hint = piece["next_read"]
        restored = json.loads("".join(fragments))
        self.assertEqual(restored, original[0])
        self.assertEqual(sha256_json(restored), page["items"][0]["operation_sha256"])

    def test_complete_field_value_preserves_null_without_returning_other_fields(self):
        page = operation_page([operation(None)], "plan", field_path="/operation/value", max_output_bytes=4096)
        self.assertTrue(page["complete"])
        self.assertIsNone(page["value"])
        self.assertNotIn("expected_old_value", page)

    def test_list_page_respects_budget_and_continuation_keeps_original_indexes(self):
        operations = [operation("v", str(index)) for index in range(20)]
        page = operation_page(operations, "plan", offset=3, limit=20, max_output_bytes=2048)
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), 2048)
        indexes = [item["index"] for item in page["items"]]
        self.assertEqual(indexes, list(range(3, 3 + len(indexes))))
        self.assertEqual(page["next_offset"], 3 + len(indexes))

    def test_invalid_read_parameters_do_not_expose_or_guess_operation(self):
        original = [operation()]
        cases = [{"offset": True}, {"offset": -1}, {"limit": 0}, {"detail": "raw"},
                 {"field_path": "/value"}, {"field_path": "/operation/value", "offset": 1},
                 {"field_path": "/operation/missing"}, {"max_output_bytes": 12}, {"value_offset": 1}]
        for arguments in cases:
            with self.subTest(arguments=arguments):
                with self.assertRaises(ValueError):
                    operation_page(original, "plan", **arguments)


class ReviewSpecRevisionTests(unittest.TestCase):
    def test_requirement_upsert_preserves_existing_order_and_complete_scenarios(self):
        original = review_spec()
        before = deepcopy(original)
        update = {"revision": "user-2", "requirements": [requirement(text="Updated original requirement"), requirement("extra")],
                  "change_source_text": "User supplied the updated requirement"}
        revised = revise_review_spec(original, update)
        self.assertEqual([item["id"] for item in revised["requirements"]], ["rule", "extra"])
        self.assertEqual(revised["requirements"][0]["text"], "Updated original requirement")
        self.assertEqual(revised["scenarios"], normalize_review_spec(original)["scenarios"])
        self.assertEqual(revised["canvas_assertions"][0]["value"], None)
        revised["scenarios"]["cases"][0]["environment"]["fields"]["field"]["value"] = "changed"
        self.assertEqual(original, before)
        self.assertEqual(update["requirements"][0]["text"], "Updated original requirement")

    def test_assertion_upsert_and_empty_update_arrays_do_not_delete_rows(self):
        original = review_spec()
        revised = revise_review_spec(original, {"revision": "next", "canvas_assertions": [assertion(value=False), assertion("new", None)]})
        self.assertEqual([item["id"] for item in revised["canvas_assertions"]], ["check", "new"])
        self.assertIs(revised["canvas_assertions"][0]["value"], False)
        self.assertIsNone(revised["canvas_assertions"][1]["value"])
        empty = revise_review_spec(original, {"revision": "next", "requirements": [], "canvas_assertions": []})
        self.assertEqual(empty["requirements"], normalize_review_spec(original)["requirements"])
        self.assertEqual(empty["canvas_assertions"], normalize_review_spec(original)["canvas_assertions"])

    def test_strict_update_rejects_unknown_policy_scenarios_duplicates_and_partial_rows(self):
        original = review_spec()
        updates = [{}, {"revision": True}, {"revision": " "}, {"revision": "a" * 129},
                   {"revision": "next", "schema_version": True}, {"revision": "next", "schema_version": 2},
                   {"revision": "next", "scope_policy": "manual_review"},
                   {"revision": "next", "scenarios": {"schema_version": 1, "cases": []}},
                   {"revision": "next", "requirements": None},
                   {"revision": "next", "requirements": [requirement(), requirement()]},
                   {"revision": "next", "requirements": [{"id": "rule", "text": "partial"}]},
                   {"revision": "next", "canvas_assertions": [assertion(), assertion()]},
                   {"revision": "next", "canvas_assertions": [{"id": " "}]},
                   {"revision": "next", "change_source_text": None}]
        for update in updates:
            with self.subTest(fields=list(update)):
                with self.assertRaises(ValueError):
                    revise_review_spec(original, update)

    def test_review_update_cannot_exceed_existing_review_budget(self):
        with patch("gxp_editor.plan_revision.MAX_REVIEW_BYTES", 100):
            with self.assertRaises(ValueError):
                revise_review_spec(review_spec(), {"revision": "next", "requirements": [requirement(text="x" * 200)]})


if __name__ == "__main__":
    unittest.main()
