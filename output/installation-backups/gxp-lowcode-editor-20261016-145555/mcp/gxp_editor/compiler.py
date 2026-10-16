from __future__ import annotations

import json
import hashlib
import re
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

from .config import EditorConfig
from .hashing import compiler_binding_payload, sha256_json, sha256_text
from .reference_repair import scan_references


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
    def frontend_repo(self) -> Path:
        return Path(self.config.frontend_repo).expanduser().resolve()

    @property
    def path(self) -> Path:
        return self.config.compiler_path

    def _git(self, *arguments: str, binary: bool = False) -> str | bytes:
        process = subprocess.run(
            ["git", "-C", str(self.frontend_repo), *arguments],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=not binary,
            encoding=None if binary else "utf-8",
            timeout=20,
            check=False,
        )
        if process.returncode != 0:
            error = process.stderr if isinstance(process.stderr, str) else process.stderr.decode("utf-8", "replace")
            raise CompilerError(f"Cannot inspect configured frontend Git repository: {error.strip()}")
        return process.stdout

    def source_provenance(self) -> dict[str, Any]:
        if not self.frontend_repo.is_dir():
            raise CompilerError(f"Configured frontend repository was not found: {self.frontend_repo}")
        root = Path(str(self._git("rev-parse", "--show-toplevel")).strip()).resolve()
        if root != self.frontend_repo:
            raise CompilerError(
                f"Configured frontend repository must be its Git root: configured={self.frontend_repo}, root={root}"
            )
        try:
            relative_bridge = self.path.resolve().relative_to(root)
        except ValueError as exc:
            raise CompilerError("ActionDesign compiler bridge must be inside the configured frontend repository") from exc
        commit = str(self._git("rev-parse", "HEAD")).strip()
        if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
            raise CompilerError("Configured frontend repository returned an invalid commit id")
        branch = str(self._git("branch", "--show-current")).strip() or "DETACHED"
        status = self._git("status", "--porcelain=v1", "-z", "--untracked-files=all", binary=True)
        diff = self._git("diff", "--no-ext-diff", "--binary", "HEAD", "--", binary=True)
        if not isinstance(status, bytes) or not isinstance(diff, bytes):
            raise CompilerError("Configured frontend repository returned invalid Git evidence")
        dirty_entries = [entry for entry in status.split(b"\0") if entry]
        workspace_hash = hashlib.sha256()
        workspace_hash.update(commit.encode("ascii"))
        workspace_hash.update(b"\0")
        workspace_hash.update(status)
        workspace_hash.update(b"\0")
        workspace_hash.update(diff)
        return {
            "ready": True,
            "frontend_repo": str(root),
            "commit": commit,
            "branch": branch,
            "dirty": bool(dirty_entries),
            "dirty_file_count": len(dirty_entries),
            "workspace_sha256": workspace_hash.hexdigest(),
            "bridge_path": relative_bridge.as_posix(),
        }

    def _call(self, payload: dict[str, Any], *, timeout_seconds: int = 60) -> dict[str, Any]:
        if not self.path.exists():
            raise CompilerError(f"ActionDesign compiler bridge was not found: {self.path}")
        process = subprocess.run(
            ["node", str(Path(__file__).with_name("compiler_runner.cjs")), str(self.path)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=self.config.frontend_repo,
            timeout=timeout_seconds,
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
            if result.get("command") != "describe_registry":
                raise CompilerError("Compiler registry response has an invalid command identity")
            if not isinstance(result.get("elements"), list) or not result["elements"]:
                raise CompilerError("Compiler registry did not return any ActionDesign elements")
            for field in ("rule_source_sha256",):
                if not re.fullmatch(r"[0-9a-f]{64}", str(result.get(field) or "").lower()):
                    raise CompilerError(f"Compiler registry field {field} is missing or invalid")
            result["registry_hash"] = self._stable_registry_hash(result)
            result["bridge_sha256"] = sha256_text(self.path.read_text(encoding="utf-8"))
            result["adapter_sha256"] = self._adapter_sha256()
            result["supported_operations"] = [
                "insert_before", "insert_after", "replace_params", "replace_block",
                "delete_block", "move_block", "create_action_group", "append_node", "bind_component_event",
            ]
            result["source_provenance"] = self.source_provenance()
            result["compiler_binding_sha256"] = sha256_json(self._binding_fields(result))
            self._registry = result
        return deepcopy(self._registry)

    @staticmethod
    def _adapter_sha256() -> str:
        return sha256_json({name: sha256_text(Path(__file__).with_name(name).read_text(encoding="utf-8"))
                            for name in ("compiler_runner.cjs", "canvas_operations.cjs")})

    @staticmethod
    def _binding_fields(registry: dict[str, Any]) -> dict[str, Any]:
        return compiler_binding_payload(registry)

    def compiler_binding(self) -> dict[str, Any]:
        registry = self.describe_registry()
        binding = self._binding_fields(registry)
        binding["compiler_binding_sha256"] = sha256_json(binding)
        return binding

    def _bind_registry_hash(self, result: dict[str, Any]) -> dict[str, Any]:
        registry = self.describe_registry()
        validation = result.get("validation")
        if not isinstance(validation, dict) or validation.get("valid") is not True:
            raise CompilerError("ActionDesign compiler did not confirm a valid canvas")
        for field in ("rule_source_sha256", "frontend_version"):
            if validation.get(field) != registry.get(field):
                raise CompilerError(f"Compiler validation {field} does not match the active registry")
        validation["registry_hash"] = registry["registry_hash"]
        result["compiler_binding"] = self.compiler_binding()
        return result

    @staticmethod
    def _validate_generated_artifacts(
        design: dict[str, Any],
        *,
        js_code: Any,
        csharp_code: Any,
        relation_data: Any,
    ) -> None:
        actions = design.get("actionData")
        variables = design.get("globalVariables")
        if not isinstance(actions, list) or not isinstance(variables, list):
            raise CompilerError("Compiler design must contain actionData and globalVariables arrays")
        if not isinstance(js_code, str) or not isinstance(csharp_code, str):
            raise CompilerError("Compiler must return both js_code and csharp_code string fields")
        if not isinstance(relation_data, dict):
            raise CompilerError("Compiler relation_data must be an object")
        action_types = {str(action.get("actionType") or "") for action in actions if isinstance(action, dict)}
        unknown = sorted(action_type for action_type in action_types if action_type not in {"js", "csharp"})
        if unknown:
            raise CompilerError("Compiler design contains unsupported action types: " + ", ".join(unknown))
        if "js" in action_types and not js_code.strip():
            raise CompilerError("Compiler generated no JavaScript for a canvas containing JS actions")
        if "csharp" in action_types and not csharp_code.strip():
            raise CompilerError("Compiler generated no C# for a canvas containing C# actions")
        if actions and not js_code.strip() and not csharp_code.strip():
            raise CompilerError("Compiler generated no executable code for a non-empty ActionDesign canvas")

    def verify_compiled_result(self, result: dict[str, Any]) -> dict[str, Any]:
        design = result.get("design")
        if not isinstance(design, dict):
            raise CompilerError("Compiler did not return a design object")
        for field in ("js_code", "csharp_code", "relation_data"):
            if field not in result:
                raise CompilerError(f"Compiler response is missing {field}")
        self._validate_generated_artifacts(
            design,
            js_code=result["js_code"],
            csharp_code=result["csharp_code"],
            relation_data=result["relation_data"],
        )
        revalidation = self.validate_design(design)
        if revalidation.get("validation") != result.get("validation"):
            raise CompilerError("Compiler result validation changed when the candidate canvas was revalidated")
        if revalidation.get("compiler_binding") != result.get("compiler_binding"):
            raise CompilerError("Compiler binding changed while verifying the candidate canvas")
        return {
            "validation": deepcopy(revalidation["validation"]),
            "compiler_binding": deepcopy(revalidation["compiler_binding"]),
            "code_generation_complete": True,
        }

    def verify_candidate_artifact(
        self,
        design: dict[str, Any],
        desired: dict[str, Any],
    ) -> dict[str, Any]:
        for field in ("js_code", "csharp_code", "relation_data"):
            if field not in desired:
                raise CompilerError(f"Candidate artifact is missing {field}")
        self._validate_generated_artifacts(
            design,
            js_code=desired["js_code"],
            csharp_code=desired["csharp_code"],
            relation_data=desired["relation_data"],
        )
        validation = self.validate_design(design)
        return {
            "validation": deepcopy(validation["validation"]),
            "compiler_binding": deepcopy(validation["compiler_binding"]),
            "code_generation_complete": True,
        }

    def validate_design(self, design: dict[str, Any]) -> dict[str, Any]:
        self._validate_editable_variable_values(design)
        result = self._call({"command": "validate_design", "design": design})
        return self._bind_registry_hash(result)

    @staticmethod
    def _validate_editable_variable_values(design: dict[str, Any]) -> None:
        """ParamInput displays value and recommits it as code on blur.

        Scope this contract to known ParamInput fields, rather than assuming all
        custom objects in arbitrary elements have the same editor behavior.
        """
        fields = {
            "SetVariable": (("inputParams", "variableValue", "变量值"),
                            ("outputParams", "variableName", "保存至变量")),
            "SetVariableValue": (("inputParams", "attributeValue", "属性值"),),
        }
        for group in design.get("actionData") or []:
            for line, node in enumerate(group.get("data") or [], 1):
                for section, field, title in fields.get(node.get("elementKey"), ()):
                    value = (node.get("paramsValue") or {}).get(section, {}).get(field)
                    if not isinstance(value, dict) or value.get("paramTypes") != "custom":
                        continue
                    if not isinstance(value.get("value"), str) or value.get("value") != value.get("code"):
                        location = f"{group.get('title') or group.get('key')} → 第 {line} 行 → {node.get('title') or node.get('elementKey')}"
                        raise CompilerError(
                            f"{location}：{title}的显示值 value 与生成表达式 code 不一致，"
                            "打开元件重新保存可能覆盖修改。请通过元件参数同步修改两者；已阻止生成审批。"
                        )

    def describe_component_contract(self, component_type: str) -> dict[str, Any]:
        result = self._call({"command": "describe_component_contract", "component_type": component_type})
        if result.get("command") != "describe_component_contract" or not isinstance(result.get("component"), dict):
            raise CompilerError("Compiler returned an invalid component contract")
        return {"component": result["component"], "compiler_binding": self.compiler_binding(),
                "component_instance_verified": False}

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

    def compile_reference_repair(self, repaired: dict[str, Any]) -> dict[str, Any]:
        """Compile only a completely resolved structured candidate, never the diagnostic projection."""
        design = repaired["design"]
        if scan_references(design, self.describe_registry())["summary"]["total"]:
            raise CompilerError("Repaired candidate still contains invalid variable bindings")
        self.validate_design(design)
        result = self.compile_patch(design, repaired["operations"])
        if scan_references(result["design"], self.describe_registry())["summary"]["total"]:
            raise CompilerError("Native parameter generation reintroduced invalid variable bindings")
        result["reference_repairs"] = deepcopy(repaired["repairs"])
        return result
