from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from gxp_editor import runtime_package as runtime


class RuntimePackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.storage = self.base / "storage"
        self.environment = patch.dict(os.environ, {
            name: "" for name in ("GXP_EDITOR_RUNTIME_ROOT", "GXP_EDITOR_RUNTIME_INSTANCE_ID",
                                 "GXP_EDITOR_RUNTIME_MANIFEST_SHA256", "GXP_EDITOR_PYTHON_IDENTITY")})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        inventory = patch.object(runtime, "_legacy_environment_users", return_value=[])
        inventory.start()
        self.addCleanup(inventory.stop)
        self.fixture()

    def fixture(self, version="fixture-1"):
        files = {
            ".codex-plugin/plugin.json": json.dumps({"version": version}),
            "requirements.txt": "# isolated fixture; no dependencies\n",
            "mcp/server.py": "# no business services\n",
            "mcp/gxp_editor/__init__.py": "",
            "mcp/gxp_editor/late_dependency.py": "VALUE = '冻结字节🙂'\n",
            "mcp/gxp_editor/compiler_runner.cjs": "console.log('fixture');\n",
            "mcp/gxp_editor/rules.json": '{"rule": "精确"}\n',
            "scripts/approval_app.py": "# harmless stub\n",
            "scripts/abort_session_app.py": "# harmless stub\n",
            "assets/action-icons/stub.svg": "<svg/>\n",
        }
        for relative, body in files.items():
            path = self.source / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        shutil.copyfile(runtime.__file__, self.source / "mcp/gxp_editor/runtime_package.py")
        runtime.write_manifest(self.source)

    def test_manifest_covers_late_import_rules_ui_and_assets_but_excludes_state(self):
        (self.source / "credential.json").write_text('{"not": "a runtime dependency"}')
        (self.source / "scripts/benchmark_private.py").write_text("# not a runtime dependency")
        manifest = runtime.build_manifest(self.source)
        names = {row["path"] for row in manifest["files"]}
        self.assertIn("mcp/gxp_editor/late_dependency.py", names)
        self.assertIn("mcp/gxp_editor/compiler_runner.cjs", names)
        self.assertIn("mcp/gxp_editor/rules.json", names)
        self.assertIn("scripts/approval_app.py", names)
        self.assertIn("assets/action-icons/stub.svg", names)
        self.assertNotIn("credential.json", names)
        self.assertNotIn("scripts/benchmark_private.py", names)

    def test_snapshot_survives_source_deletion_without_mixing_version(self):
        package = runtime.prepare_runtime(self.source, self.storage)
        expected = runtime.assert_runtime_resources(package)
        shutil.rmtree(self.source)
        self.assertEqual(runtime.assert_runtime_resources(package), expected)
        self.assertEqual((package / "mcp/gxp_editor/late_dependency.py").read_text(encoding="utf-8"), "VALUE = '冻结字节🙂'\n")
        self.assertEqual(package.name, expected["manifest_sha256"])

    def test_two_versions_have_independent_immutable_packages(self):
        first = runtime.prepare_runtime(self.source, self.storage)
        self.fixture("fixture-2")
        second = runtime.prepare_runtime(self.source, self.storage)
        self.assertNotEqual(first, second)
        self.assertEqual(runtime.assert_runtime_resources(first)["plugin_version"], "fixture-1")
        self.assertEqual(runtime.assert_runtime_resources(second)["plugin_version"], "fixture-2")

    def test_concurrent_preparation_deduplicates_one_complete_package(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: runtime.prepare_runtime(self.source, self.storage), range(4)))
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(list((self.storage / "packages").iterdir())), 1)
        runtime.assert_runtime_resources(results[0])

    def test_missing_or_modified_snapshot_has_precise_code_and_is_never_repaired(self):
        package = runtime.prepare_runtime(self.source, self.storage)
        path = package / "scripts/approval_app.py"
        path.write_text("# different bytes\n")
        with self.assertRaises(runtime.RuntimeResourceError) as caught:
            runtime.prepare_runtime(self.source, self.storage)
        self.assertEqual(caught.exception.code, "plugin_runtime_resource_changed")
        self.assertIn("scripts/approval_app.py", caught.exception.details["changed_files"])
        self.assertTrue(caught.exception.details["reconnect_required"])
        path.unlink()
        with self.assertRaises(runtime.RuntimeResourceError) as caught:
            runtime.assert_runtime_resources(package)
        self.assertEqual(caught.exception.code, "plugin_runtime_path_missing")
        self.assertIn("scripts/approval_app.py", caught.exception.details["missing_files"])

    def test_copy_failure_never_activates_half_package(self):
        original = shutil.copyfile
        count = 0
        def fail_copy(source, target):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("fixture copy failure")
            return original(source, target)
        with patch.object(runtime.shutil, "copyfile", side_effect=fail_copy):
            with self.assertRaises(OSError):
                runtime.prepare_runtime(self.source, self.storage)
        self.assertEqual(list((self.storage / "packages").iterdir()), [])

    def test_prefix_selection_checks_deleted_late_dependency_from_manifest(self):
        package = runtime.prepare_runtime(self.source, self.storage)
        (package / "mcp/gxp_editor/late_dependency.py").unlink()
        with self.assertRaises(runtime.RuntimeResourceError) as caught:
            runtime.assert_runtime_resources(package, ["mcp/gxp_editor/"])
        self.assertIn("mcp/gxp_editor/late_dependency.py", caught.exception.details["missing_files"])

    def test_source_change_while_copying_is_not_accepted(self):
        original = shutil.copyfile
        def changing_copy(source, target):
            result = original(source, target)
            if str(source).endswith("late_dependency.py"):
                Path(source).write_text("VALUE = 'changed'\n")
            return result
        with patch.object(runtime.shutil, "copyfile", side_effect=changing_copy):
            with self.assertRaises(runtime.RuntimeResourceError) as caught:
                runtime.prepare_runtime(self.source, self.storage)
        self.assertEqual(caught.exception.code, "plugin_runtime_resource_changed")
        self.assertEqual(list((self.storage / "packages").iterdir()), [])

    def test_manifest_path_escape_and_unlisted_import_are_rejected(self):
        manifest_path = self.source / runtime.MANIFEST_NAME
        value = json.loads(manifest_path.read_bytes())
        value["files"][0]["path"] = "../escape.py"
        manifest_path.write_text(json.dumps(value))
        with self.assertRaises(runtime.RuntimeResourceError) as caught:
            runtime.prepare_runtime(self.source, self.storage)
        self.assertEqual(caught.exception.code, "plugin_runtime_manifest_invalid")
        runtime.write_manifest(self.source)
        (self.source / "mcp/gxp_editor/injected.py").write_text("# unexpected import")
        with self.assertRaises(runtime.RuntimeResourceError) as caught:
            runtime.prepare_runtime(self.source, self.storage)
        self.assertIn("mcp/gxp_editor/injected.py", caught.exception.details["changed_files"])

    def test_mcp_and_ui_leases_are_independent_and_block_environment_updates(self):
        package = runtime.prepare_runtime(self.source, self.storage)
        ui = runtime.RuntimeLease(package, role="ui", storage_root=self.storage)
        with runtime.RuntimeLease(package, role="mcp", storage_root=self.storage):
            ui.__enter__()
            self.addCleanup(lambda: ui.__exit__(None, None, None))
            self.assertEqual(len(list((self.storage / "leases").glob("*.json"))), 2)
        self.assertTrue(ui.path.exists())
        with self.assertRaises(runtime.RuntimeResourceError) as caught:
            with runtime.guarded_environment_update(Path(sys.executable), self.storage):
                self.fail("must not update an active interpreter")
        self.assertEqual(caught.exception.code, "plugin_runtime_environment_in_use")
        self.assertEqual(caught.exception.details["active_or_unknown_leases"][0]["role"], "ui")
        ui.__exit__(None, None, None)
        with runtime.guarded_environment_update(Path(sys.executable), self.storage):
            pass

    def test_unknown_lease_blocks_but_verified_dead_lease_does_not(self):
        folder = self.storage / "leases"
        folder.mkdir(parents=True)
        path = folder / "unknown.json"
        path.write_text("invalid JSON")
        with self.assertRaises(runtime.RuntimeResourceError):
            with runtime.guarded_environment_update(Path(sys.executable), self.storage):
                self.fail("unknown lease must not allow mutation")
        path.write_text(json.dumps({"pid": os.getpid(), "process_token": "fixture",
                                    "python_identity": runtime.python_identity()}))
        with patch.object(runtime, "_process_identity", return_value=("unknown", None)):
            with self.assertRaises(runtime.RuntimeResourceError):
                with runtime.guarded_environment_update(Path(sys.executable), self.storage):
                    self.fail("unverifiable identity must not allow mutation")
        with patch.object(runtime, "_process_identity", return_value=("dead", None)):
            with runtime.guarded_environment_update(Path(sys.executable), self.storage):
                pass

    def test_environment_mutation_lock_excludes_new_leases(self):
        package = runtime.prepare_runtime(self.source, self.storage)
        waiting = threading.Event()
        acquired = threading.Event()
        def launch():
            waiting.set()
            with runtime.RuntimeLease(package, storage_root=self.storage):
                acquired.set()
        with runtime.guarded_environment_update(Path(sys.executable), self.storage):
            worker = threading.Thread(target=launch)
            worker.start()
            self.assertTrue(waiting.wait(1))
            self.assertFalse(acquired.wait(0.1))
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertTrue(acquired.is_set())

    def test_legacy_and_unverifiable_environment_users_refuse_setup(self):
        for state in ("live", "unknown"):
            with self.subTest(state=state), patch.object(runtime, "_legacy_environment_users",
                    return_value=[{"pid": 123, "process_state": state}]):
                with self.assertRaises(runtime.RuntimeResourceError) as caught:
                    with runtime.guarded_environment_update(Path(sys.executable), self.storage):
                        self.fail("legacy users must not be bypassed")
                self.assertEqual(caught.exception.code, "plugin_runtime_environment_in_use")

    def test_lease_cannot_switch_fixed_runtime_root(self):
        package = runtime.prepare_runtime(self.source, self.storage)
        with patch.dict(os.environ, {"GXP_EDITOR_RUNTIME_ROOT": str(self.source)}):
            with self.assertRaises(runtime.RuntimeResourceError) as caught:
                with runtime.RuntimeLease(package, storage_root=self.storage):
                    self.fail("a process must not silently switch runtime versions")
        self.assertEqual(caught.exception.code, "plugin_runtime_resource_changed")

    def test_interpreter_identity_drift_and_unapproved_sibling_rejected(self):
        package = runtime.prepare_runtime(self.source, self.storage)
        expected = runtime.python_identity()
        expected["executable_sha256"] = "0" * 64
        expected["sibling_interpreters"] = []
        with patch.dict(os.environ, {"GXP_EDITOR_PYTHON_IDENTITY": json.dumps(expected)}):
            with self.assertRaises(runtime.RuntimeResourceError) as caught:
                with runtime.RuntimeLease(package, storage_root=self.storage):
                    self.fail("changed interpreter must not acquire a lease")
        self.assertEqual(caught.exception.code, "plugin_runtime_interpreter_changed")
        trusted = {"path": "python.exe", "executable_sha256": "a", "version": "v", "prefix": "env",
                   "venv_config_sha256": "cfg", "sibling_interpreters": [{"path": "pythonw.exe", "sha256": "b"}]}
        actual = {**trusted, "path": "pythonw.exe", "executable_sha256": "b"}
        self.assertTrue(runtime._same_python_environment(actual, trusted))
        self.assertFalse(runtime._same_python_environment({**actual, "executable_sha256": "tamper"}, trusted))

    def test_node_binary_changes_during_probe_and_before_execution_are_rejected(self):
        from gxp_editor.compiler import CompilerBridge
        from tests.helpers import editor_config
        binary = self.base / "fixture-node.exe"
        binary.write_bytes(b"fixture node version one")
        completed = subprocess.CompletedProcess([str(binary), "--version"], 0, "v22.0.0\n", "")
        bridge = CompilerBridge(editor_config())
        with patch("gxp_editor.compiler.shutil.which", return_value=str(binary)), \
                patch("gxp_editor.compiler.subprocess.run", return_value=completed):
            identity = bridge._node_identity()
        self.assertEqual(identity[0], str(binary.resolve()))
        binary.write_bytes(b"fixture node version two")
        with self.assertRaises(runtime.RuntimeResourceError) as caught:
            bridge._verify_node_binary(identity[0])
        self.assertEqual(caught.exception.code, "plugin_runtime_interpreter_changed")
        def mutation(*args, **kwargs):
            binary.write_bytes(b"fixture node version three")
            return completed
        bridge = CompilerBridge(editor_config())
        with patch("gxp_editor.compiler.shutil.which", return_value=str(binary)), \
                patch("gxp_editor.compiler.subprocess.run", side_effect=mutation):
            with self.assertRaises(runtime.RuntimeResourceError):
                bridge._node_identity()

    def test_python_prelaunch_requires_frozen_bytes_and_an_approved_path(self):
        expected = runtime.python_identity()
        actual = runtime.assert_python_interpreter(Path(sys.executable), expected)
        self.assertEqual(actual["sha256"], expected["executable_sha256"])
        with self.assertRaises(runtime.RuntimeResourceError) as caught:
            runtime.assert_python_interpreter(Path(sys.executable),
                {**expected, "executable_sha256": "0" * 64, "sibling_interpreters": []})
        self.assertEqual(caught.exception.code, "plugin_runtime_interpreter_changed")
        unapproved = self.base / "unapproved-python.exe"
        shutil.copyfile(sys.executable, unapproved)
        with self.assertRaises(runtime.RuntimeResourceError):
            runtime.assert_python_interpreter(unapproved, expected)

    def test_actual_bootstrap_late_import_survives_host_source_removal(self):
        ready, proceed, result = (self.base / name for name in ("ready", "proceed", "result"))
        server = """import os, time, pathlib, json
ready = pathlib.Path(os.environ['FIXTURE_READY'])
ready.write_text(os.environ['GXP_EDITOR_RUNTIME_ROOT'])
deadline = time.monotonic() + 8
while not pathlib.Path(os.environ['FIXTURE_PROCEED']).exists():
    if time.monotonic() >= deadline: raise SystemExit(3)
    time.sleep(.02)
from gxp_editor.late_dependency import VALUE
pathlib.Path(os.environ['FIXTURE_RESULT']).write_text(json.dumps({'value': VALUE, 'root': os.environ['GXP_EDITOR_RUNTIME_ROOT']}), encoding='utf-8')
"""
        (self.source / "mcp/server.py").write_text(server, encoding="utf-8")
        runtime.write_manifest(self.source)
        environment = {**os.environ, "LOCALAPPDATA": str(self.base / "local"),
                       "FIXTURE_READY": str(ready), "FIXTURE_PROCEED": str(proceed), "FIXTURE_RESULT": str(result)}
        process = subprocess.Popen([sys.executable, str(self.source / "mcp/gxp_editor/runtime_package.py"),
                                    "start", "--source", str(self.source)], env=environment,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.025)
            self.assertTrue(ready.exists(), "fixture server did not become ready")
            package = Path(ready.read_text())
            self.assertNotEqual(package, self.source)
            shutil.rmtree(self.source)
            proceed.touch()
            out, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, err.decode("utf-8", "replace"))
            self.assertEqual(json.loads(result.read_text(encoding="utf-8"))["value"], "冻结字节🙂")
            self.assertEqual(list((self.base / "local/GxpLowcodeEditor/runtime/leases").glob("*.json")), [])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)


if __name__ == "__main__":
    unittest.main()
