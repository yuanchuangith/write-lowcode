"""Real offline execution of fixed positive, mutation and attribution fixtures."""
from __future__ import annotations

import os
import shutil
import unittest
from pathlib import Path

from scripts.evaluate_business_accuracy import evaluate_freshness, evaluate_native, evaluate_runner

FRONTEND = Path(os.environ.get("GXP_TEST_FRONTEND_REPO", r"G:\hoyi\updateComponents\gxp2.components"))


@unittest.skipUnless(shutil.which("node") and (FRONTEND / "package.json").is_file(), "Existing frontend parser required")
class BusinessAccuracyCorpusTests(unittest.TestCase):
    def test_fixed_business_mutations_and_attribution_run_actual_javascript(self):
        result = evaluate_runner(FRONTEND)
        summary = result["summary"]
        self.assertEqual(summary["templates"], 8)
        self.assertEqual(summary["versions"], 24)
        self.assertEqual(summary["correct_versions"], 8)
        self.assertEqual(summary["mutated_versions"], 16)
        self.assertEqual(summary["mutated_version_failed"], 16, result)
        self.assertEqual(summary["mutated_version_false_green"], 0, result)
        self.assertEqual(summary["correct_version_false_block"], 0, result)
        self.assertEqual(summary["supported_unverified_versions"], 0, result)
        self.assertEqual(summary["unsupported_unverified"], 3, result)
        self.assertEqual(summary["attribution_accepted"], 12, result)
        self.assertTrue(all(row["accepted"] for key in ("versions", "unsupported", "attribution") for row in result[key]), result)
        config = next(row for row in result["attribution"] if row["id"] == "configuration-only")
        self.assertEqual(config["configuration_status"], "passed")
        self.assertEqual(config["status"], "unverified")
        self.assertTrue(all(not row["browser_verified"] and not row["runtime_verified"] for row in result["versions"]))

    def test_changed_evidence_and_incomplete_current_receipts_are_rejected(self):
        result = evaluate_freshness(FRONTEND)
        self.assertEqual(result["total"], 9)
        self.assertEqual(result["rejected"], result["total"], result)


@unittest.skipUnless(os.environ.get("GXP_TEST_COMPILER_BRIDGE"), "Existing native bridge required")
class NativeAccuracyCorpusTests(unittest.TestCase):
    def test_six_seeds_across_all_three_modes_keep_independent_verification(self):
        frontend = Path(os.environ["GXP_TEST_COMPILER_BRIDGE"]).resolve().parents[2]
        result = evaluate_native(frontend)
        self.assertEqual(result["seeds"], 6)
        self.assertEqual(result["total"], 18)
        self.assertEqual(result["accepted"], result["total"], result)


if __name__ == "__main__":
    unittest.main()
