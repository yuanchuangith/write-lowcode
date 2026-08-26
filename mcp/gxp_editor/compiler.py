from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from .config import EditorConfig
from .hashing import sha256_json, sha256_text


class CompilerError(RuntimeError):
    pass


class CompilerBridge:
    _VOLATILE_DEFAULT_ROW_ID_ELEMENTS = {
        "SetComponentStatus",
        "SetUploadButtonStatus",
    }

    def __init__(self, config: EditorConfig):
        self.config = config
        self._registry: dict[str, Any] | None = None

    @property
    def path(self) -> Path:
        return self.config.compiler_path

    def _call(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.path.exists():
            raise CompilerError(f"ActionDesign compiler bridge was not found: {self.path}")
        process = subprocess.run(
            ["node", str(self.path)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=self.config.frontend_repo,
            timeout=60,
            check=False,
        )
        if process.returncode != 0:
            try:
                failure = json.loads(process.stdout)
            except json.JSONDecodeError:
                failure = None
            if isinstance(failure, dict) and failure.get("error"):
                raise CompilerError(str(failure["error"]))
            raise CompilerError((process.stderr or process.stdout or "compiler failed").strip())
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise CompilerError(f"Compiler returned invalid JSON: {exc}") from exc
        if not result.get("ok"):
            raise CompilerError(str(result.get("error") or "compiler rejected request"))
        return result

    @classmethod
    def _stable_registry_hash(cls, registry: dict[str, Any]) -> str:
        elements = deepcopy(registry.get("elements") or [])
        for element in elements:
            if (
                not isinstance(element, dict)
                or element.get("key") not in cls._VOLATILE_DEFAULT_ROW_ID_ELEMENTS
            ):
                continue
            for parameter in element.get("input_params") or []:
                if not isinstance(parameter, dict):
                    continue
                for row in parameter.get("defaultValue") or []:
                    if isinstance(row, dict):
                        row.pop("id", None)
        return sha256_json(
            {
                "rule_source_sha256": registry.get("rule_source_sha256"),
                "elements": elements,
            }
        )

    def describe_registry(self) -> dict[str, Any]:
        if self._registry is None:
            result = self._call({"command": "describe_registry"})
            result["registry_hash"] = self._stable_registry_hash(result)
            result["bridge_sha256"] = sha256_text(self.path.read_text(encoding="utf-8"))
            self._registry = result
        return deepcopy(self._registry)

    def _bind_registry_hash(self, result: dict[str, Any]) -> dict[str, Any]:
        validation = result.get("validation")
        if isinstance(validation, dict):
            validation["registry_hash"] = self.describe_registry()["registry_hash"]
        return result

    def validate_design(self, design: dict[str, Any]) -> dict[str, Any]:
        result = self._call({"command": "validate_design", "design": design})
        return self._bind_registry_hash(result)

    def compile_patch(self, design: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
        if not operations:
            raise CompilerError("At least one canvas operation is required")
        if len(operations) > 100:
            raise CompilerError("A single plan may contain at most 100 canvas operations")
        if any(not isinstance(operation, dict) for operation in operations):
            raise CompilerError("Every canvas operation must be an object")
        operation_bytes = len(json.dumps(operations, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        if operation_bytes > 2 * 1024 * 1024:
            raise CompilerError("Canvas operations exceed the 2 MiB planning limit")
        forbidden = {"js_code", "csharp_code", "sql", "key", "depth", "relation_data"}
        for operation in operations:
            overlap = forbidden.intersection(operation)
            if overlap:
                raise CompilerError("Canvas operations contain forbidden fields: " + ", ".join(sorted(overlap)))
        result = self._call({"command": "compile_patch", "design": design, "operations": operations})
        return self._bind_registry_hash(result)
