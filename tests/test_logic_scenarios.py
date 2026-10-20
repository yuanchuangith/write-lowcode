from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

from gxp_editor.logic_scenarios import LogicScenarioRunner, LogicScenarioSpecError, validate_scenario_spec


FRONTEND = os.environ.get("GXP_TEST_FRONTEND_REPO", r"G:\hoyi\updateComponents\gxp2.components")


def specification(*, steps=None, expect=None, environment=None):
    return {"schema_version": 1, "cases": [{"id": "case-one", "requirement_ids": ["requirement-one"],
        "environment": environment or {"page_mode": "add", "fields": {"result": {"value": "before"}}},
        "steps": steps or [{"kind": "call_group", "group_key": "main", "args": []}],
        "expect": {"fields": {"result": {"value": "after"}}} if expect is None else expect}]}


class LogicScenarioSpecTests(unittest.TestCase):
    def test_data_only_schema_preserves_requirement_links_and_does_not_mutate(self):
        spec = specification()
        normalized = validate_scenario_spec(spec)
        self.assertEqual(normalized, spec)
        normalized["cases"][0]["requirement_ids"].append("other")
        self.assertEqual(spec["cases"][0]["requirement_ids"], ["requirement-one"])

    def test_invalid_or_executable_fixture_is_rejected(self):
        examples = []
        item = specification(); item["cases"][0]["stub_js"] = "fetch('https://example.com')"; examples.append(item)
        item = specification(); item["cases"][0]["steps"][0]["kind"] = "eval"; examples.append(item)
        item = specification(); item["cases"][0]["environment"]["fields"]["result"]["value"] = float("nan"); examples.append(item)
        item = specification(); item["cases"].append(deepcopy(item["cases"][0])); examples.append(item)
        item = specification(); item["cases"][0]["requirement_ids"] *= 2; examples.append(item)
        item = specification(expect={"last_return": None, "last_return_undefined": True}); examples.append(item)
        item = specification(); item["cases"][0]["steps"] *= 11; examples.append(item)
        for spec in examples:
            with self.subTest(spec=spec), self.assertRaises(LogicScenarioSpecError):
                validate_scenario_spec(spec)

    def test_empty_spec_does_not_start_node(self):
        with patch("gxp_editor.logic_scenarios.subprocess.Popen") as start:
            evidence = LogicScenarioRunner().run_pair("", "", {"schema_version": 1, "cases": []})
        start.assert_not_called()
        self.assertFalse(evidence["business_scenarios_verified"])
        self.assertFalse(evidence["candidate_gate"]["blocked"])

    def test_unsafe_integer_fixtures_are_rejected_before_javascript_rounding(self):
        for value in (2**53, -(2**53), 2**53 + 1, float(2**53)):
            spec = specification(expect={"last_return": {"nested": [value]}})
            with self.subTest(value=value), self.assertRaisesRegex(LogicScenarioSpecError, "safe range"):
                validate_scenario_spec(spec)
        for value in (2**53 - 1, -(2**53 - 1), str(2**53 + 1), 1.5):
            spec = specification(expect={"last_return": {"nested": [value]}})
            self.assertEqual(validate_scenario_spec(spec), spec)

    def test_invalid_protocol_and_node_unavailable_are_unverified(self):
        runner = LogicScenarioRunner()
        with patch("gxp_editor.logic_scenarios.subprocess.Popen", side_effect=OSError("missing node")):
            report = runner.run_pair("function(){}", "function(){}", specification())
        self.assertEqual(report["summary"]["unverified"], 1)


@unittest.skipUnless(shutil.which("node") and os.path.isfile(os.path.join(FRONTEND, "package.json")), "Existing Node and frontend parser required")
class LogicScenarioExecutionTests(unittest.TestCase):
    def runner(self):
        return LogicScenarioRunner(SimpleNamespace(frontend_repo=FRONTEND))

    def run_case(self, source, spec=None, *, baseline=None):
        return self.runner().run_pair(source if baseline is None else baseline, source, spec or specification())

    def test_actual_generated_function_observes_page_mode_and_field_writes(self):
        source = 'function(inbiz){return {main:async function(){if(inbiz.getPageMode()==="add") inbiz("result").value="after";}};}'
        evidence = self.run_case(source)
        self.assertEqual(evidence["cases"][0]["candidate"]["status"], "passed", evidence["cases"][0]["candidate"])
        self.assertEqual(evidence["cases"][0]["candidate"]["actual"]["fields"]["result"]["value"], "after")
        self.assertEqual(evidence["cases"][0]["requirement_ids"], ["requirement-one"])
        self.assertFalse(evidence["runtime_verified"])

    def test_failed_baseline_does_not_prevent_independent_candidate_pass(self):
        source = 'function(inbiz){return {main:async function(){inbiz("result").value="after";}};}'
        report = self.run_case(source, baseline='function(){return {main:async function(){throw new Error("old defect")}}}')
        self.assertEqual(report["cases"][0]["baseline"]["status"], "failed")
        self.assertEqual(report["cases"][0]["candidate"]["status"], "passed")
        self.assertFalse(report["candidate_gate"]["blocked"])

    def test_wrong_candidate_and_supported_runtime_error_fail_gate(self):
        for source in ('function(inbiz){return {main:async function(){inbiz("result").value="wrong";}};}',
                       'function(){return {main:async function(){throw new Error("bad branch")}}}'):
            with self.subTest(source=source):
                report = self.run_case(source)
                self.assertEqual(report["cases"][0]["candidate"]["status"], "failed")
                self.assertTrue(report["candidate_gate"]["blocked"])

    def test_unsupported_api_stays_unverified_even_when_caught(self):
        source = 'function(inbiz){return {main:async function(){try {inbiz("result").save()}catch(e){} inbiz("result").value="after";}};}'
        report = self.run_case(source)
        self.assertEqual(report["cases"][0]["candidate"]["status"], "unverified")
        self.assertFalse(report["candidate_gate"]["blocked"])

    def test_network_and_dynamic_code_are_not_executed(self):
        for body in ('await fetch("https://example.com")', 'process.exit(0)', '({}).constructor.constructor("return process")()', 'import("node:fs")', 'const {random}=Math; random()'):
            with self.subTest(body=body):
                source = 'function(){return {main:async function(){' + body + '}}}'
                report = self.run_case(source)
                self.assertEqual(report["cases"][0]["candidate"]["status"], "unverified")
                self.assertEqual(report["cases"][0]["candidate"]["trace"], [])

    def test_sequential_metadata_retention_and_duplicate_keys_are_observable(self):
        source = '''function(inbiz){return {main:async function(){
          const callback=async function(action){if(action.id==="match") {
            const text=(inbiz("table").getSelectedRows() || []).map(row=>row.topic).join("、");
            inbiz.configEvent("getPageAddMetaData",()=>[{key:"topic",value:text},{key:"topic",value:"duplicate"}]);
          }}; inbiz("table").configEvent("click",callback);inbiz("table").on("click",callback);
        }}}'''
        steps = [{"kind": "call_group", "group_key": "main"},
                 {"kind": "emit_event", "component_id": "table", "event_name": "click", "args": [{"id": "match"}]},
                 {"kind": "emit_event", "component_id": "table", "event_name": "click", "args": [{"id": "other"}]},
                 {"kind": "read_metadata"}]
        spec = specification(steps=steps, environment={"fields": {"table": {"selected_rows": [{"topic": "甲"}, {"topic": "乙"}]}}},
                             expect={"metadata": [{"key": "topic", "value": "甲、乙"}, {"key": "topic", "value": "duplicate"}],
                                     "metadata_registrations": 1, "call_counts": {"table.getSelectedRows": 1}})
        result = self.run_case(source, spec)["cases"][0]["candidate"]
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["actual"]["metadata"]), 2)

    def test_inner_context_is_injected_only_through_declared_step(self):
        source = '''function(inbiz){return {main:async function(){inbiz("table").configEvent("click",async function(action){
          inbiz("result").value=action.innerInbiz("status").getText();
        });}}}'''
        spec = specification(environment={"fields": {"table": {}, "result": {"value": None}}, "child_fields": {"status": {"text": "合格"}}},
                             steps=[{"kind": "call_group", "group_key": "main"},
                                    {"kind": "emit_event", "component_id": "table", "event_name": "click", "args": [{}], "inject_inner_inbiz": True}],
                             expect={"fields": {"result": {"value": "合格"}}})
        self.assertEqual(self.run_case(source, spec)["cases"][0]["candidate"]["status"], "passed")

    def test_undefined_return_is_distinct_from_null_and_empty_expectation_is_unverified(self):
        source = 'function(){return {main:async function(){}}}'
        result = self.run_case(source, specification(expect={"last_return_undefined": True}))
        self.assertEqual(result["cases"][0]["candidate"]["status"], "passed")
        self.assertEqual(self.run_case(source, specification(expect={"last_return": None}))["cases"][0]["candidate"]["status"], "failed")
        self.assertEqual(self.run_case(source, specification(expect={}))["cases"][0]["candidate"]["status"], "unverified")

    def test_timeout_and_unavailable_parser_are_unverified(self):
        runner = self.runner(); runner.CASE_TIMEOUT_SECONDS = 0.15
        source = 'function(){return {main:async function(){while(true){}}}}'
        result = runner.run_pair(source, source, specification())
        self.assertEqual(result["cases"][0]["candidate"]["status"], "unverified")
        self.assertIn("timed out", result["cases"][0]["candidate"]["reason"])
        runner = LogicScenarioRunner(SimpleNamespace(frontend_repo=os.path.abspath("missing-parser-root")))
        report = runner.run_pair('function(){}', 'function(){}', specification())
        self.assertEqual(report["cases"][0]["candidate"]["status"], "unverified")

    def test_metadata_read_reflects_current_field_after_state_update(self):
        source = '''function(inbiz){return {main:async function(){
          inbiz.configEvent("getPageAddMetaData",()=>[{key:"current",value:inbiz("result").value}]);
        }}}'''
        spec = specification(steps=[{"kind": "call_group", "group_key": "main"},
                                     {"kind": "read_metadata"},
                                     {"kind": "set_inputs", "fields": {"result": {"value": "changed"}}}],
                             expect={"metadata": [{"key": "current", "value": "changed"}]})
        report = self.run_case(source, spec)
        self.assertEqual(report["cases"][0]["candidate"]["status"], "passed")

    def test_candidate_all_cases_run_before_baseline_and_cases_do_not_share_state(self):
        runner = self.runner()
        spec = specification(); other = deepcopy(spec["cases"][0]); other["id"] = "case-two"; spec["cases"].append(other)
        calls = []
        def record(source, case, timeout):
            calls.append((source, case["id"]))
            return {"status": "passed", "trace": [], "assertions": [], "actual": {}}
        with patch.object(runner, "_run", side_effect=record):
            runner.run_pair("old", "new", spec)
        self.assertEqual(calls, [("new", "case-one"), ("new", "case-two"), ("old", "case-one"), ("old", "case-two")])

    def test_declared_api_call_order_is_checked(self):
        source = 'function(inbiz){return {main:async function(){inbiz("result").value="after";console.log("done");}};}'
        spec = specification(expect={"call_order": ["group.main", "result.value.write", "console.log"]})
        self.assertEqual(self.run_case(source, spec)["cases"][0]["candidate"]["status"], "passed")
        spec["cases"][0]["expect"]["call_order"] = ["group.main", "console.log", "result.value.write"]
        self.assertEqual(self.run_case(source, spec)["cases"][0]["candidate"]["status"], "failed")

    def test_existing_manual_callback_runs_the_actual_compiler_output(self):
        from dataclasses import replace
        from pathlib import Path
        from gxp_editor.compiler import CompilerBridge
        from tests.helpers import editor_config
        bridge_path = Path(FRONTEND) / "dist/action-design-compiler/bridge.js"
        if not bridge_path.is_file():
            self.skipTest("Existing native bridge required")
        callback = json.loads((Path(__file__).parent / "fixtures/manual_child_button_callback.json").read_text(encoding="utf-8"))
        design = {"actionData": [{"key": "main", "title": "初始化", "actionType": "js", "data": []}, callback], "globalVariables": []}
        bridge = CompilerBridge(replace(editor_config(), frontend_repo=FRONTEND, compiler_bridge=str(bridge_path)))
        result = bridge.compile_patch(design, [{"operation": "bind_component_event", "action_group_key": "main",
            "component_id": "table", "component_type": "FormTables", "event_name": "onChildrenCustomBeforeClick", "target_action_group_key": "manualCallback"}])
        spec = specification(environment={"fields": {"table": {}, "trainee_task": {"selected_rows": [{"topic": "甲"}]}}, "child_fields": {"assess_status": {"text": "合格"}}},
            steps=[{"kind": "call_group", "group_key": "main"}, {"kind": "emit_event", "component_id": "table",
                "event_name": "onChildrenCustomBeforeClick", "args": [{"id": "OjuHwTLK"}], "inject_inner_inbiz": True}],
            expect={"metadata": [{"key": "evaluationTopics", "value": "甲"}, {"key": "evaluationResult", "value": "合格"}], "last_return": {"returnValue": None}})
        actual = self.run_case(result["js_code"], spec)["cases"][0]["candidate"]
        self.assertEqual(actual["status"], "passed", actual)

    def test_builtin_monkeypatch_cannot_change_the_comparison_oracle(self):
        for prefix in ('Object.keys=()=>[];', 'JSON.stringify=()=>"forged";', 'Object={keys:()=>[]};'):
            source = 'function(inbiz){' + prefix + 'return {main:async function(){inbiz("result").value="wrong";}}}'
            actual = self.run_case(source)["cases"][0]["candidate"]
            self.assertEqual(actual["status"], "failed", actual)

    def test_field_missing_undefined_and_null_are_distinguishable(self):
        for body, kind in (('inbiz("result").value=undefined;', 'undefined'), ('', 'missing')):
            spec = specification(environment={"fields": {"result": {}}}, expect={"fields": {"result": {"value": None}}})
            actual = self.run_case('function(inbiz){return {main:async function(){' + body + '}}}', spec)["cases"][0]["candidate"]
            self.assertEqual(actual["status"], "failed")
            self.assertEqual(actual["actual"]["field_value_presence"]["result"], kind)
            self.assertEqual(actual["assertions"][0]["actual_kind"], kind)

    def test_sparse_arrays_do_not_pass_as_empty_or_dense_arrays(self):
        for body in ('return Array(2);', 'const rows=[1,2];delete rows[0];return rows;'):
            source = 'function(){return {main:async function(){' + body + '}}}'
            actual = self.run_case(source, specification(expect={"last_return": []}))["cases"][0]["candidate"]
            self.assertEqual(actual["status"], "unverified", actual)
            self.assertIn("Sparse array", actual["reason"])
        source = 'function(){return {main:async function(){return [1,2];}}}'
        self.assertEqual(self.run_case(source, specification(expect={"last_return": [1,2]}))["cases"][0]["candidate"]["status"], "passed")
        self.assertEqual(self.run_case(source, specification(expect={"last_return": [1]}))["cases"][0]["candidate"]["status"], "failed")

    def test_unsafe_integer_observations_are_unverified_and_large_string_ids_are_exact(self):
        source = 'function(){return {main:async function(){return {id:9007199254740993};}}}'
        actual = self.run_case(source, specification(expect={"last_return": {"id": "9007199254740993"}}))["cases"][0]["candidate"]
        self.assertEqual(actual["status"], "unverified", actual)
        self.assertIn("safe range", actual["reason"])
        source = 'function(){return {main:async function(){return {id:"9007199254740993"};}}}'
        self.assertEqual(self.run_case(source, specification(expect={"last_return": {"id": "9007199254740993"}}))["cases"][0]["candidate"]["status"], "passed")

    def test_expectations_reject_boolean_operator_inversion_and_removed_null_guard(self):
        base = 'function(inbiz){return {main:async function(){if(CONDITION) inbiz("result").value="after";}}}'
        expression = 'inbiz.getPageMode()==="add" && (inbiz("source").value || "").trim()!==""'
        cases = []
        for index, (mode, value, expected) in enumerate([('add',' ready ','after'), ('edit',' ready ','before'), ('add','', 'before'), ('add',None,'before')]):
            case = specification(environment={"page_mode": mode, "fields": {"source": {"value": value}, "result": {"value": "before"}}},
                                 expect={"fields": {"result": {"value": expected}}})["cases"][0]
            case["id"] = 'condition-' + str(index); cases.append(case)
        spec = {"schema_version": 1, "cases": cases}
        correct = self.run_case(base.replace('CONDITION', expression), spec)
        self.assertEqual(correct["summary"]["passed"], 4)
        for mutation in (expression.replace(' && ', ' || '), '!(' + expression + ')', expression.replace('(inbiz("source").value || "")', 'inbiz("source").value')):
            with self.subTest(mutation=mutation):
                report = self.run_case(base.replace('CONDITION', mutation), spec)
                self.assertTrue(report["candidate_gate"]["blocked"], report)


if __name__ == "__main__":
    unittest.main()
