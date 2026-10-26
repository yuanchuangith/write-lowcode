from __future__ import annotations

import asyncio
import copy
import json
import unittest
from unittest.mock import Mock, patch

from tests import test_context_service as context_fixtures
from tests.helpers import relation
from gxp_editor.node_discovery import NodeDiscoveryError, find_baseline_nodes
from gxp_editor.service import EditorServiceError


class NodeDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.registry = {"elements": [{"key": "Query", "input_params": [], "output_params": []},
                                      {"key": "If", "input_params": [], "output_params": []}]}
        self.design = {"actionData": [
            {"key": "group-a", "title": "Shared group", "actionType": "js", "data": [
                {"key": "condition", "elementKey": "If", "title": "Condition", "depth": []},
                {"key": "query-a", "elementKey": "Query", "title": "Same query", "depth": ["condition"],
                 "paramsValue": {"inputParams": {"password": "private-value", "sql": "hidden-sql"}, "outputParams": {}}}]},
            {"key": "group-b", "title": "Shared group", "actionType": "csharp", "data": [
                {"key": "query-b", "elementKey": "Query", "title": "Same query", "depth": []}]},
            {"key": "empty", "title": "Empty group", "actionType": "js", "data": []}], "globalVariables": []}

    def find(self, **kwargs):
        return find_baseline_nodes(self.design, self.registry, **kwargs)

    def test_duplicate_names_return_all_exact_targets_and_original_canvas_locations(self):
        before = copy.deepcopy(self.design)
        result = self.find(query="same QUERY")
        self.assertEqual([(r["group_key"], r["node_key"]) for r in result["items"]],
                         [("group-a", "query-a"), ("group-b", "query-b")])
        self.assertEqual([r["line"] for r in result["items"]], [2, 1])
        self.assertEqual(result["items"][0]["depth"], ["condition"])
        self.assertEqual([r["group_index"] for r in result["items"]], [0, 1])
        self.assertEqual(self.design, before)

    def test_pages_are_stable_bounded_and_preserve_native_canvas_order(self):
        first = self.find(limit=1)
        second = self.find(offset=first["next_offset"], limit=1)
        final = self.find(offset=second["next_offset"], limit=1)
        self.assertEqual([page["items"][0]["node_key"] for page in [first, second, final]],
                         ["condition", "query-a", "query-b"])
        self.assertEqual(first["total"], 3)
        self.assertIsNone(final["next_offset"])
        self.assertEqual(self.find(offset=300)["items"], [])

    def test_group_pages_include_empty_and_duplicate_named_groups(self):
        page = self.find(kind="groups", query="Shared", limit=1)
        self.assertEqual(page["items"][0]["group_key"], "group-a")
        self.assertEqual(page["total"], 2)
        self.assertEqual(self.find(kind="groups", query="Empty")["items"][0]["node_count"], 0)
        self.assertEqual(self.find(kind="groups", element_key="If")["items"][0]["group_key"], "group-a")

    def test_exact_filters_and_label_search_never_search_or_expose_raw_parameters(self):
        self.assertEqual(self.find(group_key="group-b", element_key="Query")["items"][0]["node_key"], "query-b")
        self.assertEqual(self.find(query="private-value")["total"], 0)
        self.assertEqual(self.find(query="hidden-sql")["total"], 0)
        self.assertEqual(self.find(query="Shared group")["total"], 3)
        self.assertEqual(self.find(element_key="does-not-exist")["items"], [])
        output = json.dumps(self.find())
        for private in ("private-value", "hidden-sql", "password", "paramsValue", "inputParams"):
            self.assertNotIn(private, output)

    def test_invalid_paging_and_filters_reject_instead_of_coercing(self):
        for kwargs in ({"limit": 0}, {"limit": 101}, {"limit": True}, {"offset": -1}, {"offset": False},
                       {"query": []}, {"query": "x" * 201}, {"group_key": " "}, {"kind": "unknown"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(NodeDiscoveryError):
                self.find(**kwargs)

    def test_hidden_duplicate_keys_invalid_depth_or_unknown_native_elements_block_discovery(self):
        mutations = [
            lambda d: d["actionData"][1]["data"][0].update(key="query-a"),
            lambda d: d["actionData"][1].update(key="group-a"),
            lambda d: d["actionData"][1]["data"][0].update(depth=["condition"]),
            lambda d: d["actionData"][1]["data"][0].update(elementKey="Unknown")]
        for mutate in mutations:
            design = copy.deepcopy(self.design)
            mutate(design)
            with self.subTest(mutate=mutate), self.assertRaises(NodeDiscoveryError):
                find_baseline_nodes(design, self.registry, group_key="group-a", limit=1)

    def test_long_display_labels_are_bounded_and_keys_are_never_truncated(self):
        node = self.design["actionData"][0]["data"][0]
        node.update(title="label-" * 10000, key="native-" * 70)
        self.design["actionData"][0]["data"][1]["depth"] = [node["key"]]
        row = self.find(limit=1)["items"][0]
        self.assertEqual(row["node_key"], node["key"])
        self.assertEqual(len(row["title"]), 256)
        self.assertTrue(row["title_truncated"])


class NodeDiscoveryServiceTests(unittest.TestCase):
    # Share the existing in-memory service/ownership fixtures, with no Look or DB.
    setUp = context_fixtures.ContextServiceTests.setUp
    service_for = context_fixtures.ContextServiceTests.service_for
    forbid_writes = context_fixtures.ContextServiceTests.forbid_writes

    def test_standalone_discovery_is_read_only_and_returns_the_inspection_binding(self):
        before = self.state.get_session(self.session["id"])
        with self.forbid_writes(), patch.object(self.service, "inspect_edit_context",
                side_effect=AssertionError("Discovery must not need known targets or Look")):
            result = self.service.find_edit_nodes(self.session["id"])
        self.assertEqual(result["items"][0]["node_key"], "query")
        self.assertEqual(result["backup_id"], "backup-context")
        self.assertEqual(result["compiler_binding"], self.binding)
        self.assertEqual(result["baseline_content_hash"], self.draft["content_hash"])
        self.assertEqual(result["context_binding"]["owner_revision"], 0)
        self.assertEqual(self.state.get_session(self.session["id"]), before)
        self.assertTrue(result["read_only"])
        self.assertFalse(result["write_executed"])
        self.assertEqual(self.state.session_transactions(self.session["id"]), [])
        self.compiler.compile_patch.assert_not_called()

    def test_unknown_owner_rejects_before_loading_components(self):
        self.service._conversation_id = self.other_owner
        with self.assertRaises(EditorServiceError):
            self.service.find_edit_nodes(self.session["id"])
        self.components.assert_not_called()

    def test_every_page_rechecks_draft_relations_and_compiler_version(self):
        self.service.find_edit_nodes(self.session["id"], limit=1)
        self.repository.load_draft_by_id.return_value["content_hash"] = "b" * 64
        with self.assertRaisesRegex(EditorServiceError, "Draft changed.*stale"):
            self.service.find_edit_nodes(self.session["id"], offset=1, limit=1)
        self.repository.load_draft_by_id.return_value = copy.deepcopy(self.draft)
        self.repository.load_relations.return_value = [relation()]
        with self.assertRaisesRegex(EditorServiceError, "relations changed.*stale"):
            self.service.find_edit_nodes(self.session["id"], offset=1, limit=1)
        self.repository.load_relations.return_value = []
        self.compiler.compiler_binding.return_value = {"compiler_binding_sha256": "b" * 64}
        with self.assertRaisesRegex(EditorServiceError, "compiler binding changed.*stale"):
            self.service.find_edit_nodes(self.session["id"], offset=1, limit=1)

    def test_transfer_during_loading_and_round_trip_ownership_change_reject_results(self):
        def transfer():
            self.state.transfer_session(self.session["id"], self.other_owner, expected_owner=self.owner, expected_revision=0)
            self.state.transfer_session(self.session["id"], self.owner, expected_owner=self.other_owner, expected_revision=1)
            return self.registry
        self.compiler.describe_registry.side_effect = transfer
        with self.assertRaisesRegex(EditorServiceError, "ownership changed"):
            self.service.find_edit_nodes(self.session["id"])

    def test_inactive_session_rejects_before_loading_components(self):
        self.state.update_session(self.session["id"], "finished", conversation_id=self.owner, owner_revision=0)
        with self.assertRaisesRegex(EditorServiceError, "active edit session"):
            self.service.find_edit_nodes(self.session["id"])
        self.components.assert_not_called()


class NodeDiscoveryToolTests(unittest.TestCase):
    def test_tool_forwards_bounded_filters_and_conversation_scope(self):
        from tests import test_conversation_tools as tool_fixtures
        server = tool_fixtures.SERVER
        fake = Mock()
        fake.find_edit_nodes.return_value = {"items": [], "write_executed": False}
        with patch.object(server, "service", return_value=fake) as scoped:
            _, result = asyncio.run(server.mcp.call_tool("find_edit_nodes", {"session_id": "s",
                "conversation_id": "owner", "kind": "groups", "query": "label", "group_key": "g",
                "element_key": "Query", "offset": 10, "limit": 5}))
        scoped.assert_called_once_with("owner")
        fake.find_edit_nodes.assert_called_once_with("s", kind="groups", query="label", group_key="g",
                                                      element_key="Query", offset=10, limit=5)
        self.assertFalse(result["write_executed"])


if __name__ == "__main__":
    unittest.main()
