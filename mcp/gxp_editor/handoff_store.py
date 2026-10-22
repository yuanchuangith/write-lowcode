"""Versioned local design-evidence interchange shared by Look and Write.

This module has no database/editor dependencies. A checksum is integrity evidence,
never authorization, and records intentionally contain no generated source.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
MAX_BYTES = 256 * 1024
MAX_TARGETS = 10
_ID = re.compile(r"[0-9a-f]{32}\Z")
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_FORBIDDEN = {"password", "passwd", "credential", "credentials", "secret", "access_token", "refresh_token", "api_key", "connection_string", "authorization", "business_rows", "query_results", "csharp_code", "js_code", "generated_code"}


class HandoffError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _finite(value: Any, depth: int = 0) -> None:
    if depth > 40:
        raise HandoffError("HANDOFF_LIMIT", "Handoff JSON nesting exceeds 40 levels")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, str):
        if len(value.encode("utf-8")) > 64 * 1024:
            raise HandoffError("HANDOFF_LIMIT", "One handoff string exceeds 64 KiB")
        return
    if isinstance(value, list):
        if len(value) > 2000:
            raise HandoffError("HANDOFF_LIMIT", "Handoff collection exceeds 2000 items")
        for item in value:
            _finite(item, depth + 1)
        return
    if isinstance(value, dict):
        if len(value) > 2000 or any(not isinstance(key, str) for key in value):
            raise HandoffError("HANDOFF_FORMAT", "Handoff object keys must be bounded strings")
        for key, item in value.items():
            if key.lower() in _FORBIDDEN and item not in (None, "", [], {}):
                raise HandoffError("HANDOFF_PRIVATE_DATA", "Credentials, business query results and generated code cannot be handed off")
            _finite(item, depth + 1)
        return
    raise HandoffError("HANDOFF_FORMAT", "Handoff values must be finite JSON")


def canonical_bytes(value: Any) -> bytes:
    _finite(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def environment_scope(config: Any) -> str:
    def field(name: str) -> Any:
        return config.get(name) if isinstance(config, dict) else getattr(config, name, None)
    host, port, database = field("host"), field("port"), field("database")
    if not isinstance(host, str) or not host.strip() or not isinstance(database, str) or not database.strip():
        raise HandoffError("HANDOFF_ENVIRONMENT", "Database destination is unavailable")
    try:
        port = int(port)
    except (ValueError, TypeError) as exc:
        raise HandoffError("HANDOFF_ENVIRONMENT", "Database destination port is unavailable") from exc
    if not 1 <= port <= 65535:
        raise HandoffError("HANDOFF_ENVIRONMENT", "Database destination port is invalid")
    return canonical_sha256({"host": host.strip().lower(), "port": port, "database": database.strip()})


def normalize_targets(targets: Any) -> list[dict[str, str]]:
    if not isinstance(targets, list) or not 1 <= len(targets) <= MAX_TARGETS:
        raise HandoffError("HANDOFF_TARGETS", "Select 1-10 exact group_key/node_key targets")
    result, seen = [], set()
    for item in targets:
        if not isinstance(item, dict) or set(item) != {"group_key", "node_key"}:
            raise HandoffError("HANDOFF_TARGETS", "Each target requires only group_key and node_key")
        if any(not isinstance(item[key], str) or not item[key] or len(item[key]) > 200 for key in item):
            raise HandoffError("HANDOFF_TARGETS", "Target identifiers must be 1-200 characters")
        identity = (item["group_key"], item["node_key"])
        if identity in seen:
            raise HandoffError("HANDOFF_TARGETS", "Duplicate handoff target")
        seen.add(identity)
        result.append(dict(item))
    return result


def snapshot_targets(data: dict[str, Any], targets: Any, *, allow_missing: bool = False) -> list[dict[str, Any]]:
    """Capture exact parameters and raw structural guards without running code."""
    result = []
    for target in normalize_targets(targets):
        groups = [group for group in data.get("actionData", []) if isinstance(group, dict) and str(group.get("key", "")) == target["group_key"]]
        if len(groups) > 1:
            raise HandoffError("HANDOFF_AMBIGUOUS_TARGET", "Group key is ambiguous")
        nodes = groups[0].get("data", []) if groups else []
        matches = [(index, node) for index, node in enumerate(nodes) if isinstance(node, dict) and str(node.get("key", "")) == target["node_key"]]
        if len(matches) != 1:
            if not matches and allow_missing:
                result.append({**target, "missing": True})
                continue
            raise HandoffError("HANDOFF_TARGET_NOT_FOUND", "Target node is missing or ambiguous; re-read its exact location")
        index, node = matches[0]
        def identity(item: Any) -> Any:
            if not isinstance(item, dict):
                return None
            return {"node_key": str(item.get("key", "")), "element_key": str(item.get("elementKey", "")), "title": str(item.get("title", ""))}
        parents = []
        for key in node.get("depth", []) or []:
            candidates = [item for item in nodes if isinstance(item, dict) and str(item.get("key", "")) == str(key)]
            parents.append({**(identity(candidates[0]) if len(candidates) == 1 else {"node_key": str(key), "unresolved": True}), "params_sha256": canonical_sha256(candidates[0].get("paramsValue", {})) if len(candidates) == 1 else None})
        structure = [{"key": item.get("key"), "elementKey": item.get("elementKey"), "depth": item.get("depth", [])} for item in nodes if isinstance(item, dict)]
        # Flat marker canvases may omit depth even when a preceding condition
        # controls this node. Guard the complete group context rather than
        # guessing marker types or treating raw ancestry as semantic evidence.
        # This intentionally refreshes evidence for unrelated same-group edits.
        group_context = {"configuration": {key: value for key, value in groups[0].items() if key != "data"}, "nodes": nodes}
        snapshot = {
            **target, "element_key": str(node.get("elementKey", "")), "title": str(node.get("title", "")),
            "paramsValue": copy.deepcopy(node.get("paramsValue", {})), "depth": copy.deepcopy(node.get("depth", []) or []),
            "parent_path": parents, "previous_node": identity(nodes[index - 1]) if index else None,
            "next_node": identity(nodes[index + 1]) if index + 1 < len(nodes) else None,
            "control_flow": {"group_structure_sha256": canonical_sha256(structure), "group_context_sha256": canonical_sha256(group_context)},
        }
        snapshot["node_sha256"] = canonical_sha256(snapshot)
        result.append(snapshot)
    return result


def compare_targets(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    differences = []
    current = {(item["group_key"], item["node_key"]): item for item in after}
    for item in before:
        other = current.get((item["group_key"], item["node_key"]))
        fields = ["missing"] if not other else sorted(key for key in (set(item) | set(other)) - {"node_sha256", "group_key", "node_key"} if item.get(key) != other.get(key))
        if fields:
            differences.append({"group_key": item["group_key"], "node_key": item["node_key"], "changed_fields": fields})
    return differences


def _diagnosis_text(value: Any, field: str, *, identifier: bool = False) -> None:
    if not isinstance(value, str) or not value.strip():
        raise HandoffError("HANDOFF_CHANGE_SPEC", f"{field} must be nonempty text")
    if len(value.encode("utf-8")) > 4096 or identifier and len(value) > 200:
        raise HandoffError("HANDOFF_LIMIT", f"{field} exceeds its text limit (4 KiB; issue_id 200 characters)")


def _validate_diagnosis(diagnosis: Any) -> None:
    """Validate reported findings without promoting them to verified facts."""
    _finite(diagnosis)
    if not isinstance(diagnosis, dict) or set(diagnosis) - {"issues", "excluded", "unresolved"}:
        raise HandoffError("HANDOFF_CHANGE_SPEC", "diagnosis accepts issues, excluded and unresolved only")
    issues = diagnosis.get("issues")
    if not isinstance(issues, list) or not 1 <= len(issues) <= 20:
        raise HandoffError("HANDOFF_CHANGE_SPEC", "diagnosis.issues must contain 1-20 issues")
    required_fields = {"issue_id", "status", "symptom", "evidence", "verification"}
    optional_fields = {"expected", "root_cause", "solution"}
    for issue in issues:
        if (not isinstance(issue, dict) or not required_fields <= set(issue)
                or set(issue) - required_fields - optional_fields):
            raise HandoffError("HANDOFF_CHANGE_SPEC", "Each issue requires issue_id, status, symptom, evidence and verification; expected, root_cause and solution are optional")
        _diagnosis_text(issue["issue_id"], "issue.issue_id", identifier=True)
        _diagnosis_text(issue["symptom"], "issue.symptom")
        for key in optional_fields:
            if key in issue:
                _diagnosis_text(issue[key], f"issue.{key}")
        if not isinstance(issue["status"], str) or issue["status"] not in {"confirmed", "candidate"}:
            raise HandoffError("HANDOFF_CHANGE_SPEC", "issue.status must be confirmed or candidate")
        if not isinstance(issue["verification"], str) or issue["verification"] not in {"static_confirmed", "runtime_verified", "pending"}:
            raise HandoffError("HANDOFF_CHANGE_SPEC", "issue.verification must be static_confirmed, runtime_verified or pending")
        evidence = issue["evidence"]
        if not isinstance(evidence, list) or not 1 <= len(evidence) <= 20:
            raise HandoffError("HANDOFF_CHANGE_SPEC", "issue.evidence must contain 1-20 evidence items")
        for item in evidence:
            if (not isinstance(item, dict) or not {"source", "finding"} <= set(item)
                    or set(item) - {"source", "finding", "group_key", "node_key", "design_id"}
                    or ("group_key" in item) != ("node_key" in item)):
                raise HandoffError("HANDOFF_CHANGE_SPEC", "Evidence requires source and finding; optional group_key/node_key must be paired, and design_id is optional")
            for key, value in item.items():
                _diagnosis_text(value, f"evidence.{key}")
    for key in ("excluded", "unresolved"):
        if key not in diagnosis:
            continue
        if not isinstance(diagnosis[key], list) or len(diagnosis[key]) > 50:
            raise HandoffError("HANDOFF_CHANGE_SPEC", f"diagnosis.{key} must be a list of at most 50 items")
        for value in diagnosis[key]:
            _diagnosis_text(value, f"diagnosis.{key}")


def validate_change_spec(spec: Any) -> dict[str, Any]:
    allowed = {"user_request", "summary", "changes", "preserve", "scenarios", "diagnosis"}
    if not isinstance(spec, dict) or set(spec) - allowed:
        raise HandoffError("HANDOFF_CHANGE_SPEC", "change_spec accepts user_request, summary, changes, preserve, scenarios and diagnosis only")
    if not isinstance(spec.get("user_request"), str) or not spec["user_request"].strip():
        raise HandoffError("HANDOFF_CHANGE_SPEC", "change_spec.user_request must preserve the user's actual request")
    if not isinstance(spec.get("summary"), str) or not spec["summary"].strip():
        raise HandoffError("HANDOFF_CHANGE_SPEC", "change_spec.summary must describe the evidence or proposed change")
    if len(spec["summary"].encode("utf-8")) > 2048 or len(spec["user_request"].encode("utf-8")) > 8192:
        raise HandoffError("HANDOFF_LIMIT", "Keep the summary within 2 KiB and user request within 8 KiB")
    for key in ("changes", "preserve", "scenarios"):
        if key in spec and (not isinstance(spec[key], list) or len(spec[key]) > 50):
            raise HandoffError("HANDOFF_CHANGE_SPEC", f"change_spec.{key} must be a list of at most 50 items")
    for item in spec.get("changes", []):
        if not isinstance(item, dict) or set(item) - {"group_key", "node_key", "description", "parameter_path", "before", "after"}:
            raise HandoffError("HANDOFF_CHANGE_SPEC", "Each change accepts group_key, node_key, description, parameter_path, before and after")
    for item in spec.get("preserve", []):
        if not isinstance(item, str):
            raise HandoffError("HANDOFF_CHANGE_SPEC", "Preserve entries must be text")
    for item in spec.get("scenarios", []):
        if not isinstance(item, (str, dict)) or isinstance(item, dict) and set(item) - {"name", "description", "expected"}:
            raise HandoffError("HANDOFF_CHANGE_SPEC", "Scenarios are text or objects with name, description and expected")
    if "diagnosis" in spec:
        _validate_diagnosis(spec["diagnosis"])
        for item in spec.get("changes", []):
            for key in ("group_key", "node_key", "description"):
                _diagnosis_text(item.get(key), f"change.{key}")
    _finite(spec)
    if len(canonical_bytes(spec)) > 32 * 1024:
        raise HandoffError("HANDOFF_LIMIT", "change_spec exceeds 32 KiB; narrow the evidence or proposed change scope")
    return copy.deepcopy(spec)


def default_root() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    base = Path(local) if local else Path.home() / ".local" / "share"
    return base / "GxpLowcodeHandoff" / "v1"


class HandoffStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root is not None else default_root()

    @staticmethod
    def _validate(record: Any) -> None:
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise HandoffError("HANDOFF_VERSION", "Unsupported handoff schema; use ordinary targeted draft inspection")
        allowed = {"schema_version", "handoff_id", "created_at", "environment_scope", "action", "draft", "diagnosis_source", "targets", "diagnosis_targets", "source_target_differences", "change_spec"}
        if set(record) != allowed or not _ID.fullmatch(str(record.get("handoff_id", ""))) or not _HASH.fullmatch(str(record.get("environment_scope", ""))):
            raise HandoffError("HANDOFF_FORMAT", "Invalid handoff record identity or fields")
        if not isinstance(record["action"], dict) or not record["action"].get("ref_id") or set(record["action"]) - {"ref_id", "action_code", "action_name", "action_type", "page_id"}:
            raise HandoffError("HANDOFF_FORMAT", "Invalid handoff action identity")
        for key in ("draft", "diagnosis_source"):
            item = record[key]
            if not isinstance(item, dict) or not item.get("design_id") or not _HASH.fullmatch(str(item.get("data_sha256", ""))) or set(item) - {"design_id", "data_sha256", "version"}:
                raise HandoffError("HANDOFF_FORMAT", "Invalid design evidence")
        if record["diagnosis_source"].get("version") not in {"draft", "published"}:
            raise HandoffError("HANDOFF_FORMAT", "Invalid diagnosis version")
        targets = record["targets"]
        normalize_targets([{"group_key": item.get("group_key"), "node_key": item.get("node_key")} for item in targets] if isinstance(targets, list) and all(isinstance(item, dict) for item in targets) else None)
        diagnosis = record["diagnosis_targets"]
        if not isinstance(diagnosis, list) or len(diagnosis) != len(targets):
            raise HandoffError("HANDOFF_FORMAT", "Diagnosis targets must correspond to draft targets")
        fields = {"group_key", "node_key", "element_key", "title", "paramsValue", "depth", "parent_path", "previous_node", "next_node", "control_flow", "node_sha256"}
        for expected, observed in zip(targets, diagnosis):
            if not isinstance(observed, dict) or any(expected[key] != observed.get(key) for key in ("group_key", "node_key")):
                raise HandoffError("HANDOFF_FORMAT", "Diagnosis target identity differs from the draft target")
            for snapshot in (expected, observed):
                if snapshot is observed and snapshot.get("missing") is True:
                    if set(snapshot) != {"group_key", "node_key", "missing"}:
                        raise HandoffError("HANDOFF_FORMAT", "Invalid missing diagnosis target")
                    continue
                if set(snapshot) != fields or snapshot["node_sha256"] != canonical_sha256({key: value for key, value in snapshot.items() if key != "node_sha256"}):
                    raise HandoffError("HANDOFF_FORMAT", "Target snapshot is incomplete or inconsistent")
                if not isinstance(snapshot["paramsValue"], dict) or not isinstance(snapshot["depth"], list) or not isinstance(snapshot["parent_path"], list):
                    raise HandoffError("HANDOFF_FORMAT", "Target parameter or ancestry shape is invalid")
        if record["source_target_differences"] != compare_targets(diagnosis, targets):
            raise HandoffError("HANDOFF_FORMAT", "Source-to-draft differences are inconsistent")
        if not isinstance(record["created_at"], str) or len(record["created_at"]) > 80:
            raise HandoffError("HANDOFF_FORMAT", "Invalid record creation time")
        validate_change_spec(record["change_spec"])
        _finite(record)

    @staticmethod
    def summary(record: dict[str, Any], checksum: str) -> dict[str, Any]:
        return {"handoff_id": record["handoff_id"], "sha256": checksum, "created_at": record["created_at"], "action": record["action"], "draft": record["draft"], "target_count": len(record["targets"]), "summary": record["change_spec"]["summary"]}

    def write(self, record: dict[str, Any]) -> dict[str, Any]:
        record = copy.deepcopy(record)
        record.update(schema_version=SCHEMA_VERSION, handoff_id=secrets.token_hex(16), created_at=datetime.now(timezone.utc).isoformat())
        self._validate(record)
        checksum = canonical_sha256(record)
        payload = canonical_bytes({"record": record, "sha256": checksum})
        if len(payload) > MAX_BYTES:
            raise HandoffError("HANDOFF_LIMIT", "Handoff exceeds 256 KiB; narrow the target set without truncating parameters")
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.root / (record["handoff_id"] + "." + secrets.token_hex(8) + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.root / (record["handoff_id"] + ".json"))
        finally:
            temporary.unlink(missing_ok=True)
        return {"handoff_id": record["handoff_id"], "sha256": checksum, "handoff_ref": {"handoff_id": record["handoff_id"], "sha256": checksum}, "summary": self.summary(record, checksum)}

    def read(self, handoff_id: str, sha256: str = "") -> dict[str, Any]:
        if not isinstance(handoff_id, str) or not _ID.fullmatch(handoff_id):
            raise HandoffError("HANDOFF_ID", "Use a 32-character lowercase hexadecimal handoff ID, never a path")
        if sha256 and (not isinstance(sha256, str) or not _HASH.fullmatch(sha256)):
            raise HandoffError("HANDOFF_CHECKSUM", "Expected checksum must be a SHA-256 digest")
        path = self.root / (handoff_id + ".json")
        try:
            if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                raise HandoffError("HANDOFF_LIMIT", "Handoff file is oversized or not a regular local record")
            with path.open("rb") as stream:
                payload = stream.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES:
                raise HandoffError("HANDOFF_LIMIT", "Handoff record grew beyond 256 KiB")
            envelope = json.loads(payload.decode("utf-8"))
            if not isinstance(envelope, dict) or set(envelope) != {"record", "sha256"}:
                raise HandoffError("HANDOFF_FORMAT", "Malformed handoff envelope")
            record = envelope["record"]
            self._validate(record)
            checksum = canonical_sha256(record)
            if record["handoff_id"] != handoff_id or envelope["sha256"] != checksum or sha256 and sha256 != checksum:
                raise HandoffError("HANDOFF_CHECKSUM", "Handoff integrity check failed; inspect the current draft directly")
            return record
        except FileNotFoundError as exc:
            raise HandoffError("HANDOFF_NOT_FOUND", "Handoff record is unavailable; inspect the current draft directly") from exc
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError, KeyError) as exc:
            raise HandoffError("HANDOFF_FORMAT", "Handoff record is corrupt; inspect the current draft directly") from exc

    def list(self, identifier: str, environment: str = "") -> list[dict[str, Any]]:
        if not isinstance(identifier, str) or not identifier.strip() or len(identifier) > 200:
            raise HandoffError("HANDOFF_ID", "An exact action code or RefId is required to list local handoffs")
        summaries = []
        if not self.root.exists():
            return summaries
        # Bound local scanning; callers must narrow explicitly rather than auto-pick.
        paths = sorted(self.root.glob("*.json"), key=lambda path: path.name)
        if len(paths) > 1000:
            raise HandoffError("HANDOFF_LIMIT", "Too many local records to list; provide the exact handoff ID")
        for path in paths:
            if not _ID.fullmatch(path.stem):
                continue
            try:
                record = self.read(path.stem)
            except (HandoffError, OSError):
                continue
            if identifier in {record["action"].get("ref_id"), record["action"].get("action_code")} and (not environment or record["environment_scope"] == environment):
                summaries.append(self.summary(record, canonical_sha256(record)))
        if len(summaries) > 50:
            raise HandoffError("HANDOFF_LIMIT", "More than 50 matching handoffs; provide an exact ID")
        return sorted(summaries, key=lambda item: item["created_at"], reverse=True)
