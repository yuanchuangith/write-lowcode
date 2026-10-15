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
                if name == "attach_edit_session":
                    self.assertIn("conversation_id", schema["required"])
                    self.assertIn("session_id", schema["required"])
                else:
                    self.assertEqual(schema["properties"]["conversation_id"]["default"], "")
                    self.assertNotIn("conversation_id", schema.get("required", []))

    def test_mcp_calls_preserve_named_and_legacy_scope(self) -> None:
        class ScopedService:
            def __init__(self, conversation_id: str = "") -> None:
                self.conversation_id = conversation_id

            def editor_status(self):
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
                    self.assertEqual(result, expected)
            _, result = asyncio.run(
                SERVER.mcp.call_tool(
                    "attach_edit_session", {"session_id": "shared-flow", "conversation_id": "dialogue-b"}
                )
            )
            self.assertEqual(result, {"conversation_id": "dialogue-b", "session_id": "shared-flow"})


if __name__ == "__main__":
    unittest.main()
