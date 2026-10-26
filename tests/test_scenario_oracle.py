"""Current-runner passed receipts must contain a complete, consistent JSON oracle."""
from __future__ import annotations

import copy
import json
import os
import shutil
import unittest
from pathlib import Path
from types import SimpleNamespace

from gxp_editor.logic_review import LogicReviewError, _validate_passed_data_oracle
from tests.scenario_evidence import passed_receipt


class ScenarioOracleTests(unittest.TestCase):
    def setUp(self):
        self.expect = {"fields": {"result": {"value": "after"}, "nullable": {"value": None}},
                       "metadata": [{"key": "answer", "value": "after"}], "metadata_registrations": 1,
                       "last_return": {"ok": True, "number": 1.0, "rows": ["01"]},
                       "call_counts": {"group.main": 1, "never.called": 0},
                       "call_order": ["group.main", "metadata.register"]}
        self.receipt = passed_receipt(self.expect, [{"api": "group.main"}, {"api": "metadata.register"}])

    def test_complete_receipt_and_json_key_reordering_are_accepted(self):
        _validate_passed_data_oracle(self.expect, self.receipt)
        # Atomic JSON storage can sort object keys, while assertion arrays retain
        # original order. Coverage is a set of unique names, not dictionary order.
        ordered = json.loads(json.dumps(self.expect, sort_keys=True))
        self.receipt["assertions"].reverse()
        _validate_passed_data_oracle(ordered, self.receipt)

    def test_missing_null_empty_omitted_duplicate_and_unknown_assertions_are_rejected(self):
        for mutate in (
            lambda r: r.pop("assertions"), lambda r: r.update(assertions=None),
            lambda r: r.update(assertions=[]), lambda r: r["assertions"].pop(),
            lambda r: r["assertions"].append(copy.deepcopy(r["assertions"][0])),
            lambda r: r["assertions"][0].update(name="fields.other.value"),
        ):
            with self.subTest(mutate=mutate), self.assertRaises(LogicReviewError):
                receipt = copy.deepcopy(self.receipt); mutate(receipt)
                _validate_passed_data_oracle(self.expect, receipt)

    def test_false_green_observations_assertions_and_presence_are_rejected(self):
        mutations = [
            lambda r: r["actual"]["fields"]["result"].update(value="wrong"),
            lambda r: r["assertions"][0].update(actual="wrong"),
            lambda r: r["assertions"][0].update(expected="wrong"),
            lambda r: r["assertions"][0].update(passed=False),
            lambda r: r["assertions"][0].update(actual_present=False),
            lambda r: r["assertions"][0].update(actual_present=1),
            lambda r: r["assertions"][0].update(actual_kind="undefined"),
            lambda r: r["actual"]["field_value_presence"].update(result="undefined"),
            lambda r: r["actual"]["field_value_presence"].update(nullable="missing"),
            lambda r: r["actual"]["fields"]["nullable"].pop("value"),
            lambda r: r["actual"].update(last_return_undefined=True),
            lambda r: r["actual"]["call_counts"].update({"group.main": 2}),
            lambda r: r["trace"].reverse(),
            lambda r: r["trace"].append({"api": "metadata.register"}),
            lambda r: r["actual"]["last_return"].update(ok=1),
            lambda r: r["actual"]["last_return"].update(rows=[]),
            lambda r: r["actual"].update(metadata=float("nan")),
            lambda r: r.update(reason="Unsupported field API"),
            lambda r: r["trace"].append({"api": "", "value": float("nan")}),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(LogicReviewError):
                receipt = copy.deepcopy(self.receipt); mutate(receipt)
                _validate_passed_data_oracle(self.expect, receipt)

    def test_undefined_return_is_distinct_from_null_and_numeric_equality_matches_js(self):
        expect = {"last_return_undefined": True}
        receipt = passed_receipt(expect)
        _validate_passed_data_oracle(expect, receipt)
        receipt["actual"]["last_return"] = None
        with self.assertRaises(LogicReviewError):
            _validate_passed_data_oracle(expect, receipt)
        self.receipt["actual"]["last_return"]["number"] = 1
        _validate_passed_data_oracle(self.expect, self.receipt)


FRONTEND = Path(os.environ.get("GXP_TEST_FRONTEND_REPO", r"G:\hoyi\updateComponents\gxp2.components"))


@unittest.skipUnless(shutil.which("node") and (FRONTEND / "package.json").is_file(), "Existing frontend parser required")
class RealScenarioOracleTests(unittest.TestCase):
    def test_real_runner_all_data_observations_match_the_python_oracle(self):
        from gxp_editor.logic_scenarios import LogicScenarioRunner
        source = '''function(inbiz){return {main:async function(){
          inbiz("result").value="after";
          inbiz.configEvent("getPageAddMetaData",()=>[{key:"answer",value:"after"}]);
          return null;
        }};}'''
        expect = {"fields": {"result": {"value": "after"}, "nullable": {"value": None}},
                  "metadata": [{"key": "answer", "value": "after"}], "metadata_registrations": 1,
                  "last_return": None, "call_counts": {"group.main": 1, "never.called": 0},
                  "call_order": ["group.main", "result.value.write", "metadata.register", "metadata.read"]}
        scenarios = {"schema_version": 1, "cases": [{"id": "all-data", "steps": [{"kind": "call_group", "group_key": "main"}],
            "environment": {"fields": {"result": {"value": "before"}, "nullable": {"value": None}}}, "expect": expect}]}
        report = LogicScenarioRunner(SimpleNamespace(frontend_repo=str(FRONTEND))).run_pair(source, source, scenarios)
        for side in ("baseline", "candidate"):
            observed = report["cases"][0][side]
            self.assertEqual(observed["status"], "passed", observed)
            _validate_passed_data_oracle(expect, observed)


if __name__ == "__main__":
    unittest.main()
