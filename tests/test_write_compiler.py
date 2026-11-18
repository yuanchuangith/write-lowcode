import importlib
import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from gxp_editor.compiler import CompilerBridge, CompilerError
from gxp_editor.config import DEFAULT_COMPILER_BRIDGE, LEGACY_COMPILER_BRIDGE, managed_compiler_path
from gxp_editor.runtime_validation import RuntimeCsharpValidator, RuntimeValidationError
from tests.helpers import editor_config


class WriteCompilerLocationTests(unittest.TestCase):
    def test_existing_default_moves_to_managed_on_the_next_session(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"LOCALAPPDATA": directory}):
            frontend = Path(directory).resolve() / "frontend"
            config = replace(editor_config(), frontend_repo=str(frontend), compiler_bridge=LEGACY_COMPILER_BRIDGE)
            legacy = CompilerBridge(config)
            self.assertEqual(legacy.path, frontend / LEGACY_COMPILER_BRIDGE)

            bundle = managed_compiler_path(frontend)
            bundle.parent.mkdir(parents=True)
            bundle.touch()

            self.assertEqual(legacy.path, frontend / LEGACY_COMPILER_BRIDGE)
            self.assertEqual(CompilerBridge(config).path, bundle)
            explicit = replace(config, compiler_bridge=str(frontend / LEGACY_COMPILER_BRIDGE))
            self.assertEqual(CompilerBridge(explicit).path, legacy.path)
            other = replace(config, frontend_repo=str(frontend.with_name("other")))
            self.assertEqual(CompilerBridge(other).path, Path(other.frontend_repo) / LEGACY_COMPILER_BRIDGE)

    def test_explicit_external_bridge_must_be_the_managed_bundle(self):
        with TemporaryDirectory() as directory, patch.dict(os.environ, {"LOCALAPPDATA": directory}):
            frontend = Path(directory).resolve() / "frontend"
            config = replace(editor_config(), frontend_repo=str(frontend),
                             compiler_bridge=str(frontend.with_name("outside.js")))
            with self.assertRaisesRegex(CompilerError, "frontend repository or the Write compiler cache"):
                CompilerBridge(config)._bridge_identity()


class WriteCompilerCheckTests(unittest.TestCase):
    def setUp(self):
        scratch = self.enterContext(TemporaryDirectory())
        self.root = Path(scratch)
        self.enterContext(patch.dict(os.environ, {"LOCALAPPDATA": scratch}))
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        with patch.object(sys, "path", [str(scripts), *sys.path]):
            self.cli = importlib.import_module("gxp_compiler")
        self.canvas = self.root / "canvas.json"

    def write_canvas(self, language):
        self.canvas.write_text(json.dumps({
            "code": "CheckProbe", "globalVariables": [], "actionData": [
                {"key": "main", "title": "Main", "actionType": language, "data": []},
            ],
        }), encoding="utf-8")

    def run_check(self, *options):
        with io.TextIOWrapper(io.BytesIO(), encoding="utf-8") as stdout, \
                io.TextIOWrapper(io.BytesIO(), encoding="utf-8") as stderr, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            status = self.cli.main(["check", str(self.canvas), *options])
            return status, json.loads(stdout.buffer.getvalue()), stderr.buffer.getvalue().decode("utf-8")

    def test_js_only_check_skips_generation_and_dotnet_even_without_references(self):
        self.write_canvas("js")
        with patch.object(CompilerBridge, "validate_design", return_value={"validation": {"valid": True}}) as validate, \
                patch.object(CompilerBridge, "compile_design", side_effect=AssertionError("generation not needed")), \
                patch.object(RuntimeCsharpValidator, "validate", side_effect=AssertionError(".NET not needed")):
            status, result, _ = self.run_check("--references", str(self.root / "missing"))
        validate.assert_called_once()
        self.assertEqual(status, 0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["validation"]["valid"])
        self.assertEqual(result["csharp"]["status"], "skipped")

    def test_check_reports_compiler_diagnostics_and_emits_exact_source_on_failure(self):
        self.write_canvas("csharp")
        source = "public class Broken\n{\n    public int Value => ;\n}\n"
        emitted = self.root / "Generated.cs"
        diagnostic = r"C:\tmp\Candidate.cs(3,25): error CS1525: Invalid expression term ';' [C:\tmp\Candidate.csproj]"
        warning = r"C:\sdk\Microsoft.Common.targets(50,5): warning MSB3277: Conflicting references"
        with patch.object(CompilerBridge, "compile_design", return_value={
            "validation": {"valid": True}, "csharp_code": source,
        }), patch.object(RuntimeCsharpValidator, "validate", side_effect=RuntimeValidationError(
            "C# build failed", code="compilation_failed", diagnostics=[warning, diagnostic, diagnostic],
        )) as validate:
            status, result, _ = self.run_check("--references", str(self.root), "--emit-csharp", str(emitted))
        validate.assert_called_once_with("CheckProbe", source)
        self.assertEqual(status, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["csharp"]["status"], "failed")
        error, dependency_warning = result["csharp"]["diagnostics"]
        self.assertEqual((error["severity"], error["code"], error["line"], error["column"]), ("error", "CS1525", 3, 25))
        self.assertEqual(error["message"], "Invalid expression term ';'")
        self.assertEqual(dependency_warning["code"], "MSB3277")
        self.assertEqual(Path(error["file"]), emitted.resolve())
        self.assertEqual(emitted.read_bytes(), source.encode("utf-8"))

    def test_check_uses_write_runtime_settings_without_database_configuration(self):
        self.write_canvas("csharp")
        config_file = self.root / "write.json"
        config_file.write_text(json.dumps({
            "runtime_references": {"directory": str(self.root), "target_framework": "net6.0"},
        }), encoding="utf-8")
        def validate(validator, _code, _source):
            self.assertEqual(validator.runtime.path, self.root.resolve())
            self.assertEqual(validator.runtime.target_framework, "net6.0")
            return {"compilation_succeeded": True, "diagnostics": []}

        with patch.dict(os.environ, {"GXP_LOWCODE_EDITOR_CONFIG": str(config_file)}), \
                patch("keyring.get_password", side_effect=AssertionError("credentials not needed")), \
                patch.object(CompilerBridge, "compile_design", return_value={
                    "validation": {"valid": True}, "csharp_code": "public class Valid {}",
                }), patch.object(RuntimeCsharpValidator, "validate", new=validate):
            status, result, _ = self.run_check()
        self.assertEqual(status, 0)
        self.assertEqual(result["csharp"]["status"], "passed")

    def test_checker_unavailable_is_distinct_from_csharp_errors(self):
        self.write_canvas("csharp")
        with patch.object(CompilerBridge, "compile_design", return_value={
            "validation": {"valid": True}, "csharp_code": "public class Valid {}",
        }), patch.object(RuntimeCsharpValidator, "validate", side_effect=RuntimeValidationError(".NET SDK not found")):
            status, result, _ = self.run_check("--references", str(self.root))
        self.assertEqual(status, 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["csharp"]["status"], "unavailable")
        self.assertEqual(result["csharp"]["diagnostics"], [])

    @unittest.skipUnless(os.environ.get("GXP_RUN_DOTNET_SMOKE") == "1", "opt-in synthetic local SDK smoke")
    def test_check_runs_real_dotnet_and_reuses_restored_workspace(self):
        self.write_canvas("csharp")
        valid = "public class CheckProbe { public int Value => 1; }"
        invalid = "public class CheckProbe\n{\n    public int Value => ;\n}"
        with patch.object(CompilerBridge, "compile_design", side_effect=[
            {"validation": {"valid": True}, "csharp_code": valid},
            {"validation": {"valid": True}, "csharp_code": invalid},
        ]), patch.object(RuntimeCsharpValidator, "validate", new=RuntimeCsharpValidator._validate), \
                patch.object(RuntimeCsharpValidator, "_references", return_value=[]):
            options = ("--references", str(self.root), "--timings")
            passed, first, _ = self.run_check(*options)
            failed, second, _ = self.run_check(*options)
        self.assertEqual((passed, failed), (0, 1))
        self.assertEqual((first["csharp"]["status"], second["csharp"]["status"]), ("passed", "failed"))
        self.assertTrue(any(error["code"] == "CS1525" and error["line"] == 3 for error in second["csharp"]["diagnostics"]))
        self.assertIn("runtime.workspace_restore", [span["stage"] for span in first["timings"]])
        self.assertNotIn("runtime.workspace_restore", [span["stage"] for span in second["timings"]])


@unittest.skipUnless(os.environ.get("GXP_TEST_WRITE_COMPILER_FRONTEND"), "Build the Write compiler first")
class WriteCompilerIntegrationTests(unittest.TestCase):
    def test_cli_compiles_a_utf8_file_and_reports_invalid_input(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            canvas, output = path / "canvas.json", path / "compiled.json"
            canvas.write_text(json.dumps({"globalVariables": [], "actionData": [
                {"key": "main", "title": "\u6d4b\u8bd5", "actionType": "js", "data": []},
            ]}, ensure_ascii=False), encoding="utf-8-sig")
            cli = [sys.executable, str(Path(__file__).resolve().parents[1] / "scripts" / "gxp_compiler.py"),
                   "--frontend", os.environ["GXP_TEST_WRITE_COMPILER_FRONTEND"]]
            run = subprocess.run([*cli, "compile", str(canvas), "-o", str(output)],
                                 capture_output=True, encoding="utf-8", timeout=45)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stdout, "")
            self.assertIn("//\u6d4b\u8bd5", json.loads(output.read_text(encoding="utf-8"))["js_code"])
            canvas.write_text("{}", encoding="utf-8")
            run = subprocess.run([*cli, "validate", str(canvas)], capture_output=True, encoding="utf-8", timeout=15)
            self.assertEqual(run.returncode, 1)
            self.assertIn("actionData", json.loads(run.stderr)["error"])

    def test_native_patch_generates_both_languages_from_the_managed_bundle(self):
        frontend = os.environ["GXP_TEST_WRITE_COMPILER_FRONTEND"]
        bridge = CompilerBridge(replace(editor_config(), frontend_repo=frontend, compiler_bridge=DEFAULT_COMPILER_BRIDGE))
        self.assertEqual(bridge.path, managed_compiler_path(frontend))
        design = {"code": "WriteCompilerProbe", "globalVariables": [], "actionData": [
            {"key": "frontend", "title": "Frontend", "actionType": "js", "data": []},
            {"key": "backend", "title": "Backend", "actionType": "csharp", "data": []},
        ]}
        result = bridge.compile_patch(design, [{
            "operation": "append_node", "action_group_key": "frontend", "element_key": "SetVariable",
            "input_params": {"variableType": "string",
                             "variableValue": {"paramTypes": "custom", "value": '"probe"', "code": '"probe"'}},
            "output_params": {"variableName": {"paramTypes": "custom", "value": "result", "code": "result"}},
        }])
        self.assertIn('var result = "probe";', result["js_code"])
        self.assertIn("namespace WriteCompilerProbe", result["csharp_code"])
        self.assertTrue(bridge.verify_compiled_result(result)["generated_code_consistency_verified"])
        self.assertEqual(bridge.verify_binding(result["compiler_binding"]), result["compiler_binding"])
