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
    def __init__(self, message: str, *, details: Any = None):
        super().__init__(message)
        self.details = self._bounded_details(details)

    @staticmethod
    def _bounded_details(details: Any) -> dict[str, Any]:
        if not isinstance(details, dict):
            return {}
        index = details.get("operation_index")
        result = {"operation_index": index if type(index) is int and 0 <= index < 100 else None}
        for field in ("group_key", "node_key", "path"):
            value = details.get(field)
            result[field] = value[:256] if isinstance(value, str) else None
        reason = details.get("reason_code")
        result["reason_code"] = reason if isinstance(reason, str) and re.fullmatch(r"[a-z_]{1,64}", reason) else "operation_rejected"
        return result


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
        source_hash = self._verify_native_sources()
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
                raise CompilerError(str(failure["error"]), details=failure.get("details"))
            raise CompilerError((process.stderr or process.stdout or "compiler failed").strip())
        try:
            result = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise CompilerError(f"Compiler returned invalid JSON: {exc}") from exc
        if not result.get("ok"):
            raise CompilerError(str(result.get("error") or "compiler rejected request"), details=result.get("details"))
        evidence = result.get("validation", result)
        if source_hash and "rule_source_sha256" in evidence and evidence["rule_source_sha256"] != source_hash:
            raise CompilerError("Compiler rule hash does not match its embedded source manifest; rebuild the bridge")
        return result

    def _verify_native_sources(self) -> str:
        """Reject stale native rules before a session can report compiler readiness."""
        frontend_repo = self.frontend_repo
        rules_root = frontend_repo / "src/core/common/ActionDesign"
        rebuild = 'Run node scripts/build_compiler_bridge.cjs "' + str(frontend_repo) + '" before starting a new session'
        prefix = "// GXP_COMPILER_SOURCES_V1="
        try:
            with self.path.open(encoding="utf-8") as stream:
                header = stream.readline()
            if not header.startswith(prefix):
                raise CompilerError("Legacy compiler bridge has no verified native source manifest. " + rebuild)
            if not rules_root.is_dir():
                raise ValueError("native rule directory is missing")
            manifest = json.loads(header[len(prefix):])
            entries = manifest["files"]
            if manifest.get("schema_version") != 1 or not isinstance(entries, list) or not entries:
                raise ValueError("invalid manifest schema")
            seen = set()
            rule_entries = []
            for item in entries:
                relative = item["path"]
                file = (frontend_repo / relative).resolve()
                if file == frontend_repo or not file.is_relative_to(frontend_repo) or relative in seen:
                    raise ValueError("invalid or duplicate source path")
                seen.add(relative)
                if hashlib.sha256(file.read_bytes()).hexdigest() != item["sha256"]:
                    raise CompilerError(f"Compiler native source changed: {relative}. {rebuild}")
                if file.is_relative_to(rules_root) and not file.is_relative_to(rules_root / "compiler"):
                    rule_entries.append(item)
            current_rules = {file.relative_to(frontend_repo).as_posix()
                             for file in rules_root.rglob("*") if file.is_file()
                             and re.search(r"\.[cm]?[jt]sx?$", file.name)
                             and not file.is_relative_to(rules_root / "compiler")}
            if current_rules != {item["path"] for item in rule_entries}:
                raise CompilerError("Compiler native rule file set changed. " + rebuild)
            actual = hashlib.sha256(json.dumps(rule_entries, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()
            if actual != manifest["rule_source_sha256"]:
                raise ValueError("rule source hash mismatch")
            return actual
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise CompilerError(f"Invalid compiler native source manifest: {exc}. {rebuild}") from exc

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
        if self._registry is not None:
            source_hash = self._verify_native_sources()
            if (source_hash != self._registry["rule_source_sha256"]
                    or sha256_text(self.path.read_text(encoding="utf-8")) != self._registry["bridge_sha256"]):
                raise CompilerError("Compiler bridge changed; start a new validated compiler session")
            if self._adapter_sha256() != self._registry["adapter_sha256"]:
                raise CompilerError("Compiler adapter changed; start a new validated compiler session")
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
                "delete_block", "move_block", "create_action_group", "set_event_callback_contract",
                "append_node", "insert_in_block", "bind_component_event", "set_parameter", "set_array_item",
            ]
            result["source_provenance"] = self.source_provenance()
            result["compiler_binding_sha256"] = sha256_json(self._binding_fields(result))
            self._registry = result
        return deepcopy(self._registry)

    @staticmethod
    def _adapter_sha256() -> str:
        return sha256_json({name: sha256_text(Path(__file__).with_name(name).read_text(encoding="utf-8"))
                            for name in ("compiler_runner.cjs", "canvas_operations.cjs", "event_contracts.cjs",
                                         "event_contracts.json", "event_binding_checks.cjs", "interface_metadata.cjs")})

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
        binding = self._binding_fields(registry)
        binding["compiler_binding_sha256"] = sha256_json(binding)
        result["compiler_binding"] = binding
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

    def _verify_codegen_consistency(self, design: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
        """Regenerate with the same native compiler instead of trusting code hashes alone."""
        self._validate_editable_variable_values(design)
        regenerated = self._bind_registry_hash(self._call({"command": "compile_design", "design": design}))
        if regenerated.get("command") != "compile_design":
            raise CompilerError("Native code regeneration returned an invalid command identity")
        return self._compare_codegen_artifacts(design, artifacts, regenerated)

    def _compare_codegen_artifacts(self, design, artifacts, regenerated):
        self._validate_generated_artifacts(
            design, js_code=regenerated.get("js_code"), csharp_code=regenerated.get("csharp_code"),
            relation_data=regenerated.get("relation_data"),
        )
        for field in ("js_code", "csharp_code", "relation_data"):
            if regenerated[field] != artifacts.get(field):
                raise CompilerError(
                    f"Generated {field} does not match the candidate canvas; regenerate the plan"
                )
        if "interface_metadata" in artifacts and regenerated.get("interface_metadata") != artifacts["interface_metadata"]:
            raise CompilerError("Saved interface metadata changed during independent code generation")
        return regenerated

    def _verify_candidate(self, design, artifacts, *, baseline_design=None, binding_node_keys=None):
        """One new process rechecks events and independently generates all artifacts."""
        self._validate_editable_variable_values(design)
        payload = {"command": "verify_candidate", "design": design, "js_code": artifacts["js_code"]}
        if baseline_design is not None:
            payload["baseline"] = baseline_design
        else:
            payload["binding_node_keys"] = binding_node_keys
        regenerated = self._bind_registry_hash(self._call(payload))
        if regenerated.get("command") != "verify_candidate":
            raise CompilerError("Native code regeneration returned an invalid command identity")
        if not isinstance(regenerated.get("event_binding_validation"), dict):
            raise CompilerError("Event binding validation evidence is missing; regenerate the plan")
        return self._compare_codegen_artifacts(design, artifacts, regenerated)

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
        event_validation = result.get("event_binding_validation")
        if not isinstance(event_validation, dict) or not isinstance(event_validation.get("binding_node_keys"), list):
            raise CompilerError("Event binding validation evidence is missing; regenerate the plan")
        revalidation = self._verify_candidate(design, result, binding_node_keys=event_validation["binding_node_keys"])
        if revalidation["event_binding_validation"] != event_validation:
            raise CompilerError("Event binding validation evidence changed")
        if revalidation.get("validation") != result.get("validation"):
            raise CompilerError("Compiler result validation changed when the candidate canvas was revalidated")
        if revalidation.get("compiler_binding") != result.get("compiler_binding"):
            raise CompilerError("Compiler binding changed while verifying the candidate canvas")
        return {
            "event_binding_validation": event_validation,
            "validation": deepcopy(revalidation["validation"]),
            "compiler_binding": deepcopy(revalidation["compiler_binding"]),
            "code_generation_complete": True,
            "generated_code_consistency_verified": True,
            **({"interface_metadata": deepcopy(revalidation["interface_metadata"])}
               if revalidation.get("interface_metadata", {}).get("interface_count") else {}),
        }

    def verify_candidate_artifact(
        self,
        design: dict[str, Any],
        desired: dict[str, Any],
        *, baseline_design: dict[str, Any] | None = None,
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
        validation = self._verify_candidate(design, desired, baseline_design=baseline_design or design)
        event_validation = validation["event_binding_validation"]
        return {
            "event_binding_validation": event_validation,
            "validation": deepcopy(validation["validation"]),
            "compiler_binding": deepcopy(validation["compiler_binding"]),
            "code_generation_complete": True,
            "generated_code_consistency_verified": True,
            **({"interface_metadata": deepcopy(validation["interface_metadata"])}
               if validation.get("interface_metadata", {}).get("interface_count") else {}),
        }

    def verify_event_binding_validation(self, design, js_code, evidence):
        if not isinstance(evidence, dict) or not isinstance(evidence.get("binding_node_keys"), list):
            raise CompilerError("Event binding validation evidence is missing; regenerate the plan")
        checked = self._call({"command": "validate_event_bindings", "design": design,
                              "js_code": js_code, "binding_node_keys": evidence["binding_node_keys"]})["event_binding_validation"]
        if checked != evidence:
            raise CompilerError("Event binding validation evidence changed")
        return checked

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
        for index, operation in enumerate(operations):
            overlap = forbidden.intersection(operation)
            if overlap:
                raise CompilerError("Canvas operations contain forbidden fields: " + ", ".join(sorted(overlap)),
                    details={"operation_index": index, "group_key": operation.get("action_group_key"),
                             "node_key": operation.get("target_node_key") or operation.get("anchor_node_key"),
                             "path": operation.get("path"), "reason_code": "invalid_operation"})
        result = self._call({"command": "compile_patch", "design": design, "operations": operations})
        return self._bind_registry_hash(result)

    @staticmethod
    def guarded_no_changes(design: dict[str, Any], operations: list[dict[str, Any]]) -> bool:
        """Recognize only fully checked, existing-value set_parameter batches.

        This never validates a candidate or supplies compiler evidence. All other
        operation shapes fall through to the real compiler.
        """
        if not isinstance(operations, list) or not operations or len(operations) > 100 or any(
            not isinstance(op, dict) or op.get("operation") != "set_parameter" for op in operations
        ):
            return False
        try:
            if len(json.dumps(operations, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > 2 * 1024 * 1024:
                return False
        except (TypeError, ValueError, RecursionError):
            return False
        allowed = {"operation", "action_group_key", "target_node_key", "expected_element_key", "path", "expected_old_value", "value"}
        unsafe = {"__proto__", "constructor", "prototype"}

        def same(left, right):
            if type(left) is not type(right):
                return type(left) in (int, float) and type(right) in (int, float) and left == right
            if isinstance(left, dict):
                return left.keys() == right.keys() and all(same(left[key], right[key]) for key in left)
            if isinstance(left, list):
                return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
            return left == right

        def safe(value):
            if isinstance(value, dict):
                return all(isinstance(key, str) and key not in unsafe and safe(item) for key, item in value.items())
            if isinstance(value, list):
                return all(safe(item) for item in value)
            if value is None or type(value) in (str, bool, int):
                return True
            return type(value) is float and value not in (float("inf"), float("-inf")) and value == value

        for index, op in enumerate(operations):
            def reject(reason, message):
                raise CompilerError(message, details={"operation_index": index, "group_key": op.get("action_group_key"),
                    "node_key": op.get("target_node_key"), "path": op.get("path"), "reason_code": reason})

            if set(op) != allowed or any(not isinstance(op.get(field), str) or not op[field].strip()
                                        for field in ("action_group_key", "target_node_key", "expected_element_key", "path")):
                reject("invalid_operation", "set_parameter requires its exact identity, path, old value and value fields")
            if op["action_group_key"].startswith("@"):
                return False
            groups = [group for group in design.get("actionData") or [] if isinstance(group, dict) and group.get("key") == op["action_group_key"]]
            if len(groups) != 1:
                reject("ambiguous_target" if groups else "target_not_found", "set_parameter requires an unambiguous group and target node")
            nodes = [node for node in groups[0].get("data") or [] if isinstance(node, dict) and node.get("key") == op["target_node_key"]]
            if len(nodes) != 1:
                reject("ambiguous_target" if nodes else "target_not_found", "set_parameter requires an unambiguous group and target node")
            node = nodes[0]
            if node.get("elementKey") != op["expected_element_key"]:
                reject("element_type_mismatch", "Target element type changed; inspect the current node")
            path = op["path"]
            if not path.startswith("/") or re.search(r"~(?![01])", path):
                reject("invalid_parameter_path", "path must be a valid JSON pointer")
            parts = [part.replace("~1", "/").replace("~0", "~") for part in path[1:].split("/")]
            if len(parts) < 2 or parts[0] not in {"inputParams", "outputParams"} or unsafe.intersection(parts):
                reject("invalid_parameter_path", "path must select an existing safe inputParams/outputParams parameter")
            actual = node.get("paramsValue")
            if not isinstance(actual, dict) or any(section in actual and not isinstance(actual[section], dict)
                                                   for section in ("inputParams", "outputParams")):
                reject("invalid_parameter_value", "Target must contain input/output parameter dictionaries")
            for part in parts:
                if isinstance(actual, list):
                    reject("array_index_forbidden", "Array indexes are unsupported; use a unique native row key or submit the complete array parameter")
                if not isinstance(actual, dict) or part not in actual:
                    reject("parameter_path_missing", "Parameter path does not exist")
                actual = actual[part]
            if not safe(op["expected_old_value"]) or not safe(op["value"]):
                reject("invalid_parameter_value", "Parameter values must contain finite safe JSON data")
            if not same(actual, op["expected_old_value"]):
                reject("old_value_mismatch", "Parameter old value changed; inspect the current node")
            if not same(actual, op["value"]):
                # Later operations may depend on this change: leave the batch to native sequential validation.
                return False
            known = {"SetVariable": {("inputParams", "variableValue"), ("outputParams", "variableName")},
                     "SetVariableValue": {("inputParams", "attributeValue")}}
            if tuple(parts[:2]) in known.get(node.get("elementKey"), set()) and len(parts) != 2:
                reject("paraminput_record_required", "Submit the complete ParamInput record so value/code stay synchronized")
            try:
                CompilerBridge._validate_editable_variable_values({"actionData": [{**groups[0], "data": [node]}]})
            except CompilerError as exc:
                reject("paraminput_unsynchronized", str(exc))
        return True

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
