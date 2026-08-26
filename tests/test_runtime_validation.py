from __future__ import annotations

import subprocess
import tempfile
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
                status = validator.status()
        self.assertTrue(status["available"])
        self.assertFalse(status["production_compatible"])
        self.assertTrue(status["zero_backend_mode"])

    def test_missing_required_assembly_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)
            (root / "GxP2.Services.dll").unlink()
            with self.assertRaisesRegex(RuntimeValidationError, "missing required assemblies"):
                validator.status()

    def test_manifest_changes_when_reference_content_changes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            validator = self.snapshot(root)
            completed = subprocess.CompletedProcess(["dotnet", "--version"], 0, "6.0.428\n", "")
            with patch("gxp_editor.runtime_validation.subprocess.run", return_value=completed):
                before = validator.status()["reference_manifest_sha256"]
                (root / "GxP2.Model.dll").write_bytes(b"changed")
                after = validator.status()["reference_manifest_sha256"]
        self.assertNotEqual(before, after)


if __name__ == "__main__":
    unittest.main()
