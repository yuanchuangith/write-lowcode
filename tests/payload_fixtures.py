"""Synthetic service fixture: local state/backup, no business IO or execution."""
from __future__ import annotations

import json
import tempfile
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from tests.helpers import baseline, editor_config
from tests.test_service import FakeCompiler, FakeDatabase, PlanRepository
from gxp_editor.backup import BackupStore
from gxp_editor.operation_preflight import pointer_tokens, same_json
from gxp_editor.runtime_validation import RuntimeCsharpValidator
from gxp_editor.service import EditorService
from gxp_editor.state import StateStore


class PayloadCompiler(FakeCompiler):
    def __init__(self, contract_padding=""):
        self.contract_padding = contract_padding
        self.seen_operations = []

    def describe_registry(self):
        return {**super().describe_registry(), "elements": [
            {"key": "Noop", "name": "Synthetic", "input_params": [{"key": "settings", "defaultValue": self.contract_padding}], "output_params": []},
            {"key": "If", "name": "Synthetic parent", "input_params": [], "output_params": [], "level_marker": True}],
            "supported_operations": ["set_parameter", "set_array_item"]}

    def validate_design(self, design):
        return {"validation": self.evidence()["validation"]}

    def evidence(self):
        binding = self.compiler_binding()
        return {"compiler_binding": binding, "validation": {"valid": True,
            "registry_hash": binding["registry_hash"], "rule_source_sha256": binding["rule_source_sha256"]},
            "event_binding_validation": {"binding_node_keys": [], "checks": [], "javascript_syntax_checked": True,
                                          "javascript_lexical_scope": "all_js_groups", "javascript_group_keys": ["main"]},
            "code_generation_complete": True, "generated_code_consistency_verified": True}

    def compile_patch(self, design, operations, **kwargs):
        self.seen_operations.append(deepcopy(operations))
        candidate = deepcopy(design)
        for operation in operations:
            group = next(group for group in candidate["actionData"] if group["key"] == operation["action_group_key"])
            node = next(node for node in group["data"] if node["key"] == operation["target_node_key"])
            parts = pointer_tokens(operation["path"])
            target = node["paramsValue"]
            for part in parts[:-1]:
                target = target[part]
            if operation["operation"] == "set_array_item":
                rows, selector = target[parts[-1]], operation["item_key"]
                index = next(i for i, row in enumerate(rows) if same_json(row.get(selector["field"]), selector["value"]))
                rows[index] = deepcopy(operation["value"])
            else:
                target[parts[-1]] = deepcopy(operation["value"])
        return {"design": candidate, "js_code": "function(inbiz){return {main:async function(){}}}",
                "csharp_code": "", "relation_data": {}, "diff": [{"before": design, "after": candidate}]}

    def verify_compiled_result(self, result, **kwargs):
        return self.evidence()

    def verify_candidate_artifact(self, design, desired, **kwargs):
        return self.evidence()


class PayloadHarness:
    """Real service phases and immutable artifacts; compiler is explicitly simulated."""
    def __init__(self, canvas=None, contract_padding=""):
        self.canvas = canvas or {"actionData": [{"key": "main", "title": "Main", "actionType": "js", "data": [
            {"key": "target", "title": "Target", "elementKey": "Noop", "depth": [],
             "paramsValue": {"inputParams": {"settings": {"flag": False, "notes": ""}}, "outputParams": {}}}]}], "globalVariables": []}
        self.compiler = PayloadCompiler(contract_padding)

    def __enter__(self):
        self.stack = ExitStack()
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="gxp-payload-")))
        self.config = editor_config()
        self.draft = baseline(data=json.dumps(self.canvas, ensure_ascii=False), csharp_code="", js_code="function(inbiz){return {main:async function(){}}}")
        self.draft["data_json"] = deepcopy(self.canvas)
        self.repository = PlanRepository(self.draft)
        self.repository.load_draft_by_identifier = lambda identifier: deepcopy(self.draft)
        self.service = object.__new__(EditorService)
        self.service.state = StateStore(self.root / "state.sqlite3")
        self.owner = self.service.state.create_conversation("synthetic payload")['conversation_id']
        self.service._conversation_id = self.owner
        runtime = RuntimeCsharpValidator(self.config)
        self.stack.enter_context(patch.object(self.service, "_components", return_value=(
            self.config, FakeDatabase(), self.repository, self.compiler, runtime)))
        self.stack.enter_context(patch("gxp_editor.service.approval_secret", return_value="synthetic-guard-secret"))
        self.stack.enter_context(patch("gxp_editor.service.runtime_root", return_value=self.root))
        self.stack.enter_context(patch("gxp_editor.service.BackupStore", side_effect=lambda **kw: BackupStore(self.root / "backups", **kw)))
        self.stack.enter_context(patch("gxp_editor.handoff_import.discover_handoffs", return_value={"items": [], "total": 0}))
        self.stack.enter_context(patch.object(self.service, "_try_launch_approval", return_value={
            "approval_window_opened": True, "approval_window_starting": False, "simulated": True}))
        return self

    def __exit__(self, *args):
        return self.stack.__exit__(*args)

    @staticmethod
    def spec():
        return {"schema_version": 1, "revision": "synthetic-1", "scope_policy": "strict",
            "requirements": [{"id": "change", "text": "Update the selected synthetic flag or row only",
                "source_text": "Update the selected synthetic flag or row only",
                "targets": [{"group_key": "main", "node_key": "target"}]}],
            "scenarios": {"schema_version": 1, "cases": []}}
