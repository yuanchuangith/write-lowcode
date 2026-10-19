from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from gxp_editor.compiler import CompilerBridge, CompilerError
from tests.helpers import editor_config


def registry(default_id: str, *, status: str = "display-visible") -> dict:
    return {
        "ok": True,
        "command": "describe_registry",
        "frontend_version": "test",
        "rule_source_sha256": "a" * 64,
        "registry_hash": "unstable-bridge-hash",
        "elements": [
            {
                "key": "SetComponentStatus",
                "input_params": [
                    {
                        "defaultValue": [
                            {
                                "id": default_id,
                                "status": status,
                            }
                        ]
                    }
                ],
            }
        ],
    }


class CompilerBridgeTests(unittest.TestCase):
    def bridge(self, root: Path) -> CompilerBridge:
        (root / "bridge.js").write_text("// test bridge\n", encoding="utf-8")
        config = replace(editor_config(), frontend_repo=str(root))
        bridge = CompilerBridge(config)
        # This fixture mocks Node responses, not a real frontend build.
        bridge._verify_native_sources = lambda: "a" * 64
        bridge.source_provenance = lambda: {
            "ready": True,
            "frontend_repo": str(root),
            "commit": "d" * 40,
            "branch": "test",
            "dirty": False,
            "dirty_file_count": 0,
            "workspace_sha256": "e" * 64,
            "bridge_path": "bridge.js",
        }
        return bridge

    def test_node_output_is_decoded_as_utf8(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary))
            localized_name = "\u8bbe\u7f6e\u7ec4\u4ef6\u72b6\u6001"
            completed = subprocess.CompletedProcess(
                ["node", str(bridge.path)],
                0,
                json.dumps({"ok": True, "name": localized_name}, ensure_ascii=False),
                "",
            )
            with patch("gxp_editor.compiler.subprocess.run", return_value=completed) as run:
                result = bridge._call({"command": "describe_registry"})

        self.assertEqual(result["name"], localized_name)
        self.assertEqual(run.call_args.kwargs["encoding"], "utf-8")

    def test_registry_hash_ignores_generated_default_row_ids(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_bridge = self.bridge(Path(first))
            second_bridge = self.bridge(Path(second))
            with patch.object(first_bridge, "_call", return_value=registry("ROW-ONE")):
                first_hash = first_bridge.describe_registry()["registry_hash"]
            with patch.object(second_bridge, "_call", return_value=registry("ROW-TWO")):
                second_hash = second_bridge.describe_registry()["registry_hash"]

        self.assertEqual(first_hash, second_hash)

    def test_registry_hash_changes_for_semantic_defaults(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_bridge = self.bridge(Path(first))
            second_bridge = self.bridge(Path(second))
            with patch.object(first_bridge, "_call", return_value=registry("ROW", status="display-visible")):
                first_hash = first_bridge.describe_registry()["registry_hash"]
            with patch.object(second_bridge, "_call", return_value=registry("ROW", status="display-hidden")):
                second_hash = second_bridge.describe_registry()["registry_hash"]

        self.assertNotEqual(first_hash, second_hash)

    def test_validation_is_bound_to_the_stable_registry_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary))
            validation = {
                "ok": True,
                "validation": {
                    "valid": True,
                    "registry_hash": "unstable",
                    "frontend_version": "test",
                    "rule_source_sha256": "a" * 64,
                },
            }
            with patch.object(bridge, "_call", side_effect=[validation, registry("ROW")]):
                result = bridge.validate_design({"actionData": []})
            active_registry_hash = bridge.describe_registry()["registry_hash"]

        self.assertEqual(
            result["validation"]["registry_hash"],
            active_registry_hash,
        )

    def test_invalid_canvas_validation_is_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary))
            invalid = {
                "ok": True,
                "validation": {
                    "valid": False,
                    "frontend_version": "test",
                    "rule_source_sha256": "a" * 64,
                },
            }
            with patch.object(bridge, "_call", side_effect=[invalid, registry("ROW")]):
                with self.assertRaisesRegex(CompilerError, "valid canvas"):
                    bridge.validate_design({"actionData": []})

    def test_missing_generated_javascript_is_blocking(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary))
            result = {
                "design": {
                    "actionData": [{"key": "group", "actionType": "js", "data": []}],
                    "globalVariables": [],
                },
                "js_code": "",
                "csharp_code": "",
                "relation_data": {},
            }
            with self.assertRaisesRegex(CompilerError, "no JavaScript"):
                bridge.verify_compiled_result(result)

    def test_source_provenance_is_bound_into_compiler_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary))
            with patch.object(bridge, "_call", return_value=registry("ROW")):
                binding = bridge.compiler_binding()

        self.assertEqual(binding["source_provenance"]["commit"], "d" * 40)
        self.assertRegex(binding["compiler_binding_sha256"], r"^[0-9a-f]{64}$")

    def test_invisible_custom_code_is_blocked_before_compiler_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            bridge = self.bridge(Path(temporary))
            for element, section, field in (
                ("SetVariable", "inputParams", "variableValue"),
                ("SetVariable", "outputParams", "variableName"),
                ("SetVariableValue", "inputParams", "attributeValue"),
            ):
                for custom in ({"code": "newRule"}, {"value": "oldRule", "code": "newRule"}):
                    with self.subTest(element=element, custom=custom):
                        design = {"actionData": [{"title": "主动作", "data": [{
                            "elementKey": element, "title": "变量元件",
                            "paramsValue": {section: {field: {"paramTypes": "custom", **custom}}},
                        }]}]}
                        with patch.object(bridge, "_call") as call:
                            with self.assertRaisesRegex(CompilerError, "显示值.*不一致"):
                                bridge.validate_design(design)
                            call.assert_not_called()

    def test_editable_expression_and_selected_reference_are_supported(self):
        expression = "isNew ? parameter : currentValue"
        for parameter in (
            {"paramTypes": "custom", "value": expression, "code": expression, "label": "条件赋值"},
            {"paramTypes": "localVariable", "value": "localVariable-result", "code": "result"},
        ):
            CompilerBridge._validate_editable_variable_values({"actionData": [{"data": [{
                "elementKey": "SetVariable", "paramsValue": {"inputParams": {"variableValue": parameter}},
            }]}]})


if __name__ == "__main__":
    unittest.main()
