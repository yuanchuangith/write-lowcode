from __future__ import annotations

import copy
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from tests.helpers import transaction
from gxp_editor.approval import transaction_binding_hash
from gxp_editor.telemetry import traced_tool, stage, add_external, current_trace_id


class PerformanceTraceTests(unittest.TestCase):
    def test_observations_never_mutate_signed_artifacts(self):
        artifact = transaction()
        original = copy.deepcopy(artifact)
        before = transaction_binding_hash(artifact)
        @traced_tool
        def call():
            with stage("compiler.generate", count=2):
                return artifact
        result = call()
        self.assertEqual(artifact, original)
        self.assertEqual(before, transaction_binding_hash(artifact))
        self.assertNotIn("trace_id", artifact)
        self.assertEqual(result["timings"][0]["stage"], "compiler.generate")

    def test_failure_keeps_completed_and_failed_spans(self):
        @traced_tool
        def call():
            with stage("database.read"):
                pass
            with stage("compiler.generate"):
                raise ValueError("invalid request")
        result = call()
        self.assertTrue(result["blocked"])
        self.assertEqual([row["stage"] for row in result["timings"]], ["database.read", "compiler.generate"])
        self.assertEqual(result["timings"][-1]["error_code"], "ValueError")
        self.assertEqual(result["failure_stage"], "compiler.generate")
        self.assertEqual(current_trace_id(), "")

    def test_returned_compiler_errors_get_stable_transport_location(self):
        @traced_tool
        def call():
            return {"blocked": True, "error": "Invalid operation", "error_details": {
                "phase": "operation_preflight", "reason_code": "invalid_operation", "operation_index": 1}}
        result = call()
        self.assertEqual(result["failure_stage"], "operation_preflight")
        self.assertEqual(result["error_code"], "invalid_operation")

    def test_logs_and_external_timings_cannot_contain_payloads(self):
        @traced_tool
        def call():
            with stage("runtime.build", source="private-source", password="private-password", count=1):
                add_external([{"stage": "runtime.restore", "elapsed_ms": 3, "source": "secret", "cache_hit": True},
                              {"stage": "bad text", "elapsed_ms": 4}, {"stage": "runtime.bad", "elapsed_ms": float("nan")}])
            return {"source": "private-source"}
        with patch("gxp_editor.telemetry._LOG") as logger:
            result = call()
        self.assertNotIn("private-source", str(logger.info.call_args))
        self.assertNotIn("password", str(result["timings"]))
        self.assertEqual(len(result["timings"]), 2)

    def test_threads_have_separate_trace_ids(self):
        @traced_tool
        def call(index):
            with stage("compiler.generate", count=index):
                return {"index": index}
        with ThreadPoolExecutor(max_workers=3) as executor:
            results = list(executor.map(call, range(8)))
        self.assertEqual(len({row["trace_id"] for row in results}), 8)
        for result in results:
            self.assertEqual(result["timings"][0]["count"], result["index"])

    def test_nested_tool_reuses_trace_instead_of_overwriting_total(self):
        ids = []
        @traced_tool
        def inner():
            ids.append(current_trace_id())
            return {"ok": True}
        @traced_tool
        def outer():
            ids.append(current_trace_id())
            return inner()
        result = outer()
        self.assertEqual(ids, [result["trace_id"], result["trace_id"]])


if __name__ == "__main__":
    unittest.main()
