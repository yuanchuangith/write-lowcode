"""Offline native generation-mode checks; never open approval or write drafts."""
from __future__ import annotations

import os
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from tests.helpers import editor_config
from tests import test_codegen_consistency as codegen_helpers
from gxp_editor.compiler import CompilerBridge, CompilerError


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"), "Native compiler path required")
class NativeGenerationModeTests(unittest.TestCase):
    variable = staticmethod(codegen_helpers.NativeCodegenConsistencyTests.variable)

    @classmethod
    def setUpClass(cls):
        path = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve()
        cls.bridge = CompilerBridge(replace(
            editor_config(), frontend_repo=str(path.parents[2]), compiler_bridge=str(path)))
        empty = {
            "code": "GenerationModes",
            "actionData": [
                {"key": key, "title": key, "actionType": language, "data": []}
                for key, language in (("main", "js"), ("legacy", "js"),
                                      ("spareA", "js"), ("spareB", "js"),
                                      ("spareC", "js"), ("backend", "csharp"))
            ],
            "globalVariables": [],
        }
        native = cls.bridge._call({
            "command": "compile_patch", "design": empty,
            "operations": [cls.variable("legacyValue", '"canvas-legacy"', group="legacy")],
        })
        cls.design = native["design"]
        cls.saved = cls.artifacts(native)
        # Model saved, valid legacy source that differs from current generation.
        # It must survive only when incremental generation was actually selected.
        cls.saved["js_code"] = cls.saved["js_code"].replace('"canvas-legacy"', '"saved-legacy"')
        assert cls.saved["js_code"] != native["js_code"]

    @staticmethod
    def artifacts(result):
        return {key: deepcopy(result[key]) for key in ("js_code", "csharp_code", "relation_data")}

    def compile_and_verify(self, operations, *, mode=None, design=None, saved=None):
        design = self.design if design is None else design
        saved = self.saved if saved is None else saved
        options = {} if mode is None else {"generation_mode": mode}
        result = self.bridge.compile_patch(design, operations, baseline_artifacts=saved, **options)
        proof = self.bridge.verify_compiled_result(
            result, baseline_design=design, baseline_artifacts=saved, **options)
        self.assertTrue(proof["generated_code_consistency_verified"])
        self.assertEqual(proof["generation_selection"], result["generation_selection"])
        return result

    def assert_selection(self, result, requested, selected, reason):
        selection = result["generation_selection"]
        self.assertEqual(selection["requested_mode"], requested)
        self.assertEqual(selection["selected_mode"], selected)
        self.assertEqual(selection["reason_code"], reason)
        self.assertEqual(selection["policy_version"], "action_groups_v1")
        self.assertEqual(selection["counts"]["total_group_count"], 6)
        for field in ("baseline_sha256", "candidate_sha256"):
            self.assertRegex(selection[field], r"^[0-9a-f]{64}$")
        return selection

    def test_default_and_explicit_full_regenerate_saved_source(self):
        for mode in (None, "full"):
            with self.subTest(mode=mode):
                result = self.compile_and_verify([self.variable("changed", '"new"')], mode=mode)
                self.assert_selection(result, "full", "full", "explicit_full")
                self.assertIn('"canvas-legacy"', result["js_code"])
                self.assertNotIn('"saved-legacy"', result["js_code"])
                self.assertNotIn("generation", result)

    def test_explicit_incremental_preserves_saved_source_and_rechecks_artifact(self):
        result = self.compile_and_verify([self.variable("changed", '"new"')], mode="incremental")
        selection = self.assert_selection(result, "incremental", "incremental", "explicit_incremental")
        self.assertEqual(selection["counts"]["direct_changed_group_count"], 1)
        self.assertEqual(selection["counts"]["affected_group_count"], 1)
        self.assertEqual(selection["scope"]["affected_group_keys"], ["main"])
        self.assertIn("legacy", selection["scope"]["unaffected_group_keys"])
        self.assertIn('"saved-legacy"', result["js_code"])
        self.assertEqual(result["csharp_code"], self.saved["csharp_code"])
        desired = {**self.artifacts(result), "generation_selection": deepcopy(selection),
                   "generation": deepcopy(result["generation"])}
        proof = self.bridge.verify_candidate_artifact(
            result["design"], desired, baseline_design=self.design,
            baseline_artifacts=self.saved, generation_mode="incremental")
        self.assertEqual(proof["generation_selection"], selection)

    def test_auto_selects_incremental_for_a_small_independent_change(self):
        result = self.compile_and_verify([self.variable("changed", '"new"')], mode="auto")
        selection = self.assert_selection(result, "auto", "incremental", "small_change")
        self.assertEqual(selection["counts"]["affected_group_count"], 1)
        self.assertEqual(selection["counts"]["dependency_group_count"], 0)
        self.assertIn('"saved-legacy"', result["js_code"])

    def test_auto_selects_full_for_a_broad_direct_change(self):
        operations = [self.variable("changed", '"new"', group=group)
                      for group in ("main", "spareA", "spareB", "spareC")]
        result = self.compile_and_verify(operations, mode="auto")
        selection = self.assert_selection(result, "auto", "full", "affected_group_limit")
        self.assertEqual(selection["counts"]["direct_changed_group_count"], 4)
        self.assertEqual(selection["counts"]["affected_group_count"], 4)
        self.assertIn('"canvas-legacy"', result["js_code"])
        self.assertNotIn('"saved-legacy"', result["js_code"])

    def test_auto_counts_transitive_callers_before_selecting_full(self):
        calls = [
            {"operation": "append_node", "action_group_key": caller, "element_key": "CallAction",
             "input_params": {"actionName": {"value": callee, "label": callee, "actionType": "js"}}}
            for caller, callee in (("spareA", "legacy"), ("spareB", "spareA"), ("spareC", "spareB"))
        ]
        before = self.bridge.compile_patch(self.design, calls, generation_mode="full")
        saved = self.artifacts(before)
        saved["js_code"] = saved["js_code"].replace('"canvas-legacy"', '"saved-legacy"')
        result = self.compile_and_verify(
            [self.variable("another", '"changed"', group="legacy")], mode="auto",
            design=before["design"], saved=saved)
        selection = self.assert_selection(result, "auto", "full", "affected_group_limit")
        self.assertEqual(selection["counts"]["direct_changed_group_count"], 1)
        self.assertEqual(selection["counts"]["affected_group_count"], 4)
        self.assertEqual(selection["counts"]["dependency_group_count"], 3)
        self.assertIn('"canvas-legacy"', result["js_code"])

    def test_unknown_modes_are_rejected_and_incremental_does_not_fall_back(self):
        operations = [self.variable("changed", '"new"')]
        with self.assertRaisesRegex(CompilerError, "generation_mode"):
            self.bridge.compile_patch(self.design, operations, baseline_artifacts=self.saved,
                                      generation_mode="mostly_incremental")
        with self.assertRaisesRegex(CompilerError, "baseline"):
            self.bridge.compile_patch(self.design, operations, generation_mode="incremental")

    def test_independent_verification_rejects_tampered_mode_and_decision_evidence(self):
        result = self.compile_and_verify([self.variable("changed", '"new"')], mode="incremental")
        for field, value in (("selected_mode", "full"), ("reason_code", "explicit_full")):
            with self.subTest(field=field):
                tampered = deepcopy(result)
                tampered["generation_selection"][field] = value
                with self.assertRaisesRegex(CompilerError, "[Gg]eneration.*(selection|evidence)"):
                    self.bridge.verify_compiled_result(
                        tampered, baseline_design=self.design, baseline_artifacts=self.saved,
                        generation_mode="incremental")
        desired = {**self.artifacts(result),
                   "generation_selection": deepcopy(result["generation_selection"])}
        desired["generation_selection"]["counts"]["affected_group_count"] += 1
        with self.assertRaisesRegex(CompilerError, "[Gg]eneration.*(selection|evidence)"):
            self.bridge.verify_candidate_artifact(
                result["design"], desired, baseline_design=self.design,
                baseline_artifacts=self.saved, generation_mode="incremental")


if __name__ == "__main__":
    unittest.main()
