from __future__ import annotations

import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).resolve().parents[1] / "mcp" / "server.py"
SPEC = importlib.util.spec_from_file_location("gxp_editor_tool_server", SERVER_PATH)
assert SPEC is not None and SPEC.loader is not None
SERVER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SERVER)


class ConversationToolContractTests(unittest.TestCase):
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
        for name in ("plan_canvas_patch", "plan_reference_repairs", "prepare_write", "open_plan_review"):
            self.assertEqual(tools[name].inputSchema["properties"]["response_detail"]["default"], "summary")
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
            if name == "create_conversation":
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
