from __future__ import annotations

import asyncio
import importlib.util
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("gxp_editor_tool_server", SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class ConversationToolContractTests(unittest.TestCase):
    def test_create_conversation_survives_mcp_output_validation_without_edit_session(self):
        import json
        from gxp_editor.state import StateStore

        with TemporaryDirectory() as folder:
            state = StateStore(Path(folder) / "editor.sqlite3")
            with patch.object(SERVER, "service", return_value=SimpleNamespace(create_conversation=state.create_conversation)):
                content, result = asyncio.run(SERVER.mcp.call_tool("create_conversation", {"label": "  transport test  "}))
            self.assertRegex(result["conversation_id"], r"^[0-9a-f]{32}$")
            self.assertEqual(result["label"], "transport test")
            self.assertEqual(result["timings"], [])
            self.assertIsInstance(result["total_elapsed_ms"], float)
            self.assertGreaterEqual(result["total_elapsed_ms"], 0)
            self.assertRegex(result["trace_id"], r"^[0-9a-f]{32}$")
            self.assertEqual(json.loads(content[0].text), result)
            with state._connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 1)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM edit_queues").fetchone()[0], 0)

    def test_create_conversation_failure_survives_mcp_output_validation(self):
        from gxp_editor.state import StateStore

        with TemporaryDirectory() as folder:
            state = StateStore(Path(folder) / "editor.sqlite3")
            with patch.object(SERVER, "service", return_value=SimpleNamespace(create_conversation=state.create_conversation)):
                _, result = asyncio.run(SERVER.mcp.call_tool("create_conversation", {"label": "x" * 121}))
            self.assertTrue(result["blocked"])
            self.assertFalse(result["write_executed"])
            self.assertEqual(result["error_code"], "StateError")
            self.assertIsInstance(result["timings"], list)
            self.assertIsInstance(result["total_elapsed_ms"], float)
            self.assertIsInstance(result["evidence_gaps"], list)
            self.assertIsInstance(result["fallback"], dict)
            with state._connection() as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 0)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM edit_queues").fetchone()[0], 0)

    def test_settings_entry_is_available_without_editor_service_or_conversation(self):
        import sys
        from types import ModuleType

        tools = {tool.name: tool for tool in asyncio.run(SERVER.mcp.list_tools())}
        self.assertEqual(29, len(tools))
        properties = tools["database_settings"].inputSchema["properties"]
        self.assertEqual({"operation", "changes", "expected_revision"}, set(properties))
        self.assertEqual("status", properties["operation"]["default"])
        self.assertEqual({"status", "open", "update"}, set(properties["operation"]["enum"]))
        backend = ModuleType("gxp_editor.connection_settings")
        with patch.object(backend, "manage_settings", create=True, return_value={"configured": False}) as manage, \
             patch.dict(sys.modules, {"gxp_editor.connection_settings": backend}), \
             patch.object(SERVER, "service", side_effect=AssertionError("No editor service required")):
            _, status = asyncio.run(SERVER.mcp.call_tool("database_settings", {}))
            self.assertFalse(status["configured"])
            manage.assert_called_once_with("status", None, "")
            asyncio.run(SERVER.mcp.call_tool("database_settings", {"operation": "update", "changes": {"database": "dev_test"}, "expected_revision": "revision"}))
            manage.assert_called_with("update", {"database": "dev_test"}, "revision")
            asyncio.run(SERVER.mcp.call_tool("database_settings", {"operation": "open"}))
            manage.assert_called_with("open", None, "")

    def test_chat_cancellation_exposes_preview_and_requires_bound_user_reply(self):
        from unittest.mock import Mock
        tools = {tool.name: tool for tool in asyncio.run(SERVER.mcp.list_tools())}
        self.assertIn('inspect_session_cancellation', tools)
        self.assertEqual(set(tools['cancel_unexecuted_session'].inputSchema['required']),
                         {'session_id', 'cancellation_fingerprint', 'user_confirmation'})
        fake = Mock()
        fake.cancel_unexecuted_session.return_value = {'cancelled': True, 'write_executed': False}
        arguments = {'session_id': 'old-session', 'conversation_id': 'owner',
                     'cancellation_fingerprint': 'a' * 64, 'user_confirmation': '同意取消上述旧审核'}
        with patch.object(SERVER, 'service', return_value=fake) as scoped:
            _, result = asyncio.run(SERVER.mcp.call_tool('cancel_unexecuted_session', arguments))
        scoped.assert_called_once_with('owner')
        fake.cancel_unexecuted_session.assert_called_once_with('old-session', 'a' * 64, '同意取消上述旧审核')
        self.assertFalse(result['write_executed'])

    def test_local_release_tool_requires_scoped_confirmation_and_forwards_it(self):
        from unittest.mock import Mock
        tools = {tool.name: tool for tool in asyncio.run(SERVER.mcp.list_tools())}
        self.assertEqual(set(tools["release_local_session"].inputSchema["required"]),
                         {"session_id", "recovery_fingerprint", "user_confirmation"})
        fake = Mock()
        fake.release_local_session.return_value = {"session_released": True, "verified": False, "business_data_written": False}
        with patch.object(SERVER, "service", return_value=fake) as factory:
            _, result = asyncio.run(SERVER.mcp.call_tool("release_local_session", {"session_id": "s",
                "recovery_fingerprint": "a" * 64, "user_confirmation": "同意保留当前数据并释放上述本地流程",
                "conversation_id": "owner"}))
        factory.assert_called_once_with("owner")
        fake.release_local_session.assert_called_once_with("s", "a" * 64, "同意保留当前数据并释放上述本地流程")
        self.assertFalse(result["verified"])
        self.assertFalse(result["business_data_written"])

    def test_new_tools_and_default_summary_are_exposed(self):
        tools = {tool.name: tool for tool in asyncio.run(SERVER.mcp.list_tools())}
        self.assertIn("inspect_edit_context", tools)
        self.assertIn("read_plan_review", tools)
        for name in ("plan_canvas_patch", "plan_reference_repairs", "prepare_write", "open_plan_review",
                     "begin_edit_session", "begin_reference_repair_session", "inspect_edit_context"):
            self.assertEqual(tools[name].inputSchema["properties"]["response_detail"]["default"], "summary")
        self.assertEqual(tools["read_plan_review"].inputSchema["properties"]["detail"]["default"], "summary")
        self.assertIn("read_edit_value", tools)
        self.assertIs(tools["editor_status"].inputSchema["properties"]["probe_runtime"]["default"], False)

    def test_compiler_error_location_survives_mcp_transport(self):
        from unittest.mock import Mock
        from gxp_editor.compiler import CompilerError
        fake = Mock()
        fake.plan_canvas_patch.side_effect = CompilerError("Old value changed", details={
            "operation_index": 2, "group_key": "main", "node_key": "n", "path": "/inputParams/value",
            "reason_code": "old_value_mismatch", "actual_value": "private"})
        with patch.object(SERVER, "service", return_value=fake):
            _, result = asyncio.run(SERVER.mcp.call_tool("plan_canvas_patch", {"session_id": "s", "operations": []}))
        self.assertFalse(result["plan_created"])
        self.assertEqual(result["error_details"]["operation_index"], 2)
        self.assertEqual(result["error_details"]["reason_code"], "old_value_mismatch")
        self.assertNotIn("private", str(result))

    def test_focused_value_and_report_paging_forward_exact_arguments(self):
        from unittest.mock import Mock
        fake = Mock()
        fake.inspect_edit_context.return_value = fake.read_edit_value.return_value = fake.read_plan_review.return_value = {"read_only": True}
        targets = [{"group_key": "main", "node_key": "target"}]
        with patch.object(SERVER, "service", return_value=fake) as scoped:
            asyncio.run(SERVER.mcp.call_tool("inspect_edit_context", {"session_id": "s", "targets": targets,
                "conversation_id": "owner", "parameter_paths": ["/inputParams/value"]}))
            fake.inspect_edit_context.assert_called_once_with("s", targets, operation_cursor=0,
                include_context=None, response_detail="summary", parameter_paths=["/inputParams/value"])
            asyncio.run(SERVER.mcp.call_tool("inspect_edit_context", {"session_id": "s", "targets": targets,
                "conversation_id": "owner", "response_detail": "full", "include_context": False}))
            fake.inspect_edit_context.assert_called_with("s", targets, operation_cursor=0,
                include_context=False, response_detail="full", parameter_paths=None)
            selector = {"field": "key", "value": "native-row"}
            asyncio.run(SERVER.mcp.call_tool("read_edit_value", {"session_id": "s", "group_key": "main",
                "node_key": "target", "path": "/inputParams/rows", "conversation_id": "owner",
                "item_key": selector, "offset": 2, "max_output_bytes": 4096}))
            fake.read_edit_value.assert_called_once_with("s", "main", "target", "/inputParams/rows",
                item_key=selector, offset=2, max_output_bytes=4096)
            asyncio.run(SERVER.mcp.call_tool("read_plan_review", {"plan_id": "p", "section": "changes",
                "item_id": "change-1", "conversation_id": "owner", "field_path": "/before/text",
                "value_offset": 2, "max_output_bytes": 4096}))
            fake.read_plan_review.assert_called_once_with("p", section="changes", offset=0, limit=10,
                item_id="change-1", detail="summary", field_path="/before/text", value_offset=2, max_output_bytes=4096)
            self.assertTrue(all(call.args == ("owner",) for call in scoped.call_args_list))

    def test_generation_modes_are_exposed_and_forwarded_with_full_default(self):
        from unittest.mock import Mock
        from mcp.server.fastmcp.exceptions import ToolError
        tools = {tool.name: tool for tool in asyncio.run(SERVER.mcp.list_tools())}
        mode = tools["plan_canvas_patch"].inputSchema["properties"]["generation_mode"]
        self.assertEqual(mode["default"], "full")
        self.assertEqual(set(mode["enum"]), {"full", "incremental", "auto"})
        fake = Mock()
        fake.plan_canvas_patch.return_value = {"plan_created": True}
        with patch.object(SERVER, "service", return_value=fake):
            for requested in (None, "full", "incremental", "auto"):
                arguments = {"session_id": "s", "operations": []}
                if requested is not None:
                    arguments["generation_mode"] = requested
                asyncio.run(SERVER.mcp.call_tool("plan_canvas_patch", arguments))
                self.assertEqual(fake.plan_canvas_patch.call_args.kwargs["generation_mode"], requested or "full")
            calls = fake.plan_canvas_patch.call_count
            with self.assertRaisesRegex(ToolError, "generation_mode"):
                asyncio.run(SERVER.mcp.call_tool("plan_canvas_patch", {"session_id": "s", "operations": [],
                                                                      "generation_mode": "invalid"}))
            self.assertEqual(fake.plan_canvas_patch.call_count, calls)

    def test_registered_tools_expose_dialogue_scope_and_explicit_takeover(self) -> None:
        tools = {tool.name: tool for tool in asyncio.run(SERVER.mcp.list_tools())}
        self.assertIn("create_conversation", tools)
        self.assertIn("attach_edit_session", tools)
        for name, tool in tools.items():
            if name in {"create_conversation", "database_settings"}:
                continue
            with self.subTest(tool=name):
                schema = tool.inputSchema
                self.assertEqual(schema["properties"]["conversation_id"]["type"], "string")
                if name in {"attach_edit_session", "create_edit_queue", "get_edit_queue"}:
                    self.assertIn("conversation_id", schema["required"])
                    if name == "attach_edit_session":
                        self.assertIn("session_id", schema["required"])
                    if name == "get_edit_queue":
                        self.assertIn("queue_id", schema["required"])
                else:
                    self.assertEqual(schema["properties"]["conversation_id"]["default"], "")
                    self.assertNotIn("conversation_id", schema.get("required", []))

    def test_mcp_calls_preserve_named_and_legacy_scope(self) -> None:
        class ScopedService:
            def __init__(self, conversation_id: str = "") -> None:
                self.conversation_id = conversation_id

            def editor_status(self, *, probe_runtime=False):
                return {"conversation_id": self.conversation_id}

            def attach_edit_session(self, session_id: str):
                return {"conversation_id": self.conversation_id, "session_id": session_id}

        with patch.object(SERVER, "EditorService", ScopedService):
            for arguments, expected in (
                ({"conversation_id": "dialogue-a"}, {"conversation_id": "dialogue-a"}),
                ({"conversation_id": "dialogue-b"}, {"conversation_id": "dialogue-b"}),
                ({}, {"conversation_id": ""}),
            ):
                with self.subTest(arguments=arguments):
                    _, result = asyncio.run(SERVER.mcp.call_tool("editor_status", arguments))
                    self.assertEqual({key: value for key, value in result.items() if key not in {"trace_id", "timings", "total_elapsed_ms"}}, expected)
                    self.assertRegex(result["trace_id"], r"^[0-9a-f]{32}$")
            _, result = asyncio.run(
                SERVER.mcp.call_tool(
                    "attach_edit_session", {"session_id": "shared-flow", "conversation_id": "dialogue-b"}
                )
            )
            self.assertEqual({key: value for key, value in result.items() if key not in {"trace_id", "timings", "total_elapsed_ms"}},
                {"conversation_id": "dialogue-b", "session_id": "shared-flow"})


if __name__ == "__main__":
    unittest.main()
