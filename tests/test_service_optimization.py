"""No-op and request-local reuse must not retain stale compiler evidence."""
from __future__ import annotations

import json
import unittest
from unittest.mock import Mock, patch

from tests.helpers import baseline, editor_config
from tests.test_compiler_optimization import fixture, operation
from gxp_editor.compiler import CompilerError
from gxp_editor.service import EditorService, component_request


class ServiceOptimizationTests(unittest.TestCase):
    def test_noop_checks_original_value_without_native_process_or_new_plan(self):
        canvas = fixture()
        draft = baseline(data=json.dumps(canvas), data_json=canvas)
        session = {"id": "s", "design_id": draft["id"], "baseline": draft,
                   "state": "locked", "conversation_id": ""}
        service = object.__new__(EditorService)
        service.state = Mock()
        service.state.get_session.return_value = session
        repository, compiler, runtime = Mock(), Mock(), Mock()
        repository.load_draft_by_id.return_value = draft
        spec = {"schema_version": 1, "revision": "r1", "scope_policy": "manual_review", "requirements": []}
        with patch.object(service, "_components", return_value=(editor_config(), object(), repository, compiler, runtime)):
            response = service.plan_canvas_patch("s", [operation()], spec)
            self.assertTrue(response["no_changes"])
            self.assertIsNone(response["existing_plan_id"])
            compiler.describe_registry.assert_not_called()
            compiler.compile_patch.assert_not_called()
            runtime.validate.assert_not_called()
            service.state.create_plan.assert_not_called()
            with self.assertRaises(CompilerError) as caught:
                service.plan_canvas_patch("s", [operation(expected_old_value="stale")], spec)
            self.assertEqual(caught.exception.details["reason_code"], "old_value_mismatch")

    def test_components_reused_only_within_one_request_including_nested_status(self):
        first, second = object.__new__(EditorService), object.__new__(EditorService)
        first.state = second.state = Mock()
        first.state.active_session.return_value = None

        @component_request
        def request(service, fail=False):
            components = service._components()
            # A nested public tool must share this request's validated bridge.
            service.editor_status(probe_runtime=False)
            self.assertIs(service._components(), components)
            if fail:
                raise RuntimeError("interrupted")
            return components

        with patch("gxp_editor.service.load_config", return_value=editor_config()), \
             patch("gxp_editor.service.EditorDatabase"), patch("gxp_editor.service.EditorRepository"), \
             patch("gxp_editor.service.RuntimeCsharpValidator"), \
             patch("gxp_editor.service.CompilerBridge") as factory:
            original = request(first)
            self.assertEqual(factory.call_count, 1)
            self.assertIsNot(request(first), original)
            self.assertEqual(factory.call_count, 2)
            request(second)
            self.assertEqual(factory.call_count, 3)
            with self.assertRaises(RuntimeError):
                request(first, fail=True)
            request(first)
            self.assertEqual(factory.call_count, 5)


if __name__ == "__main__":
    unittest.main()
