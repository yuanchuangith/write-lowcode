from __future__ import annotations

import subprocess
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


class RuntimeValidationTests(unittest.TestCase):
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
