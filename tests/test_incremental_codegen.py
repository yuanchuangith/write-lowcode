"""Native offline checks: no database, approval window, or business save."""
from __future__ import annotations

import json
import os
import subprocess
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from tests.helpers import editor_config
from tests import test_codegen_consistency as codegen_helpers
from gxp_editor.compiler import CompilerBridge, CompilerError
from gxp_editor.service import EditorService
from gxp_editor.approval_display import generation_review_summary
from gxp_editor.approval import transaction_binding_hash
from gxp_editor.transaction import TransactionPlanError, validate_transaction_artifact
from tests import test_transaction as transaction_helpers

ROOT = Path(__file__).resolve().parents[1]


class IncrementalContractTests(unittest.TestCase):
    def test_approval_evidence_partition_and_old_findings_cannot_be_misrepresented(self):
        artifact = transaction_helpers.TransactionTests._canvas_transaction([
            {"key": "main", "actionType": "js", "data": []},
            {"key": "old", "actionType": "js", "data": []}])
        evidence = artifact["compiler_evidence"]
        generation = {"mode": "incremental_action_groups", "regenerated_group_keys": ["main"],
            "preserved_group_keys": ["old"], "preserved_source_verified": True,
            "baseline_sha256": "a" * 64, "candidate_sha256": "b" * 64}
        evidence["generation"] = generation
        proof = evidence["event_binding_validation"]
        proof["generation"] = deepcopy(generation)
        proof["baseline_diagnostics"] = [{"group_key": "old", "identifier": "missingContext", "status": "unverified", "blocks_changed_candidate": False}]
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        validate_transaction_artifact(artifact)
        display = generation_review_summary(evidence)
        self.assertIn("missingContext 未声明", display)
        self.assertIn("未验证", display)
        for field, value in (("preserved_group_keys", ["main", "old"]), ("regenerated_group_keys", []), ("baseline_sha256", "invalid")):
            broken = deepcopy(artifact)
            broken["compiler_evidence"]["event_binding_validation"]["generation"][field] = value
            broken["transaction_hash"] = transaction_binding_hash(broken)
            with self.subTest(field=field), self.assertRaises(TransactionPlanError):
                validate_transaction_artifact(broken)
        proof["baseline_diagnostics"][0]["status"] = "passed"
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        with self.assertRaisesRegex(TransactionPlanError, "must remain unverified"):
            validate_transaction_artifact(artifact)

    def test_saved_relations_are_taken_from_the_baseline_and_never_invented(self):
        saved = {"js_code": "old-js", "csharp_code": "old-csharp", "relations": [
            {"action_data_id": "node", "isDeleted": 0, "data": '{"original": true}'},
            {"action_data_id": "deleted", "isDeleted": 1, "data": "old"}]}
        self.assertEqual(EditorService._baseline_codegen_artifacts(saved), {
            "js_code": "old-js", "csharp_code": "old-csharp", "relation_data": {"node": '{"original": true}'}})
        saved["relations"].append(saved["relations"][0])
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            EditorService._baseline_codegen_artifacts(saved)

    def test_spans_and_dependency_scope(self):
        script = r'''
const assert = require('node:assert/strict'), inc = require(process.argv[1]);
const group = (key, data=[]) => ({key, actionType:'js', data});
const ref = key => ({key:'ref-'+key, paramsValue:{inputParams:{action:{value:key}}}});
const before = {actionData:[group('a'),group('b',[ref('a')]),group('c',[ref('b')]),group('other')]};
const after = structuredClone(before); after.actionData[0].title='changed';
assert.deepEqual(inc.scope(before,after).keys,['a','b','c']);
assert.deepEqual(inc.scope(before,after).preserved,['other']);
const normalized = structuredClone(before); normalized.actionData.forEach(g=>g.data.forEach(n=>n.depth=[]));
assert.deepEqual(inc.scope(before,normalized).keys,[]);
const source = 'namespace Demo { public class Demo { public Demo() {}\n // a\n public async Task<Dictionary<string, object>> a() { var text=@"} public async Task<Dictionary<string, object>> fake() {"; return null; }\n // b\n public async Task<Dictionary<string, object>> b() { /* } */ return null; } } }';
const spans = inc.csharpSpans(source,[{key:'a'},{key:'b'}]);
assert.equal(spans.spans.size,2);
assert.match(source.slice(spans.spans.get('a').start,spans.spans.get('a').end),/return null; }$/);
assert.throws(()=>inc.csharpSpans(source,[{key:'a'},{key:'missing'}]),/do not match/);
assert.equal(inc.csharpSpans(source.replace('return null;', 'var s=$"{new [] { "}" }[0]}"; return null;'),[{key:'a'},{key:'b'}]).spans.size,2);
assert.throws(()=>inc.csharpSpans(source.replace('return null;', 'var s="""raw"""; return null;'),[{key:'a'},{key:'b'}]),/source map/);
'''
        result = subprocess.run(["node", "-e", script, str(ROOT / "mcp/gxp_editor/incremental_codegen.cjs")],
                                capture_output=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"), "Native compiler path required")
class NativeIncrementalCodegenTests(unittest.TestCase):
    def setUp(self):
        path = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve()
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(path.parents[2]), compiler_bridge=str(path)))
        self.design = {"code": "IncrementalExample", "actionData": [
            {"key": "main", "title": "Changed button", "actionType": "js", "data": []},
            {"key": "legacy", "title": "Untouched legacy", "actionType": "js", "data": []},
            {"key": "backend", "title": "Backend", "actionType": "csharp", "data": []},
            {"key": "otherBackend", "title": "Other backend", "actionType": "csharp", "data": []}], "globalVariables": []}
        self.baseline = self.bridge._call({"command": "compile_design", "design": self.design})

    variable = staticmethod(codegen_helpers.NativeCodegenConsistencyTests.variable)

    def compile(self, operation, design=None, baseline=None):
        design = self.design if design is None else design
        baseline = self.baseline if baseline is None else baseline
        saved = {k: deepcopy(baseline[k]) for k in ("js_code", "csharp_code", "relation_data")}
        result = self.bridge.compile_patch(design, [operation], baseline_artifacts=saved, generation_mode="incremental")
        evidence = self.bridge.verify_compiled_result(result, baseline_design=design, baseline_artifacts=saved)
        self.assertTrue(evidence["generated_code_consistency_verified"])
        self.assertEqual(evidence["generation"], result["generation"])
        return result, saved

    def test_js_change_preserves_old_eval_and_undeclared_legacy_without_execution(self):
        # Compile source only. The legacy action must never be executed.
        full = self.bridge._call({"command": "compile_patch", "design": self.design, "operations": [
            self.variable("requestResult", '({Data:"[]"})', group="legacy"),
            self.variable("rows", "eval(requestResult.Data)", group="legacy")]})
        design = full["design"]
        # Saved legacy source can depend on a host-module binding. Preserve it
        # exactly, report it unverified, and do not add it to the global allowlist.
        saved = {k: deepcopy(full[k]) for k in ("js_code", "csharp_code", "relation_data")}
        saved["js_code"] = saved["js_code"].replace("eval(requestResult.Data)", "eval(requestResult.Data); missingContext.value")
        result, _ = self.compile(self.variable("value", '"new"'), design, saved)
        self.assertEqual(result["generation"]["regenerated_group_keys"], ["main"])
        self.assertEqual(result["csharp_code"], saved["csharp_code"])
        self.assertIn("eval(requestResult.Data); missingContext.value", result["js_code"])
        self.assertEqual(result["event_binding_validation"]["baseline_diagnostics"][0]["status"], "unverified")
        self.assertEqual(result["event_binding_validation"]["baseline_diagnostics"][0]["identifier"], "missingContext")
        with self.assertRaisesRegex(CompilerError, "Undeclared action identifier missingContext"):
            self.compile(self.variable("value", "eval(missingContext.value)"))

    def test_csharp_change_preserves_js_and_untouched_method(self):
        result, saved = self.compile(self.variable("value", '"new"', group="backend"))
        self.assertEqual(result["js_code"], saved["js_code"])
        self.assertEqual(result["generation"]["regenerated_group_keys"], ["backend"])
        self.assertIn('var value = Convert.ToString("new");', result["csharp_code"])
        self.assertEqual(result["csharp_code"].split("// Other backend")[1], saved["csharp_code"].split("// Other backend")[1])

    def test_new_group_is_generated_and_other_groups_remain_saved(self):
        operation = {"operation": "create_action_group", "group_alias": "created", "group_title": "New callback", "action_type": "js", "input_params": [], "output_params": []}
        result, saved = self.compile(operation)
        self.assertEqual(len(result["generation"]["regenerated_group_keys"]), 1)
        self.assertEqual(result["csharp_code"], saved["csharp_code"])
        self.assertIn("New callback", result["js_code"])

    def test_tampering_with_preserved_or_generated_source_or_relations_is_rejected(self):
        result, saved = self.compile(self.variable("value", '"new"'))
        for field, value in (("js_code", result["js_code"].replace('"new"', '"tampered"')),
                             ("csharp_code", result["csharp_code"] + "// tampered"),
                             ("relation_data", {"invented": "value"})):
            desired = {k: deepcopy(result[k]) for k in ("js_code", "csharp_code", "relation_data")}
            desired[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(CompilerError, "does not match"):
                self.bridge.verify_candidate_artifact(result["design"], desired,
                    baseline_design=self.design, baseline_artifacts=saved, generation_mode="incremental")

    def test_reserved_names_and_platform_mount_writes_still_block(self):
        for name, expression in (("inbiz", "1"), ("Inbiz", "1"), ("value", "(window.inbiz = {})")):
            with self.subTest(name=name, expression=expression), self.assertRaisesRegex(CompilerError, "Reserved platform"):
                self.compile(self.variable(name, expression))

    def test_relations_for_untouched_nodes_are_preserved(self):
        self.baseline["relation_data"] = {"legacy-relation": '{"keep":true}'}
        result, _ = self.compile(self.variable("value", '"new"'))
        self.assertEqual(result["relation_data"], self.baseline["relation_data"])

    def test_changed_dependency_regenerates_caller_and_retains_full_context(self):
        call = {"operation": "append_node", "action_group_key": "main", "element_key": "CallAction",
                "input_params": {"actionName": {"value": "legacy", "label": "Untouched legacy", "actionType": "js"}}}
        before = self.bridge.compile_patch(self.design, [call])
        result, _ = self.compile(self.variable("value", '"new"', group="legacy"), before["design"], before)
        self.assertEqual(result["generation"]["regenerated_group_keys"], ["main", "legacy"])
        self.assertIn("await self.legacy(", result["js_code"])

    def test_unmappable_saved_source_does_not_fall_back_or_create_code(self):
        self.baseline["js_code"] = "function(inbiz,relatedAttributes) { var self={}; return self; }"
        with self.assertRaisesRegex(CompilerError, "saved JS groups do not match"):
            self.compile(self.variable("value", '"new"'))


if __name__ == "__main__":
    unittest.main()
