from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .config import EditorConfig, runtime_root
from .hashing import sha256_json, sha256_text
from .runtime_workspace import WorkspaceBusyError, cleanup_interrupted_request, lease_workspace


class RuntimeValidationError(RuntimeError):
    def __init__(self, message: str, *, code: str = "checker_unavailable", diagnostics: list[str] | None = None):
        super().__init__(message)
        self.code = code
        self.diagnostics = list(diagnostics or [])


class RuntimeCsharpValidator:
    REQUIRED_ASSEMBLIES = {
        "FreeSql.dll",
        "GxP2.Model.dll",
        "GxP2.Services.dll",
        "Newtonsoft.Json.dll",
    }
    MAX_REFERENCE_FILES = 500
    MAX_REFERENCE_BYTES = 1024 * 1024 * 1024
    STATUS_TIMEOUT_SECONDS = 20
    VALIDATION_TIMEOUT_SECONDS = 65
    DOTNET_STATUS_TIMEOUT_SECONDS = 10
    BUILD_TIMEOUT_SECONDS = 55
    RESTORE_TIMEOUT_SECONDS = 20
    WORKSPACE_WAIT_SECONDS = 2
    # Invalidate workspaces restored before parent MSBuild imports were disabled
    # through global command-line properties.
    PROJECT_TEMPLATE_VERSION = 2
    NUGET_TEMPLATE_VERSION = 1

    def __init__(self, config: EditorConfig):
        self.config = config
        self.runtime = config.runtime_references
        self.last_performance: list[dict[str, Any]] = []

    @contextmanager
    def _measure(self, stage: str, **tags: Any):
        started = time.monotonic()
        try:
            yield
        except Exception as exc:
            tags["error_code"] = getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            self.last_performance.append({"stage": stage, "elapsed_ms": round((time.monotonic() - started) * 1000, 3), **tags})
            self._publish_performance()

    @staticmethod
    def _bounded_performance(value: Any) -> list[dict[str, Any]]:
        """Only bounded timing metadata crosses the interrupted-probe channel."""
        result = []
        for item in value[:64] if isinstance(value, list) else []:
            if not isinstance(item, dict) or not re.fullmatch(r"runtime\.[a-z_]{1,48}", str(item.get("stage", ""))):
                continue
            duration = item.get("elapsed_ms")
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or duration < 0 or duration > 86400000 or not math.isfinite(duration)):
                continue
            entry = {"stage": item["stage"], "elapsed_ms": duration}
            for key in ("cache_hit", "cold"):
                if isinstance(item.get(key), bool):
                    entry[key] = item[key]
            code = item.get("error_code")
            if isinstance(code, str) and re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]{0,63}", code):
                entry["error_code"] = code
            wait = item.get("slot_wait_ms")
            if (isinstance(wait, (int, float)) and not isinstance(wait, bool)
                    and 0 <= wait <= 86400000 and math.isfinite(wait)):
                entry["slot_wait_ms"] = wait
            result.append(entry)
        return result

    @staticmethod
    def _performance_path(request_id: str) -> Path | None:
        if not isinstance(request_id, str) or not re.fullmatch(r"[0-9a-f]{32}", request_id):
            return None
        return runtime_root() / "validation" / "probe-metadata" / (request_id + ".json")

    def _publish_performance(self) -> None:
        path = self._performance_path(getattr(self, "request_id", None))
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self._bounded_performance(self.last_performance)), encoding="utf-8")
            temporary.replace(path)
        except OSError:
            # Observability never changes validation or cache validity.
            pass

    def _consume_performance(self, request_id: str) -> list[dict[str, Any]]:
        path = self._performance_path(request_id)
        if path is None:
            return []
        try:
            value = json.loads(path.read_text(encoding="utf-8")) if path.stat().st_size <= 32768 else []
            return self._bounded_performance(value)
        except (OSError, ValueError):
            return []
        finally:
            for item in (path, path.with_suffix(".tmp")):
                try:
                    item.unlink(missing_ok=True)
                except OSError:
                    pass

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

    def _probe(self, command: str, *, timeout: float, **payload: Any) -> dict[str, Any]:
        # DLL reads can block inside the OS, so a thread timeout is insufficient.
        self.last_performance = []
        request_id = uuid.uuid4().hex
        workspace_pool = runtime_root() / "validation" / "workspaces"
        environment = dict(os.environ)
        module_root = str(Path(__file__).resolve().parents[1])
        environment["PYTHONPATH"] = module_root + os.pathsep + environment.get("PYTHONPATH", "")
        process = subprocess.Popen(
            [sys.executable, "-m", "gxp_editor.runtime_probe"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        request = {"command": command, "runtime": asdict(self.runtime), "request_id": request_id, **payload}
        try:
            request_bytes = json.dumps(request, ensure_ascii=False).encode("utf-8")
            stdout, stderr = process.communicate(request_bytes, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if os.name == "nt":
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        capture_output=True, timeout=5, check=False,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    pass
            else:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.poll() is None:
                process.kill()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
            cleanup_interrupted_request(workspace_pool, request_id)
            self.last_performance = self._consume_performance(request_id)
            self.last_performance.append({"stage": "runtime.probe", "elapsed_ms": timeout * 1000, "error_code": "checker_timeout"})
            raise RuntimeValidationError(
                f"Advisory runtime {command} exceeded {timeout:g} seconds; "
                "local DLL access or compilation did not finish.",
                code="checker_timeout",
            ) from exc
        self.last_performance = self._consume_performance(request_id)
        try:
            response = json.loads(stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            cleanup_interrupted_request(workspace_pool, request_id)
            error_output = stderr.decode("utf-8", errors="replace")[-2000:]
            raise RuntimeValidationError(
                "Runtime probe returned invalid UTF-8 JSON: " + error_output,
                code="probe_protocol_error",
            ) from exc
        if not isinstance(response, dict):
            cleanup_interrupted_request(workspace_pool, request_id)
            raise RuntimeValidationError("Runtime probe returned a non-object response", code="probe_protocol_error")
        self.last_performance = self._bounded_performance(response.get("performance")) or self.last_performance
        if process.returncode != 0 or response.get("ok") is not True:
            cleanup_interrupted_request(workspace_pool, request_id)
            raise RuntimeValidationError(
                str(response.get("error") or "Runtime probe failed"),
                code=str(response.get("error_code") or "checker_unavailable"),
                diagnostics=response.get("diagnostics") if isinstance(response.get("diagnostics"), list) else [],
            )
        result = response.get("result")
        if not isinstance(result, dict):
            cleanup_interrupted_request(workspace_pool, request_id)
            raise RuntimeValidationError("Runtime probe returned an invalid result", code="probe_protocol_error")
        return result

    def status(self) -> dict[str, Any]:
        return self._probe("status", timeout=self.STATUS_TIMEOUT_SECONDS)

    def reuse_evidence(self, evidence: dict[str, Any], csharp_code: str) -> dict[str, Any] | None:
        """Reuse a bound successful build when code and reference bytes are unchanged."""
        if not csharp_code.strip():
            return self.no_source_evidence(csharp_code)
        if (evidence.get("csharp_sha256") != sha256_text(csharp_code)
                or evidence.get("success") is not True or evidence.get("compilation_succeeded") is not True
                or evidence.get("available") is not True):
            return None
        manifest = self._manifest(self._references())
        if any(evidence.get(key) != value for key, value in manifest.items()):
            return None
        return {**evidence, "build_reused": True}

    def _status(self) -> dict[str, Any]:
        references = self._references()
        try:
            process = subprocess.run(
                [self.runtime.dotnet_command, "--version"],
                stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                capture_output=True,
                timeout=self.DOTNET_STATUS_TIMEOUT_SECONDS,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeValidationError("dotnet runtime check timed out", code="checker_timeout") from exc
        except OSError as exc:
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

    @staticmethod
    def _build_failure_code(output: str) -> str:
        # A failed restore/SDK/MSBuild invocation does not establish a defect in C#.
        # Every actual error diagnostic must be a C# compiler error. In
        # particular, a mixed unnumbered MSBuild error must not keep restore.
        errors = re.findall(r"\berror(?:\s+([A-Z]+\d+))?\s*:", output, re.IGNORECASE)
        only_csharp = bool(errors) and all(re.fullmatch(r"CS\d+", code, re.IGNORECASE) for code in errors)
        return "compilation_failed" if only_csharp else "checker_unavailable"

    @classmethod
    def _source_only_failure(cls, output: str) -> bool:
        if cls._build_failure_code(output) != "compilation_failed":
            return False
        errors = [line for line in output.splitlines() if re.search(r"\berror\s+CS\d+\s*:", line, re.IGNORECASE)]
        # CSC-level errors may describe missing metadata, source or output files.
        # Reuse only diagnostics actually located in the candidate source.
        return bool(errors) and all(re.search(
            r"(?:^|[\\/])Candidate\.cs\(\d+(?:,\d+)?\)\s*:\s*error\s+CS\d+\s*:",
            line, re.IGNORECASE,
        ) for line in errors)

    def validate(self, action_code: str, csharp_code: str) -> dict[str, Any]:
        self.last_performance = []
        if not csharp_code.strip():
            return self.no_source_evidence(csharp_code)
        result = self._probe(
            "validate", timeout=self.VALIDATION_TIMEOUT_SECONDS,
            action_code=action_code, csharp_code=csharp_code,
        )
        if result.get("csharp_sha256") != sha256_text(csharp_code):
            raise RuntimeValidationError(
                "Runtime probe validated different C# source text", code="probe_protocol_error"
            )
        return result

    def no_source_evidence(self, csharp_code: str = "") -> dict[str, Any]:
        """No SDK or assembly inspection is relevant to an empty C# candidate."""
        return {"available": False, "success": True, "compilation_succeeded": False,
                "validation_scope": "no_csharp_source", "production_compatible": False,
                "reference_conflicts": False, "probe_skipped": True, "zero_backend_mode": True,
                "source_mode": self.runtime.source_mode, "target_framework": self.runtime.target_framework,
                "csharp_sha256": sha256_text(csharp_code), "diagnostics": []}

    def _validate(self, action_code: str, csharp_code: str) -> dict[str, Any]:
        self.last_performance = []
        if not csharp_code.strip():
            return self.no_source_evidence(csharp_code)
        if not action_code or len(action_code) > 128:
            raise RuntimeValidationError("Action code is missing or too long")
        deadline = time.monotonic() + self.BUILD_TIMEOUT_SECONDS
        with self._measure("runtime.references"):
            references = self._references()
            manifest = self._manifest(references)
        validation_root = runtime_root() / "validation"
        validation_root.mkdir(parents=True, exist_ok=True)
        with self._measure("runtime.sdk"):
            sdk = self._sdk_identity(validation_root, deadline)
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
    <NuGetAudit>false</NuGetAudit>
    <AssemblyName>GxpLowcodeCandidate</AssemblyName>
    <UseSharedCompilation>false</UseSharedCompilation>
    <GenerateAssemblyInfo>false</GenerateAssemblyInfo>
    <ImportDirectoryBuildProps>false</ImportDirectoryBuildProps>
    <ImportDirectoryBuildTargets>false</ImportDirectoryBuildTargets>
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
        environment_key = sha256_json({
            "sdk": sdk, "manifest": manifest, "project_sha256": sha256_text(project_text),
            "nuget_sha256": sha256_text(self._nuget_text(Path("WORKSPACE"))),
            "project_template": self.PROJECT_TEMPLATE_VERSION, "nuget_template": self.NUGET_TEMPLATE_VERSION,
        })
        request_id = getattr(self, "request_id", None) or uuid.uuid4().hex
        compilation_error = None
        try:
            with lease_workspace(
                validation_root / "workspaces", environment_key, request_id, project_text=project_text,
                nuget_text_factory=self._nuget_text,
                wait_seconds=min(self.WORKSPACE_WAIT_SECONDS, self._remaining(deadline)),
            ) as workspace:
                self.last_performance.append({"stage": "runtime.workspace", "elapsed_ms": workspace.wait_ms,
                                              "cache_hit": workspace.cache_hit, "slot_wait_ms": workspace.wait_ms})
                self._publish_performance()
                (workspace.root / "global.json").write_text(json.dumps({"sdk": {
                    "version": sdk["sdk_version"], "rollForward": "disable", "allowPrerelease": True,
                }}), encoding="utf-8")
                # SDK props are imported before the project body. These must
                # be global properties, not just values inside the csproj.
                common = ["--nologo", "--verbosity", "minimal", "-p:RestoreConfigFile=" + str(workspace.nuget_config),
                          "-p:NuGetAudit=false", "-p:ImportDirectoryBuildProps=false", "-p:ImportDirectoryBuildTargets=false"]
                if not workspace.cache_hit:
                    with self._measure("runtime.workspace_restore", cold=True):
                        restore = self._dotnet(
                            [sdk["dotnet_path"], "restore", str(workspace.project), *common, "--ignore-failed-sources"],
                            workspace.root, min(self.RESTORE_TIMEOUT_SECONDS, self._remaining(deadline)), "restore",
                        )
                        self._require_build_success(restore, restoring=True)
                    workspace.capture_restore_state()
                workspace.source.write_text(csharp_code, encoding="utf-8")
                try:
                    with self._measure("runtime.csharp_build", cache_hit=workspace.cache_hit):
                        process = self._dotnet(
                            [sdk["dotnet_path"], "build", str(workspace.project), *common, "--no-restore", "--no-incremental"],
                            workspace.root, self._remaining(deadline), "compilation",
                        )
                        self._require_build_success(process)
                except RuntimeValidationError as exc:
                    if exc.code != "compilation_failed" or not self._source_only_failure(process.stdout + "\n" + process.stderr):
                        raise
                    # Keep only the verified restore environment, not a success
                    # verdict. Finish drift checks and cleanup before rethrowing.
                    compilation_error = exc
                output = self._bounded_output(process)
                full_output = process.stdout + "\n" + process.stderr
                reference_conflicts = bool(re.search(r"\bMSB3277\b", full_output, re.IGNORECASE))
                with self._measure("runtime.references_verify"):
                    final_manifest = self._manifest(self._references())
                    if (final_manifest["reference_manifest_sha256"] != manifest["reference_manifest_sha256"]
                            or final_manifest["reference_count"] != manifest["reference_count"]):
                        raise RuntimeValidationError("Runtime reference snapshot changed during C# validation", code="reference_drift")
                workspace.mark_restore_ready()
        except WorkspaceBusyError as exc:
            raise RuntimeValidationError(str(exc), code="checker_busy") from exc
        except (OSError, ValueError) as exc:
            raise RuntimeValidationError(f"Local C# workspace could not complete: {exc}") from exc
        if compilation_error is not None:
            raise compilation_error
        return {
            **manifest,
            "available": True,
            "production_compatible": self.runtime.source_mode == "deployment_snapshot" and not reference_conflicts,
            "reference_conflicts": reference_conflicts,
            "zero_backend_mode": True,
            "success": True,
            "compilation_succeeded": True,
            "validation_scope": "local_reference_compile",
            "csharp_sha256": sha256_text(csharp_code),
            "diagnostics": [line for line in output.splitlines() if " warning " in line.lower()][:20],
        }

    @staticmethod
    def _nuget_text(root: Path) -> str:
        return ("<configuration><packageSources><clear /></packageSources>"
                "<fallbackPackageFolders><clear /></fallbackPackageFolders>"
                "<config><add key=\"globalPackagesFolder\" value=\"{}\" /></config></configuration>").format(
            escape(str(root / "packages"), {chr(34): "&quot;"}))

    @staticmethod
    def _remaining(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeValidationError("Candidate C# compilation timed out", code="checker_timeout")
        return remaining

    @staticmethod
    def _dotnet(command: list[str], cwd: Path, timeout: float, phase: str) -> subprocess.CompletedProcess[str]:
        process = None
        try:
            environment = dict(os.environ)
            environment.update({"DOTNET_CLI_USE_MSBUILD_SERVER": "0", "MSBUILDDISABLENODEREUSE": "1",
                                "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "true"})
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd,
                                       env=environment,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            stdout, stderr = process.communicate(timeout=timeout)
            return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            if process is not None:
                RuntimeCsharpValidator._terminate_command(process)
            raise RuntimeValidationError(f"Candidate C# {phase} timed out", code="checker_timeout") from exc
        except OSError as exc:
            raise RuntimeValidationError(f"Candidate C# {phase} failed to start: {exc}") from exc

    @staticmethod
    def _terminate_command(process: subprocess.Popen[str]) -> None:
        if os.name == "nt":
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=3,
                               creationflags=subprocess.CREATE_NO_WINDOW, check=False)
            except (OSError, subprocess.TimeoutExpired):
                pass
        else:
            # Keep commands in the probe process group so its outer timeout still
            # kills the complete tree. At the shorter inner timeout reap children.
            try:
                listing = subprocess.run(["ps", "-eo", "pid=,ppid="], capture_output=True, text=True, timeout=2, check=False)
                children: dict[int, list[int]] = {}
                for line in listing.stdout.splitlines():
                    pid, parent = map(int, line.split())
                    children.setdefault(parent, []).append(pid)
                descendants: list[int] = []
                def collect(pid: int) -> None:
                    for child in children.get(pid, []):
                        collect(child)
                        descendants.append(child)
                collect(process.pid)
                for pid in descendants:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def _sdk_identity(self, cwd: Path, deadline: float) -> dict[str, str]:
        command = shutil.which(self.runtime.dotnet_command)
        if not command:
            raise RuntimeValidationError("Configured dotnet command was not found")
        executable = str(Path(command).resolve())
        version_result = self._dotnet([executable, "--version"], cwd, min(self.DOTNET_STATUS_TIMEOUT_SECONDS, self._remaining(deadline)), "SDK discovery")
        if version_result.returncode != 0:
            raise RuntimeValidationError("dotnet SDK discovery failed: " + self._bounded_output(version_result))
        version = version_result.stdout.strip()
        if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.\-]+)?", version):
            raise RuntimeValidationError("dotnet SDK discovery returned an invalid version")
        sdk_result = self._dotnet([executable, "--list-sdks"], cwd, min(self.DOTNET_STATUS_TIMEOUT_SECONDS, self._remaining(deadline)), "SDK discovery")
        if sdk_result.returncode != 0:
            raise RuntimeValidationError("dotnet SDK location discovery failed: " + self._bounded_output(sdk_result))
        paths = [Path(match.group(2)) / version for line in sdk_result.stdout.splitlines()
                 if (match := re.fullmatch(r"([^\s]+)\s+\[(.+)\]\s*", line)) and match.group(1) == version]
        if len(paths) != 1 or not paths[0].is_dir():
            raise RuntimeValidationError("Selected dotnet SDK location was not found or is ambiguous")
        sdk_environment = {key: value for key, value in os.environ.items()
                           if key.upper().startswith(("DOTNET_", "MSBUILD", "NUGET_"))}
        return {"dotnet_path": executable, "dotnet_sha256": self._file_sha256(Path(executable)),
                "sdk_version": version, "sdk_path": str(paths[0].resolve()), "environment_sha256": sha256_json(sdk_environment)}

    def _require_build_success(self, process: subprocess.CompletedProcess[str], *, restoring: bool = False) -> None:
        if process.returncode == 0:
            return
        full_output = process.stdout + "\n" + process.stderr
        failure_code = "checker_unavailable" if restoring else self._build_failure_code(full_output)
        message = "Candidate C# compilation failed" if failure_code == "compilation_failed" else "Local C# checker could not complete"
        raise RuntimeValidationError(message + ":\n" + self._bounded_output(process), code=failure_code, diagnostics=full_output.splitlines())
