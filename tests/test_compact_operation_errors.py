"""Malformed input identifies its real branch without unrelated union errors."""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from pydantic import TypeAdapter, ValidationError
from tests.test_conversation_tools import SERVER
from gxp_editor.tool_inputs import CanvasOperation


class CompactOperationErrorTests(unittest.TestCase):
    def test_missing_group_has_one_error_instead_of_every_operation_branch(self):
        with self.assertRaises(ValidationError) as caught:
            TypeAdapter(CanvasOperation).validate_python({"operation": "append_node", "element_key": "OutputLog"})
        errors = caught.exception.errors(include_input=False)
        self.assertEqual(len(errors), 1)
        self.assertIn("action_group_key", errors[0]["loc"])
        self.assertLess(len(str(caught.exception).encode("utf-8")), 1000)

    def test_guard_modes_still_reject_both_or_missing_without_service_construction(self):
        operation = {"operation": "set_parameter", "action_group_key": "g", "target_node_key": "n",
                     "expected_element_key": "DataFilter", "path": "/inputParams/x", "value": None}
        for value in (operation, {**operation, "guard_ref": "signed", "expected_old_value": None}):
            with self.subTest(value=value), patch.object(SERVER, "service") as factory:
                with self.assertRaises(Exception) as caught:
                    asyncio.run(SERVER.mcp.call_tool("plan_canvas_patch", {"session_id": "s", "operations": [value]}))
                factory.assert_not_called()
                self.assertLess(len(str(caught.exception).encode("utf-8")), 1600)

    def test_null_and_guarded_variants_retain_original_exact_json(self):
        base = {"operation": "set_parameter", "action_group_key": "g", "target_node_key": "n",
                "expected_element_key": "DataFilter", "path": "/inputParams/x", "value": None}
        for guard in ({"expected_old_value": None}, {"guard_ref": "signed"}):
            value = {**base, **guard}
            self.assertEqual(TypeAdapter(CanvasOperation).validate_python(value), value)

    def test_unknown_large_operation_tag_cannot_expand_error_text(self):
        with self.assertRaises(ValidationError) as caught:
            TypeAdapter(CanvasOperation).validate_python({"operation": "unknown-" + "x" * 50000})
        self.assertLess(len(str(caught.exception).encode("utf-8")), 1500)


if __name__ == "__main__":
    unittest.main()
