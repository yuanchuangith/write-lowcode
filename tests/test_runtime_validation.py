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
from gxp_editor.config import runtime_root

ORIGINAL_SDK_IDENTITY = RuntimeCsharpValidator._sdk_identity


class RuntimeValidationTests(unittest.TestCase):
    def test_empty_source_skips_probe_dotnet_and_assembly_access(self):
        validator = RuntimeCsharpValidator(editor_config())
        with patch.object(validator, "_probe", side_effect=AssertionError("must not start probe")), \
             patch.object(validator, "_status", side_effect=AssertionError("must not inspect SDK")), \
             patch.object(validator, "_references", side_effect=AssertionError("must not read DLLs")):
            for source in ("", " \n\t"):
                for validate in (validator.validate, validator._validate):
                    evidence = validate("synthetic", source)
                    self.assertTrue(evidence["success"])
                    self.assertFalse(evidence["compilation_succeeded"])
                    self.assertFalse(evidence["production_compatible"])
                    self.assertEqual(evidence["validation_scope"], "no_csharp_source")
                    self.assertEqual(evidence["csharp_sha256"], sha256_text(source))

    def setUp(self):
        runtime_directory = tempfile.TemporaryDirectory()
        self.addCleanup(runtime_directory.cleanup)
        environment = patch.dict(os.environ, {"LOCALAPPDATA": runtime_directory.name})
        environment.start()
        self.addCleanup(environment.stop)
        sdk = patch.object(RuntimeCsharpValidator, "_sdk_identity", return_value={
            "dotnet_path": sys.executable, "sdk_version": "6.0.428", "sdk_path": str(Path(runtime_directory.name) / "sdk"),
        })
        sdk.start()
        self.addCleanup(sdk.stop)

    def build_protocol(self, build):
        def run(command, cwd, timeout, phase):
            self.assertIn("-p:ImportDirectoryBuildProps=false", command)
            self.assertIn("-p:ImportDirectoryBuildTargets=false", command)
            if command[1] == "restore":
                obj = Path(cwd) / "obj"
                obj.mkdir(exist_ok=True)
                (obj / "project.assets.json").write_text('{}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "Restored", "")
            self.assertEqual(command[1], "build")
            self.assertIn("--no-restore", command)
            self.assertIn("--no-incremental", command)
            return build(command, cwd=cwd) if callable(build) else build
        return run

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

    def fake_dotnet(self, root: Path, expected_source: str, *, fail: bool = False, stall_build: bool = False) -> Path:
        """A protocol fixture: checks source bytes, without pretending to compile C#."""
        script = root / "fake_dotnet.py"
        expected = root / "expected_source.json"
        expected.write_text(json.dumps(expected_source, ensure_ascii=False), encoding="utf-8")
        script.write_text(
            "import json,sys\nfrom pathlib import Path\n"
            f"expected = json.loads(Path({str(expected)!r}).read_text(encoding='utf-8'))\n"
            "if sys.argv[1] == '--version': print('6.0.428'); raise SystemExit(0)\n"
            f"if sys.argv[1] == '--list-sdks': Path({str(root / '6.0.428')!r}).mkdir(exist_ok=True); print({'6.0.428 [' + str(root) + ']'!r}); raise SystemExit(0)\n"
            "if sys.argv[1] == 'restore':\n"
            " root = Path(sys.argv[2]).parent\n (root / 'obj').mkdir(exist_ok=True)\n"
            " (root / 'obj' / 'project.assets.json').write_text('{}', encoding='utf-8')\n raise SystemExit(0)\n"
            "if '--no-restore' not in sys.argv or '--no-incremental' not in sys.argv: raise SystemExit('missing fresh compile flags')\n"
            "actual = Path(sys.argv[2]).with_name('Candidate.cs').read_text(encoding='utf-8')\n"
            "if actual != expected: raise SystemExit('source text changed in transit')\n"
            + ("import time; time.sleep(20)\n" if stall_build else "")
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
            with patch.object(validator, "_dotnet", side_effect=RuntimeValidationError("compile timed out", code="checker_timeout")):
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

            with patch.object(validator, "_dotnet", side_effect=self.build_protocol(inspect_build)):
                self.assertTrue(validator._validate("fixture", "class A {}")["success"])

    def test_build_failure_distinguishes_code_errors_from_checker_failures(self):
        cases = [
            ("Candidate.cs(1,1): error CS1002: ; expected", "compilation_failed"),
            ("error NU1101: Unable to find package Microsoft.NETCore.App.Ref", "checker_unavailable"),
            ("error NETSDK1045: The current SDK does not support targeting", "checker_unavailable"),
            ("error MSB4018: The RestoreTask task failed unexpectedly", "checker_unavailable"),
            ("NuGet.targets(778,5): error : Value cannot be null. (Parameter 'path1')", "checker_unavailable"),
            ("error CS1002: ; expected\nerror NU1101: Missing reference pack", "checker_unavailable"),
            ("error CS1002: ; expected\nNuGet.targets: error : unnumbered failure", "checker_unavailable"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            for output, expected_code in cases:
                with self.subTest(output=output):
                    process = subprocess.CompletedProcess(["dotnet", "build"], 1, output, "tool stderr")
                    with patch.object(validator, "_dotnet", side_effect=self.build_protocol(process)):
                        with self.assertRaises(RuntimeValidationError) as caught:
                            validator._validate("fixture", "class A {}")
                    self.assertEqual(caught.exception.code, expected_code)
                    self.assertEqual(caught.exception.diagnostics, (output + "\ntool stderr").splitlines())

    def test_compiler_error_before_output_excerpt_is_preserved(self):
        output = "Candidate.cs(1,1): error CS1002: ; expected\n" + "build detail\n" * 100
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            process = subprocess.CompletedProcess(["dotnet", "build"], 1, output, "")
            with patch.object(validator, "_dotnet", side_effect=self.build_protocol(process)):
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

            with patch.object(validator, "_dotnet", side_effect=self.build_protocol(mutate_reference)):
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
            with patch.object(validator, "_dotnet", side_effect=self.build_protocol(process)):
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

    def test_repeated_candidate_always_compiles_but_restores_only_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            completed = subprocess.CompletedProcess(["dotnet", "build"], 0, "Build succeeded", "")
            calls = []
            protocol = self.build_protocol(completed)

            def command(*args, **kwargs):
                calls.append(args[0])
                return protocol(*args, **kwargs)

            with patch.object(validator, "_dotnet", side_effect=command):
                first = validator._validate("first", "class A {}")
                second = validator._validate("second", "class A {}")
            self.assertEqual(first, second)
            self.assertEqual([call[1] for call in calls], ["restore", "build", "build"])
            self.assertEqual(calls[0][2], calls[2][2])
            self.assertTrue(next(span for span in validator.last_performance if span["stage"] == "runtime.workspace")["cache_hit"])
            self.assertNotIn("performance", second)
            self.assertFalse(list((runtime_root() / "validation").rglob("Candidate.cs")))

    def test_source_error_clears_candidate_but_reuses_only_restore_on_correction(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            def fail_with_outputs(command, **kwargs):
                root = Path(kwargs["cwd"])
                (root / "bin").mkdir()
                (root / "bin" / "candidate.dll").write_bytes(b"failed output")
                (root / "obj" / "Debug").mkdir()
                (root / "obj" / "Debug" / "candidate.pdb").write_bytes(b"failed debug output")
                return subprocess.CompletedProcess(command, 1, "Candidate.cs(1,1): error CS1002: ; expected", "")
            with patch.object(validator, "_dotnet", side_effect=self.build_protocol(fail_with_outputs)) as commands:
                with self.assertRaises(RuntimeValidationError):
                    validator._validate("first", "invalid source")
            self.assertEqual(commands.call_count, 2)
            root = runtime_root() / "validation"
            self.assertFalse(list(root.rglob("Candidate.cs")))
            state = json.loads(next(root.rglob("state.json")).read_text())
            self.assertEqual(state["status"], "restore_ready")
            self.assertFalse(list(root.rglob("bin")))
            self.assertFalse(list(root.rglob("Debug")))
            self.assertTrue(any(span["stage"] == "runtime.references_verify" for span in validator.last_performance))
            success = subprocess.CompletedProcess(["dotnet", "build"], 0, "Build succeeded", "")
            with patch.object(validator, "_dotnet", side_effect=self.build_protocol(success)) as commands:
                self.assertTrue(validator._validate("first", "class A {}" )["success"])
            self.assertEqual([call.args[0][1] for call in commands.call_args_list], ["build"])

    def test_non_source_and_environment_failures_cannot_preserve_restore(self):
        failures = [
            "CSC : error CS0006: Metadata file missing",
            "Candidate.cs(1,1): error CS1002: ; expected\nerror NU1101: Missing pack",
            "Candidate.cs(1,1): error CS1002: ; expected\nerror MSB4018: Task failed",
            "Candidate.cs(1,1): error CS1002: ; expected\nerror NETSDK1045: Unsupported SDK",
            "Candidate.cs(1,1): error CS1002: ; expected\nNuGet.targets: error : null path",
        ]
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            for output in failures:
                with self.subTest(output=output):
                    failure = subprocess.CompletedProcess(["dotnet", "build"], 1, output, "")
                    with patch.object(validator, "_dotnet", side_effect=self.build_protocol(failure)) as commands:
                        with self.assertRaises(RuntimeValidationError):
                            validator._validate("fixture", "invalid source")
                    self.assertEqual([call.args[0][1] for call in commands.call_args_list], ["restore", "build"])
                    state = json.loads(next((runtime_root() / "validation").rglob("state.json")).read_text())
                    self.assertEqual(state["status"], "dirty")

    def test_source_error_with_restore_metadata_or_reference_drift_stays_dirty(self):
        for drift in ("restore", "reference"):
            with self.subTest(drift=drift), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                validator = self.snapshot(root)
                def fail_with_drift(command, **kwargs):
                    target = (Path(kwargs["cwd"]) / "obj" / "project.assets.json" if drift == "restore"
                              else root / "GxP2.Model.dll")
                    target.write_text("changed", encoding="utf-8")
                    return subprocess.CompletedProcess(command, 1, "Candidate.cs(1,1): error CS1002: ; expected", "")
                with patch.object(validator, "_dotnet", side_effect=self.build_protocol(fail_with_drift)):
                    with self.assertRaises(RuntimeValidationError) as caught:
                        validator._validate("fixture", "invalid source")
                self.assertNotEqual(caught.exception.code, "compilation_failed")
                state_path = next((runtime_root() / "validation").rglob("state.json"))
                self.assertEqual(json.loads(state_path.read_text())["status"], "dirty")
                self.assertFalse(list(state_path.parent.rglob("Candidate.cs")))
                # Remove this subcase's pool so the next subcase inspects its own slot.
                import shutil
                shutil.rmtree(runtime_root() / "validation")

    def test_reference_content_and_sdk_version_invalidate_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)
            success = subprocess.CompletedProcess(["dotnet", "build"], 0, "Build succeeded", "")
            with patch.object(validator, "_dotnet", side_effect=self.build_protocol(success)) as commands:
                validator._validate("fixture", "class A {}")
                before = (root / "GxP2.Model.dll").stat()
                data = (root / "GxP2.Model.dll").read_bytes()
                (root / "GxP2.Model.dll").write_bytes(b"X" * len(data))
                os.utime(root / "GxP2.Model.dll", ns=(before.st_atime_ns, before.st_mtime_ns))
                validator._validate("fixture", "class A {}")
                with patch.object(validator, "_sdk_identity", return_value={"dotnet_path": sys.executable,
                                  "sdk_version": "6.0.429", "sdk_path": str(root / "different-sdk")}):
                    validator._validate("fixture", "class A {}")
            self.assertEqual([call.args[0][1] for call in commands.call_args_list], ["restore", "build"] * 3)
            self.assertEqual(len({call.args[0][2] for call in commands.call_args_list}), 3)

    def test_corrupt_restore_cache_is_replaced_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            success = subprocess.CompletedProcess(["dotnet", "build"], 0, "Build succeeded", "")
            with patch.object(validator, "_dotnet", side_effect=self.build_protocol(success)):
                validator._validate("fixture", "class A {}")
            next((runtime_root() / "validation").rglob("project.assets.json")).write_text("corrupt")
            with patch.object(validator, "_dotnet", side_effect=self.build_protocol(success)) as commands:
                validator._validate("fixture", "class A {}")
            self.assertEqual([call.args[0][1] for call in commands.call_args_list], ["restore", "build"])

    def test_restore_failure_is_checker_failure_without_build_or_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            failure = subprocess.CompletedProcess(["dotnet", "restore"], 1, "error NU1101: Missing offline SDK pack", "")
            with patch.object(validator, "_dotnet", return_value=failure) as commands:
                with self.assertRaises(RuntimeValidationError) as caught:
                    validator._validate("fixture", "class A {}")
            self.assertEqual(caught.exception.code, "checker_unavailable")
            self.assertEqual(commands.call_count, 1)
            self.assertFalse(list((runtime_root() / "validation").rglob("Candidate.cs")))

    def test_real_probe_transports_performance_without_changing_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)
            validator.runtime = replace(validator.runtime, dotnet_command=str(self.fake_dotnet(root, "class A {}")))
            first = validator.validate("first", "class A {}")
            self.assertTrue(any(item["stage"] == "runtime.workspace_restore" for item in validator.last_performance))
            second = validator.validate("second", "class A {}")
            self.assertFalse(any(item["stage"] == "runtime.workspace_restore" for item in validator.last_performance))
            self.assertTrue(any(item["stage"] == "runtime.csharp_build" for item in validator.last_performance))
            self.assertEqual(first, second)
            self.assertNotIn("performance", second)

    def test_outer_probe_timeout_scrubs_active_candidate_and_recovers_cold(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)
            command = self.fake_dotnet(root, "class A {}", stall_build=True)
            validator.runtime = replace(validator.runtime, dotnet_command=str(command))
            with self.assertRaises(RuntimeValidationError) as caught:
                validator._probe("validate", timeout=2, action_code="fixture", csharp_code="class A {}")
            self.assertEqual(caught.exception.code, "checker_timeout")
            stages = {span["stage"] for span in validator.last_performance}
            self.assertTrue({"runtime.references", "runtime.sdk", "runtime.workspace_restore", "runtime.probe"}.issubset(stages))
            pool = runtime_root() / "validation" / "workspaces"
            self.assertFalse(list((pool.parent / "probe-metadata").glob("*")))
            self.assertFalse(list(pool.rglob("Candidate.cs")))
            self.assertFalse(list(pool.rglob("lease.json")))
            self.assertEqual(json.loads(next(pool.rglob("state.json")).read_text())["status"], "dirty")
            self.fake_dotnet(root, "class A {}")
            validator.validate("fixture", "class A {}")
            self.assertTrue(any(span["stage"] == "runtime.workspace_restore" for span in validator.last_performance))

    def test_interrupted_timing_metadata_is_bounded_and_never_contains_payload(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary))
            validator.request_id = "a" * 32
            validator.last_performance = [{"stage": "runtime.sdk", "elapsed_ms": 1.25, "cache_hit": False,
                                           "source": "private candidate", "params": {"secret": "hidden"},
                                           "error_code": "checker_timeout"}] * 100
            validator._publish_performance()
            path = validator._performance_path(validator.request_id)
            serialized = path.read_text()
            self.assertNotIn("private candidate", serialized)
            self.assertNotIn("secret", serialized)
            consumed = validator._consume_performance(validator.request_id)
            self.assertEqual(len(consumed), 64)
            self.assertEqual(set(consumed[0]), {"stage", "elapsed_ms", "cache_hit", "error_code"})
            self.assertFalse(path.exists())
            self.assertIsNone(validator._performance_path("../outside"))

    def test_dotnet_inner_timeout_kills_child_and_reports_checker_timeout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            started = time.monotonic()
            with self.assertRaises(RuntimeValidationError) as caught:
                RuntimeCsharpValidator._dotnet([sys.executable, "-c", "import time; time.sleep(20)"], root, 0.2, "compilation")
            self.assertEqual(caught.exception.code, "checker_timeout")
            self.assertLess(time.monotonic() - started, 7)

    @unittest.skipUnless(os.environ.get("GXP_RUN_DOTNET_SMOKE") == "1", "opt-in synthetic local SDK smoke")
    def test_real_dotnet_cold_hot_and_invalid_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            validator = self.snapshot(Path(temporary), mode="development_build")
            # The regular tests replace SDK discovery. This optional smoke uses
            # the original method and no business DLLs or application state.
            validator._sdk_identity = lambda cwd, deadline: ORIGINAL_SDK_IDENTITY(validator, cwd, deadline)
            # The SDK loads ancestor props before the project body. Verify
            # command-line global properties keep unrelated inputs excluded.
            (runtime_root() / "Directory.Build.props").write_text(
                '<Project><ItemGroup><Compile Include="UnexpectedParentSource.cs" /></ItemGroup></Project>', encoding="utf-8")
            with patch.object(validator, "_references", return_value=[]):
                first = validator._validate("smoke", "public class Smoke { public int Value => 1; }")
                self.assertTrue(first["success"])
                self.assertTrue(any(item["stage"] == "runtime.workspace_restore" for item in validator.last_performance))
                second = validator._validate("smoke", "public class Smoke { public int Value => 2; }")
                self.assertTrue(second["success"])
                self.assertFalse(any(item["stage"] == "runtime.workspace_restore" for item in validator.last_performance))
                with self.assertRaises(RuntimeValidationError) as caught:
                    validator._validate("smoke", "public class Smoke { public int Value => ; }")
                self.assertEqual(caught.exception.code, "compilation_failed")
                corrected = validator._validate("smoke", "public class Smoke { public int Value => 3; }")
                self.assertTrue(corrected["compilation_succeeded"])
                self.assertFalse(any(item["stage"] == "runtime.workspace_restore" for item in validator.last_performance))
                self.assertTrue(any(item["stage"] == "runtime.csharp_build" for item in validator.last_performance))
            self.assertFalse(list((runtime_root() / "validation").rglob("Candidate.cs")))


if __name__ == "__main__":
    unittest.main()
