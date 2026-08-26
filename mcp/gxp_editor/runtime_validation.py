from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .config import EditorConfig, runtime_root
from .hashing import sha256_json, sha256_text


class RuntimeValidationError(RuntimeError):
    pass


class RuntimeCsharpValidator:
    REQUIRED_ASSEMBLIES = {
        "FreeSql.dll",
        "GxP2.Model.dll",
        "GxP2.Services.dll",
        "Newtonsoft.Json.dll",
    }
    MAX_REFERENCE_FILES = 500
    MAX_REFERENCE_BYTES = 1024 * 1024 * 1024

    def __init__(self, config: EditorConfig):
        self.config = config
        self.runtime = config.runtime_references

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def _references(self) -> list[Path]:
        directory = self.runtime.path
        if not directory.is_dir():
            raise RuntimeValidationError(
                f"Runtime reference snapshot was not found: {directory}"
            )
        references = sorted(
            (item.resolve() for item in directory.glob("*.dll") if item.is_file()),
            key=lambda item: item.name.lower(),
        )
        if not references:
            raise RuntimeValidationError("Runtime reference snapshot contains no DLL files")
        if len(references) > self.MAX_REFERENCE_FILES:
            raise RuntimeValidationError(
                f"Runtime reference snapshot contains {len(references)} DLLs; limit is {self.MAX_REFERENCE_FILES}"
            )
        total_bytes = sum(item.stat().st_size for item in references)
        if total_bytes > self.MAX_REFERENCE_BYTES:
            raise RuntimeValidationError("Runtime reference snapshot exceeds the 1 GiB safety limit")
        missing = sorted(self.REQUIRED_ASSEMBLIES - {item.name for item in references})
        if missing:
            raise RuntimeValidationError(
                "Runtime reference snapshot is missing required assemblies: " + ", ".join(missing)
            )
        return references

    def _manifest(self, references: list[Path]) -> dict[str, Any]:
        entries = [
            {
                "name": item.name,
                "length": item.stat().st_size,
                "sha256": self._file_sha256(item),
            }
            for item in references
        ]
        return {
            "source_mode": self.runtime.source_mode,
            "target_framework": self.runtime.target_framework,
            "reference_count": len(entries),
            "reference_manifest_sha256": sha256_json(entries),
        }

    def status(self) -> dict[str, Any]:
        references = self._references()
        try:
            process = subprocess.run(
                [self.runtime.dotnet_command, "--version"],
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeValidationError(f"dotnet runtime check failed: {exc}") from exc
        if process.returncode != 0:
            raise RuntimeValidationError(
                (process.stderr or process.stdout or "dotnet --version failed").strip()
            )
        result = self._manifest(references)
        result.update(
            {
                "available": True,
                "production_compatible": self.runtime.source_mode == "deployment_snapshot",
                "dotnet_version": process.stdout.strip(),
                "zero_backend_mode": True,
            }
        )
        return result

    @staticmethod
    def _bounded_output(process: subprocess.CompletedProcess[str]) -> str:
        lines = (process.stdout + "\n" + process.stderr).splitlines()
        meaningful = [line.strip() for line in lines if line.strip()]
        return "\n".join(meaningful[-80:])[:16000]

    def validate(self, action_code: str, csharp_code: str) -> dict[str, Any]:
        if not csharp_code.strip():
            status = self.status()
            return {
                **status,
                "success": True,
                "csharp_sha256": sha256_text(csharp_code),
                "diagnostics": [],
            }
        if not action_code or len(action_code) > 128:
            raise RuntimeValidationError("Action code is missing or too long")
        references = self._references()
        manifest = self._manifest(references)
        validation_root = runtime_root() / "validation"
        validation_root.mkdir(parents=True, exist_ok=True)
        reference_items = "\n".join(
            "    <Reference Include=\"{}\"><HintPath>{}</HintPath><Private>false</Private></Reference>".format(
                escape(item.stem), escape(str(item))
            )
            for item in references
        )
        project_text = f"""<Project Sdk=\"Microsoft.NET.Sdk\">
  <PropertyGroup>
    <TargetFramework>{escape(self.runtime.target_framework)}</TargetFramework>
    <Nullable>disable</Nullable>
    <ImplicitUsings>disable</ImplicitUsings>
    <EnableDefaultCompileItems>false</EnableDefaultCompileItems>
    <RestoreIgnoreFailedSources>true</RestoreIgnoreFailedSources>
    <AssemblyName>GxpLowcodeCandidate</AssemblyName>
  </PropertyGroup>
  <ItemGroup>
    <FrameworkReference Include=\"Microsoft.AspNetCore.App\" />
    <Compile Include=\"Candidate.cs\" />
  </ItemGroup>
  <ItemGroup>
{reference_items}
  </ItemGroup>
</Project>
"""
        with tempfile.TemporaryDirectory(prefix="candidate-", dir=validation_root) as temporary:
            root = Path(temporary)
            project = root / "Candidate.csproj"
            source = root / "Candidate.cs"
            project.write_text(project_text, encoding="utf-8")
            source.write_text(csharp_code, encoding="utf-8")
            try:
                process = subprocess.run(
                    [
                        self.runtime.dotnet_command,
                        "build",
                        str(project),
                        "--nologo",
                        "--verbosity",
                        "minimal",
                        "--ignore-failed-sources",
                    ],
                    text=True,
                    capture_output=True,
                    cwd=root,
                    timeout=90,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeValidationError(f"Candidate C# compilation failed to start: {exc}") from exc
        output = self._bounded_output(process)
        if process.returncode != 0:
            raise RuntimeValidationError("Candidate C# compilation failed:\n" + output)
        return {
            **manifest,
            "available": True,
            "production_compatible": self.runtime.source_mode == "deployment_snapshot",
            "zero_backend_mode": True,
            "success": True,
            "csharp_sha256": sha256_text(csharp_code),
            "diagnostics": [line for line in output.splitlines() if " warning " in line.lower()][:20],
        }
