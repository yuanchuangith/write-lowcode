"""Run launcher scripts with an isolated native stub; never create a real venv or install pip."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
STUB_SOURCE = r"""
using System;
using System.IO;
using System.Reflection;
using System.Diagnostics;
using System.Linq;
public class RuntimeCommandStub {
    public static int Main(string[] args) {
        if (args.Length > 1 && args[1] == "setup") {
            // Execute the actual guarded setup bootstrap in an isolated Python
            // process; only its native venv/pip children remain harmless stubs.
            var start = new ProcessStartInfo();
            start.FileName = Environment.GetEnvironmentVariable("GXP_SCRIPT_TEST_REAL_PYTHON");
            start.Arguments = "\"" + Environment.GetEnvironmentVariable("GXP_SCRIPT_TEST_BOOTSTRAP") + "\" "
                + String.Join(" ", args.Skip(1).Select(value => "\"" + value + "\""));
            start.UseShellExecute = false;
            using (var child = Process.Start(start)) { child.WaitForExit(); return child.ExitCode; }
        }
        string stage = args.Length > 1 && args[1] == "venv" ? "venv"
            : args.Length > 1 && args[1] == "pip" ? (Array.IndexOf(args, "--upgrade") >= 0 ? "upgrade" : "install")
            : "server";
        File.AppendAllText(Environment.GetEnvironmentVariable("GXP_SCRIPT_TEST_LOG"), stage + "\n");
        if (Environment.GetEnvironmentVariable("GXP_SCRIPT_TEST_FAILURE") == stage) return 37;
        if (stage == "venv") {
            string scripts = Path.Combine(args[2], "Scripts");
            Directory.CreateDirectory(scripts);
            File.Copy(Assembly.GetExecutingAssembly().Location, Path.Combine(scripts, "python.exe"));
        }
        return 0;
    }
}
"""


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell is required")
class RuntimeScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.stub_folder = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.stub_folder.cleanup)
        cls.stub = Path(cls.stub_folder.name) / "python.exe"
        cls.bootstrap = Path(cls.stub_folder.name) / "isolated_setup.py"
        cls.bootstrap.write_text("""import os, sys
sys.path.insert(0, os.environ['GXP_SCRIPT_TEST_MODULE_ROOT'])
from gxp_editor import runtime_package
# This fixture's target is a newly-created temporary environment; no real
# process inventory or user-owned interpreter is involved.
runtime_package._legacy_environment_users = lambda interpreter: []
runtime_package.sys.executable = os.environ['GXP_SCRIPT_TEST_NATIVE']
runtime_package._main(sys.argv[1:])
""", encoding="utf-8")
        # A native executable exercises PowerShell's real LASTEXITCODE behavior.
        command = "$ErrorActionPreference = 'Stop'\nAdd-Type -TypeDefinition @'\n" + STUB_SOURCE + "\n'@ -OutputType ConsoleApplication -OutputAssembly $env:GXP_SCRIPT_TEST_STUB\n"
        env = dict(os.environ, GXP_SCRIPT_TEST_STUB=str(cls.stub))
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
            text=True, capture_output=True, env=env, timeout=30,
        )
        if result.returncode or not cls.stub.is_file():
            raise AssertionError(f"Native test stub compilation failed: {result.stdout}\n{result.stderr}")

    def environment(self, runtime: Path, failure: str) -> dict[str, str]:
        return dict(os.environ, LOCALAPPDATA=str(runtime),
                    PATH=str(self.stub.parent) + os.pathsep + os.environ.get("PATH", ""),
                    GXP_SCRIPT_TEST_LOG=str(runtime / "commands.log"), GXP_SCRIPT_TEST_FAILURE=failure,
                    GXP_SCRIPT_TEST_NATIVE=str(self.stub), GXP_SCRIPT_TEST_REAL_PYTHON=sys.executable,
                    GXP_SCRIPT_TEST_BOOTSTRAP=str(self.bootstrap),
                    GXP_SCRIPT_TEST_MODULE_ROOT=str(PLUGIN_ROOT / "mcp"))

    def run_powershell_script(self, name: str, env: dict[str, str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(PLUGIN_ROOT / "scripts" / name)],
            text=True, capture_output=True, env=env, timeout=20,
        )

    def test_setup_stops_at_each_native_failure_without_success_banner(self) -> None:
        for failure, stages in (("venv", ["venv"]), ("upgrade", ["venv", "upgrade"]),
                                ("install", ["venv", "upgrade", "install"])):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as folder:
                runtime = Path(folder)
                result = self.run_powershell_script("setup.ps1", self.environment(runtime, failure))
                self.assertNotEqual(0, result.returncode)
                self.assertNotIn("Runtime ready:", result.stdout)
                self.assertEqual(stages, (runtime / "commands.log").read_text().splitlines())

    def test_setup_reports_ready_only_after_all_steps_succeed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            result = self.run_powershell_script("setup.ps1", self.environment(runtime, ""))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn("Runtime ready:", result.stdout)
            self.assertEqual(["venv", "upgrade", "install"], (runtime / "commands.log").read_text().splitlines())

    def test_launchers_propagate_native_server_exit_status(self) -> None:
        for name in ("start_mcp.ps1", "start_mcp.cmd"):
            for failure, expected in (("server", 37), ("", 0)):
                with self.subTest(script=name, failure=failure), tempfile.TemporaryDirectory() as folder:
                    runtime = Path(folder)
                    python_path = runtime / "GxpLowcodeEditor/.venv/Scripts/python.exe"
                    python_path.parent.mkdir(parents=True)
                    shutil.copyfile(self.stub, python_path)
                    env = self.environment(runtime, failure)
                    if name.endswith(".ps1"):
                        result = self.run_powershell_script(name, env)
                    else:
                        result = subprocess.run(
                            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(PLUGIN_ROOT / "scripts" / name)],
                            text=True, capture_output=True, env=env, timeout=20,
                        )
                    self.assertEqual(expected, result.returncode, result.stderr)
                    self.assertEqual(["server"], (runtime / "commands.log").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
