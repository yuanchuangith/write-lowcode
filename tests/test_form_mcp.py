from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))
from gxp_form_editor.common import FormError

spec = importlib.util.spec_from_file_location("form_mcp_under_test", ROOT / "mcp/form_server.py")
server = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = server
spec.loader.exec_module(server)


class FormMcpTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_are_separate_and_never_expose_approval_or_execute(self):
        tools = await server.mcp.list_tools()
        names = {tool.name for tool in tools}
        self.assertIn("begin_form_edit_session", names)
        self.assertIn("plan_form_restore", names)
        self.assertIn("prepare_form_write", names)
        self.assertEqual(len(names), 15)
        self.assertFalse(any("execute" in name or "approve" in name or "sql" in name or "publish" in name for name in names))
        plan = next(tool for tool in tools if tool.name == "plan_form_patch")
        self.assertFalse(plan.inputSchema["$defs"]["FormOperation"]["additionalProperties"])
        self.assertNotIn("guard_ref", plan.inputSchema["$defs"]["FormOperation"]["properties"])

    async def test_actual_mcp_conversion_preserves_nested_readiness_and_failure(self):
        class Fake:
            def form_editor_status(self):
                return {"domain": "form_config", "write_ready": False, "read_ready": True,
                        "reasons": ["server_conditional_write_required"], "nested": {"count": 2}}

            def create_form_conversation(self, label):
                raise FormError("fixture_error", "Expected error", {"required": ["native_contract"]})

        with patch.object(server, "FormService", return_value=Fake()):
            result = await server.mcp.call_tool("form_editor_status", {})
            blocks = result[0] if isinstance(result, tuple) else result
            value = json.loads(next(block.text for block in blocks if block.type == "text"))
            self.assertIs(value["write_ready"], False)
            self.assertEqual(value["nested"]["count"], 2)
            self.assertIn("total_elapsed_ms", value)
            result = await server.mcp.call_tool("create_form_conversation", {"label": "test"})
            blocks = result[0] if isinstance(result, tuple) else result
            value = json.loads(next(block.text for block in blocks if block.type == "text"))
            self.assertEqual(value["error_code"], "fixture_error")
            self.assertEqual(value["details"]["required"], ["native_contract"])

    async def test_schema_rejects_arbitrary_url_and_raw_code_operations(self):
        tools = await server.mcp.list_tools()
        target = next(tool for tool in tools if tool.name == "begin_form_edit_session")
        self.assertFalse(target.inputSchema["$defs"]["PageTarget"]["additionalProperties"])
        with self.assertRaises(Exception):
            server.FormOperation(operation="set_property", component_id="field", expected_component_type="Input",
                                 path="/required", expected_old_value=False, value=True, raw_code="unsafe")

    async def test_actual_mcp_preserves_required_null_values_and_absence_guards(self):
        received = []
        class Fake:
            def plan_form_patch(self, session_id, operations, source_text):
                received.extend(operations)
                return {"no_changes": True}
        with patch.object(server, "FormService", return_value=Fake()):
            await server.mcp.call_tool("plan_form_patch", {
                "session_id": "fixture", "conversation_id": "fixture", "source_text": "Clear optional default",
                "operations": [{"operation": "set_default", "component_id": "field", "expected_component_type": "Input",
                                "path": "/default", "expected_old_value": None, "value": None, "expected_old_exists": False}]})
        self.assertIsNone(received[0]["expected_old_value"])
        self.assertIsNone(received[0]["value"])
        self.assertFalse(received[0]["expected_old_exists"])
        self.assertNotIn("platform", received[0])


if __name__ == "__main__":
    unittest.main()
