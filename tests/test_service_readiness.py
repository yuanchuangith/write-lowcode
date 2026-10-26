"""Reused adjacent evidence must never hide drift after external work."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from gxp_editor.service import EditorServiceError
from tests.payload_fixtures import PayloadHarness


class ServiceReadinessTests(unittest.TestCase):
    def test_begin_blocks_source_drift_during_backup_before_occupancy(self):
        with PayloadHarness() as harness:
            with patch.object(harness.compiler, "registry_and_binding", side_effect=ValueError("source drift after backup")):
                with self.assertRaisesRegex(EditorServiceError, "Compiler readiness changed"):
                    harness.service.begin_edit_session("synthetic")
            self.assertIsNone(harness.service.state.active_session(harness.owner))

    def test_prepare_checks_source_after_draft_io_and_reuses_adjacent_grants(self):
        with PayloadHarness() as harness:
            session = harness.service.begin_edit_session("synthetic")
            value = deepcopy(harness.canvas["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["settings"])
            operation = {"operation": "set_parameter", "action_group_key": "main", "target_node_key": "target",
                "expected_element_key": "Noop",
                "path": "/inputParams/settings", "expected_old_value": value, "value": {**value, "flag": True}}
            plan = harness.service.plan_canvas_patch(session["session_id"], [operation], review_spec=harness.spec())
            old_load = harness.repository.load_draft_by_id
            changed = False
            def load_then_drift(identifier):
                nonlocal changed
                loaded = old_load(identifier)
                changed = True
                return loaded
            original = harness.compiler.registry_and_binding
            def fresh_binding():
                if changed:
                    raise ValueError("source changed after draft IO")
                return original()
            grants = Mock(wraps=harness.repository.grant_status)
            with patch.object(harness.repository, "load_draft_by_id", side_effect=load_then_drift), \
                 patch.object(harness.repository, "grant_status", grants), \
                 patch.object(harness.compiler, "registry_and_binding", side_effect=fresh_binding):
                with self.assertRaisesRegex(ValueError, "source changed after draft IO"):
                    harness.service.prepare_write(plan["plan_id"])
            self.assertEqual(grants.call_count, 1)
            self.assertEqual(harness.service.state.session_transactions(session["session_id"]), [])

    def test_deferred_status_does_not_claim_compiler_or_write_readiness(self):
        with PayloadHarness() as harness:
            with patch.object(harness.compiler, "registry_and_binding") as scan:
                status = harness.service._readiness_status(probe_runtime=False, defer_compiler=True)
            scan.assert_not_called()
            self.assertTrue(status["noncompiler_write_ready"])
            self.assertFalse(status["write_ready"])
            self.assertFalse(status["gates"]["compiler"]["ready"])

    def test_deferred_readiness_failures_keep_a_false_gate(self):
        with PayloadHarness() as harness:
            for failing in ("configuration", "database"):
                with self.subTest(failing=failing):
                    if failing == "configuration":
                        context = patch.object(harness.service, "_components", side_effect=ValueError("unavailable"))
                    else:
                        context = patch.object(harness.service._components()[1], "status", side_effect=ValueError("offline"))
                    with context:
                        status = harness.service._readiness_status(probe_runtime=False, defer_compiler=True)
                    self.assertFalse(status["noncompiler_write_ready"])
                    self.assertFalse(status["write_ready"])


if __name__ == "__main__":
    unittest.main()
