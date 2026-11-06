from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from gxp_editor import runtime_package as runtime


class FormRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.result = self.base / "result.json"
        self.environment = {**os.environ, "LOCALAPPDATA": str(self.base / "local"),
                            "FORM_RUNTIME_TEST_RESULT": str(self.result)}
        for name in ("GXP_EDITOR_RUNTIME_ROOT", "GXP_EDITOR_RUNTIME_INSTANCE_ID",
                     "GXP_EDITOR_RUNTIME_MANIFEST_SHA256", "GXP_EDITOR_PYTHON_IDENTITY"):
            self.environment.pop(name, None)
        files = {
            ".codex-plugin/plugin.json": json.dumps({"version": "form-fixture-1"}),
            "requirements.txt": "# no optional dependencies\n",
            "mcp/gxp_editor/__init__.py": "",
            "mcp/gxp_form_editor/__init__.py": "",
            "scripts/approval_app.py": "# action approval fixture\n",
            "scripts/abort_session_app.py": "# action cancellation fixture\n",
        }
        files.update({name: "# form runtime fixture\n" for name in runtime.FORM_RUNTIME_FILES
                      if name.startswith("scripts/")})
        for kind, entrypoint in runtime.SERVER_ENTRYPOINTS.items():
            files[entrypoint] = (
                "import json, os\nfrom pathlib import Path\n"
                "Path(os.environ['FORM_RUNTIME_TEST_RESULT']).write_text(json.dumps("
                "{'server': " + repr(kind) + ", 'root': os.environ['GXP_EDITOR_RUNTIME_ROOT'], "
                "'instance': os.environ['GXP_EDITOR_RUNTIME_INSTANCE_ID']}), encoding='utf-8')\n"
            )
        for name, body in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        shutil.copyfile(runtime.__file__, self.source / "mcp/gxp_editor/runtime_package.py")
        runtime.write_manifest(self.source)

    def run_server(self, *arguments):
        return subprocess.run(
            [sys.executable, str(self.source / "mcp/gxp_editor/runtime_package.py"),
             "start", "--source", str(self.source), *arguments],
            env=self.environment, capture_output=True, text=True, encoding="utf-8", timeout=10,
        )

    def test_default_action_and_form_use_same_verified_package_with_separate_instances(self):
        action = self.run_server()
        self.assertEqual(action.returncode, 0, action.stderr)
        action_result = json.loads(self.result.read_text(encoding="utf-8"))
        form = self.run_server("--server", "form")
        self.assertEqual(form.returncode, 0, form.stderr)
        form_result = json.loads(self.result.read_text(encoding="utf-8"))
        self.assertEqual(action_result["server"], "action")
        self.assertEqual(form_result["server"], "form")
        self.assertEqual(action_result["root"], form_result["root"])
        self.assertNotEqual(action_result["instance"], form_result["instance"])
        self.assertNotEqual(Path(form_result["root"]), self.source)
        leases = self.base / "local/GxpLowcodeEditor/runtime/leases"
        self.assertEqual(list(leases.glob("*.json")), [])

    def test_entrypoint_is_an_enum_not_an_arbitrary_script_path(self):
        result = self.run_server("--server", "../external.py")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)
        self.assertFalse(self.result.exists())

    def test_missing_form_resources_block_form_but_keep_legacy_action_working(self):
        (self.source / "scripts/form_approval_app.py").unlink()
        runtime.write_manifest(self.source)
        result = self.run_server("--server", "form")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("plugin_runtime_path_missing", result.stderr)
        self.assertIn("scripts/form_approval_app.py", result.stderr)
        self.assertFalse(self.result.exists())
        action = self.run_server()
        self.assertEqual(action.returncode, 0, action.stderr)
        self.assertEqual(json.loads(self.result.read_text(encoding="utf-8"))["server"], "action")

    def test_form_entrypoint_and_launchers_are_bound_into_runtime_manifest(self):
        manifest = runtime.build_manifest(self.source)
        paths = {item["path"] for item in manifest["files"]}
        self.assertTrue(runtime.FORM_RUNTIME_FILES.issubset(paths))
        (self.source / "mcp/form_server.py").write_text("raise RuntimeError('changed')\n", encoding="utf-8")
        result = self.run_server("--server", "form")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("plugin_runtime_resource_changed", result.stderr)
        self.assertFalse(self.result.exists())


if __name__ == "__main__":
    unittest.main()
