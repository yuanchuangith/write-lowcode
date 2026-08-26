from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from gxp_editor.compiler import CompilerBridge
from tests.helpers import editor_config


def registry(default_id: str, *, status: str = "display-visible") -> dict:
    return {
        "ok": True,
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
        return CompilerBridge(config)

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
            validation = {"ok": True, "validation": {"valid": True, "registry_hash": "unstable"}}
            with patch.object(bridge, "_call", side_effect=[validation, registry("ROW")]):
                result = bridge.validate_design({"actionData": []})

        self.assertEqual(
            result["validation"]["registry_hash"],
            bridge.describe_registry()["registry_hash"],
        )


if __name__ == "__main__":
    unittest.main()
