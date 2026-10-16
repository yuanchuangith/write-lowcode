from __future__ import annotations

import subprocess
import json
import os
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tests.helpers import editor_config
from gxp_editor.config import RuntimeReferenceConfig
from gxp_editor.runtime_validation import RuntimeCsharpValidator, RuntimeValidationError
from gxp_editor.hashing import sha256_text


class RuntimeValidationTests(unittest.TestCase):
    def setUp(self):
        runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_directory.cleanup)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": runtime_directory.name})
        environment.start()
        self.addCleanup(environment.stop)

    def snapshot(self, root: Path, mode: str = "deployment_snapshot") -> RuntimeCsharpValidator:
        for name in RuntimeCsharpValidator.REQUIRED_ASSEMBLIES:
            (root / name).write_bytes((name + "\n").encode("utf-8"))
        config = replace(
            editor_config(),
            runtime_references=RuntimeReferenceConfig(
                directory=str(root),
                source_mode=mode,
                target_framework="net6.0",
            ),
        )
        return RuntimeCsharpValidator(config)

    def fake_dotnet(self, root: Path, expected_source: str, *, fail: bool = False) -> Path:
        """A protocol fixture: checks source bytes, without pretending to compile C#."""
        script = root / "fake_dotnet.py"
        expected = root / "expected_source.json"
        expected.write_text(json.dumps(expected_source, ensure_ascii=False), encoding="utf-8")
        script.write_text(
            "import json,sys\nfrom pathlib import Path\n"
            f"expected = json.loads(Path({str(expected)!r}).read_text(encoding='utf-8'))\n"
            "actual = Path(sys.argv[2]).with_name('Candidate.cs').read_text(encoding='utf-8')\n"
            "if actual != expected: raise SystemExit('source text changed in transit')\n"
            + ("sys.stdout.buffer.write('error CS9999: 中文编译错误\\n'.encode('utf-8'))\nraise SystemExit(1)\n" if fail else "raise SystemExit(0)\n"),
            encoding="utf-8",
        )
        command = root / ("fake-dotnet.cmd" if os.name == "nt" else "fake-dotnet")
        if os.name == "nt":
            command.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        else:
            import shlex
            command.write_text(
                "#!/bin/sh\nexec " + shlex.quote(sys.executable) + " " + shlex.quote(str(script)) + ' "$@"\n',
                encoding="utf-8",
            )
            command.chmod(0o700)
        return command

    def test_real_probe_preserves_chinese_quotes_and_silent_gbk_corruption_cases(self):
        # UTF-8 decoded as GBK can swallow JSON's escaped quote after 丁, or silently
        # change 功能. Exercise the actual child protocol under the Windows code page.
        for source in ('class A { string Name = "丁"; }', '// 功能\nclass A {}'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                validator = self.snapshot(root)
                command = self.fake_dotnet(root, source)
                validator.runtime = replace(validator.runtime, dotnet_command=str(command))
                with patch.dict(os.environ, {"PYTHONUTF8": "0", "PYTHONIOENCODING": "gbk"}):
                    result = validator.validate("fixture", source)
                self.assertEqual(result["csharp_sha256"], sha256_text(source))
                self.assertTrue(result["compilation_succeeded"])
                self.assertEqual(result["validation_scope"], "local_reference_compile")

    def test_real_probe_preserves_compilation_failure_classification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)
            command = self.fake_dotnet(root, "class A {}", fail=True)
            validator.runtime = replace(validator.runtime, dotnet_command=str(command))
            with self.assertRaises(RuntimeValidationError) as caught:
                validator.validate("fixture", "class A {}")
        self.assertEqual(caught.exception.code, "compilation_failed")
        self.assertTrue(caught.exception.diagnostics)

    def test_probe_source_hash_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            with patch.object(validator, "_probe", return_value={"success": True, "csharp_sha256": "0" * 64}):
                with self.assertRaises(RuntimeValidationError) as caught:
                    validator.validate("fixture", "class A {}")
        self.assertEqual(caught.exception.code, "probe_protocol_error")

    def test_outer_deadlines_allow_inner_commands_to_finish(self):
        self.assertGreater(RuntimeCsharpValidator.STATUS_TIMEOUT_SECONDS, RuntimeCsharpValidator.DOTNET_STATUS_TIMEOUT_SECONDS)
        self.assertGreater(RuntimeCsharpValidator.VALIDATION_TIMEOUT_SECONDS, RuntimeCsharpValidator.BUILD_TIMEOUT_SECONDS)

    def test_compiler_timeout_is_not_reported_as_compile_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            with patch("gxp_editor.runtime_validation.subprocess.run", side_effect=subprocess.TimeoutExpired("dotnet", 55)):
                with self.assertRaises(RuntimeValidationError) as caught:
                    validator._validate("fixture", "class A {}")
        self.assertEqual(caught.exception.code, "checker_timeout")

    def test_candidate_build_uses_explicit_offline_nuget_configuration(self):
        from xml.etree import ElementTree

        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))

            def inspect_build(command, **kwargs):
                config_argument = next(value for value in command if value.startswith("-p:RestoreConfigFile="))
                config_path = Path(config_argument.split("=", 1)[1])
                configuration = ElementTree.parse(config_path).getroot()
                self.assertIsNotNone(configuration.find("packageSources/clear"))
                self.assertEqual(len(configuration.findall("packageSources/add")), 0)
                self.assertIsNotNone(configuration.find("fallbackPackageFolders/clear"))
                self.assertIsNotNone(configuration.find("config/add[@key='globalPackagesFolder']"))
                project = ElementTree.parse(command[2]).getroot()
                self.assertEqual(project.findtext("PropertyGroup/NuGetAudit"), "false")
                self.assertIn("-p:NuGetAudit=false", command)
                return subprocess.CompletedProcess(command, 0, "Build succeeded", "")

            with patch("gxp_editor.runtime_validation.subprocess.run", side_effect=inspect_build):
                self.assertTrue(validator._validate("fixture", "class A {}")["success"])

    def test_build_failure_distinguishes_code_errors_from_checker_failures(self):
        cases = [
            ("Candidate.cs(1,1): error CS1002: ; expected", "compilation_failed"),
            ("error NU1101: Unable to find package Microsoft.NETCore.App.Ref", "checker_unavailable"),
            ("error NETSDK1045: The current SDK does not support targeting", "checker_unavailable"),
            ("error MSB4018: The RestoreTask task failed unexpectedly", "checker_unavailable"),
            ("NuGet.targets(778,5): error : Value cannot be null. (Parameter 'path1')", "checker_unavailable"),
            ("error CS1002: ; expected\nerror NU1101: Missing reference pack", "checker_unavailable"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            for output, expected_code in cases:
                with self.subTest(output=output):
                    process = subprocess.CompletedProcess(["dotnet", "build"], 1, output, "tool stderr")
                    with patch("gxp_editor.runtime_validation.subprocess.run", return_value=process):
                        with self.assertRaises(RuntimeValidationError) as caught:
                            validator._validate("fixture", "class A {}")
                    self.assertEqual(caught.exception.code, expected_code)
                    self.assertEqual(caught.exception.diagnostics, (output + "\ntool stderr").splitlines())

    def test_compiler_error_before_output_excerpt_is_preserved(self):
        output = "Candidate.cs(1,1): error CS1002: ; expected\n" + "build detail\n" * 100
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            process = subprocess.CompletedProcess(["dotnet", "build"], 1, output, "")
            with patch("gxp_editor.runtime_validation.subprocess.run", return_value=process):
                with self.assertRaises(RuntimeValidationError) as caught:
                    validator._validate("fixture", "class A {}")
        self.assertEqual(caught.exception.code, "compilation_failed")
        self.assertEqual(caught.exception.diagnostics[0], "Candidate.cs(1,1): error CS1002: ; expected")
        self.assertGreater(len(caught.exception.diagnostics), 80)

    def test_reference_snapshot_drift_during_build_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)

            def mutate_reference(command, **kwargs):
                (root / "GxP2.Model.dll").write_bytes(b"changed during compilation")
                return subprocess.CompletedProcess(command, 0, "Build succeeded", "")

            with patch("gxp_editor.runtime_validation.subprocess.run", side_effect=mutate_reference):
                with self.assertRaises(RuntimeValidationError) as caught:
                    validator._validate("fixture", "class A {}")
        self.assertEqual(caught.exception.code, "reference_drift")

    def test_successful_build_with_reference_conflict_is_not_production_compatible(self):
        # Use a deployment snapshot to ensure the conflict overrides source-mode claims.
        output = (
            "warning MSB3277: Found conflicts between System.Security.Permissions, Version=5.0.0.0 and Version=8.0.0.0\n"
            + "build detail\n" * 90
            + "Build succeeded"
        )
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary), mode="deployment_snapshot")
            process = subprocess.CompletedProcess(["dotnet", "build"], 0, output, "")
            with patch("gxp_editor.runtime_validation.subprocess.run", return_value=process):
                result = validator._validate("fixture", "class A {}")
        self.assertTrue(result["success"])
        self.assertTrue(result["compilation_succeeded"])
        self.assertTrue(result["reference_conflicts"])
        self.assertFalse(result["production_compatible"])

    def test_development_build_is_never_production_compatible(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary), mode="development_build")
            completed = subprocess.CompletedProcess(["dotnet", "--version"], 0, "6.0.428\n", "")
            with patch("gxp_editor.runtime_validation.subprocess.run", return_value=completed):
                status = validator._status()
        self.assertTrue(status["available"])
        self.assertFalse(status["production_compatible"])
        self.assertTrue(status["zero_backend_mode"])

    def test_missing_required_assembly_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)
            (root / "GxP2.Services.dll").unlink()
            with self.assertRaisesRegex(RuntimeValidationError, "missing required assemblies"):
                validator._status()

    def test_manifest_changes_when_reference_content_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)
            completed = subprocess.CompletedProcess(["dotnet", "--version"], 0, "6.0.428\n", "")
            with patch("gxp_editor.runtime_validation.subprocess.run", return_value=completed):
                before = validator._status()["reference_manifest_sha256"]
                (root / "GxP2.Model.dll").write_bytes(b"changed")
                after = validator._status()["reference_manifest_sha256"]
        self.assertNotEqual(before, after)

    def test_runtime_probe_returns_real_manifest(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            validator.runtime = replace(validator.runtime, dotnet_command=sys.executable)
            result = validator.status()
        self.assertEqual(result["reference_count"], 4)
        self.assertEqual(len(result["reference_manifest_sha256"]), 64)

    def test_stalled_runtime_probe_is_terminated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)
            sleeper = root / ("slow-dotnet.cmd" if os.name == "nt" else "slow-dotnet")
            sleeper.write_text(
                "@ping 127.0.0.1 -n 20 >nul\n" if os.name == "nt" else "#!/bin/sh\nsleep 20\n",
                encoding="utf-8",
            )
            if os.name != "nt":
                sleeper.chmod(0o700)
            validator.runtime = replace(validator.runtime, dotnet_command=str(sleeper))
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeValidationError, "exceeded 1 seconds"):
                validator._probe("status", timeout=1)
            self.assertLess(time.monotonic() - started, 8)


if __name__ == "__main__":
    unittest.main()
