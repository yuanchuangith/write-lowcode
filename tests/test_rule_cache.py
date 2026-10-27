from __future__ import annotations

import copy
import os
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_compiler as helpers
from gxp_editor.compiler import CompilerError, _REGISTRY_CACHE, _CONTRACT_CACHE


class RuleCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.factory = helpers.CompilerBridgeTests()
        _REGISTRY_CACHE.clear()
        _CONTRACT_CACHE.clear()

    def bridge(self):
        bridge = self.factory.bridge(self.root)
        bridge._node_identity = lambda: ("node", "v22.0.0")
        return bridge

    def test_request_instances_reuse_rules_but_refresh_git_provenance(self):
        first = self.bridge()
        with patch.object(first, "_call", return_value=helpers.registry("one")) as call:
            original = first.describe_registry()
        self.assertEqual(call.call_count, 1)
        second = self.bridge()
        provenance = {**second.source_provenance(), "commit": "c" * 40}
        second.source_provenance = lambda: provenance
        with patch.object(second, "_call", side_effect=AssertionError("warm rules must not spawn registry Node")):
            warm = second.describe_registry()
        self.assertEqual(original["registry_hash"], warm["registry_hash"])
        self.assertNotEqual(original["source_provenance"], warm["source_provenance"])
        warm["elements"].clear()
        self.assertTrue(second.describe_registry()["elements"])

    def test_same_length_same_mtime_bridge_change_cannot_hit(self):
        first = self.bridge()
        with patch.object(first, "_call", return_value=helpers.registry("one")):
            first.describe_registry()
        path = self.root / "bridge.js"
        stat = path.stat()
        content = path.read_bytes().replace(b"test", b"else")
        path.write_bytes(content)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        second = self.bridge()
        # factory writes its fixture, so restore the changed bytes afterwards.
        path.write_bytes(content)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with patch.object(second, "_call", return_value=helpers.registry("two")) as call:
            result = second.describe_registry()
        self.assertEqual(call.call_count, 1)
        self.assertNotEqual(first._registry["bridge_sha256"], result["bridge_sha256"])
        with self.assertRaises(CompilerError):
            first.describe_registry()

    def test_corrupt_cached_rules_are_rebuilt_not_trusted(self):
        bridge = self.bridge()
        with patch.object(bridge, "_call", return_value=helpers.registry("one")):
            bridge.describe_registry()
        value = next(iter(_REGISTRY_CACHE.values()))
        value["registry_hash"] = "corrupt"
        fresh = self.bridge()
        with patch.object(fresh, "_call", return_value=helpers.registry("two")) as call:
            fresh.describe_registry()
        self.assertEqual(call.call_count, 1)

    def test_component_contract_cache_does_not_mark_page_instance_verified(self):
        first = self.bridge()
        contract = {"command": "describe_component_contract", "component": {"type": "button", "events": []}}
        with patch.object(first, "_call", side_effect=[helpers.registry("one"), contract]):
            cold = first.describe_component_contract("button")
        second = self.bridge()
        with patch.object(second, "_call", side_effect=AssertionError("warm contract must not spawn Node")):
            warm = second.describe_component_contract("button")
        self.assertEqual(cold, warm)
        self.assertFalse(warm["component_instance_verified"])

    def prepare_bridge(self):
        bridge = self.bridge()
        with patch.object(bridge, "_call", return_value=helpers.registry("one")):
            bridge.describe_registry()
        return bridge

    @staticmethod
    def command_response():
        return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "validation": {
            "valid": True, "frontend_version": "test", "rule_source_sha256": "a" * 64}}), "")

    def test_adjacent_registry_and_binding_share_one_full_scan(self):
        bridge = self.prepare_bridge()
        with patch.object(bridge, "_verify_native_sources", wraps=bridge._verify_native_sources) as sources:
            registry, binding = bridge.registry_and_binding()
        self.assertEqual(sources.call_count, 1)
        self.assertEqual(binding["compiler_binding_sha256"], registry["compiler_binding_sha256"])
        with patch.object(bridge, "_verify_native_sources", wraps=bridge._verify_native_sources) as sources:
            bridge.describe_registry()
            bridge.compiler_binding()
        self.assertEqual(sources.call_count, 2)

    def test_new_request_warm_registry_needs_one_scan_after_external_probes(self):
        original = self.prepare_bridge()
        fresh = self.bridge()
        events = []
        node_identity = fresh._node_identity
        provenance = fresh.source_provenance
        source_key = fresh._source_key
        with patch.object(fresh, "_node_identity", side_effect=lambda: (events.append("node"), node_identity())[1]), \
             patch.object(fresh, "source_provenance", side_effect=lambda: (events.append("git"), provenance())[1]), \
             patch.object(fresh, "_source_key", side_effect=lambda: (events.append("source"), source_key())[1]) as scan, \
             patch.object(fresh, "_call", side_effect=AssertionError("Warm registry must not run Node")):
            result = fresh.describe_registry()
        self.assertEqual(scan.call_count, 1)
        self.assertLess(events.index("node"), events.index("source"))
        self.assertLess(events.index("git"), events.index("source"))
        self.assertEqual(result["registry_hash"], original._registry["registry_hash"])

    def test_failed_registry_attempt_does_not_commit_provenance_for_retry(self):
        bridge = self.bridge()
        before = {**bridge.source_provenance(), "commit": "b" * 40}
        after = {**before, "commit": "c" * 40}
        with patch.object(bridge, "source_provenance", side_effect=[before, after]) as provenance, \
             patch.object(bridge, "_verify_native_sources", side_effect=[CompilerError("source drift"), "a" * 64, "a" * 64]), \
             patch.object(bridge, "_call", return_value=helpers.registry("one")) as call:
            with self.assertRaisesRegex(CompilerError, "source drift"):
                bridge.describe_registry()
            self.assertIsNone(bridge._provenance)
            self.assertIsNone(bridge._registry)
            result = bridge.describe_registry()
        self.assertEqual(provenance.call_count, 2)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["source_provenance"], after)

    def test_existing_instance_does_not_rebind_to_another_cached_runtime(self):
        original = self.prepare_bridge()
        old_key = original._registry_key_value
        newer = self.bridge()
        newer._node_identity = lambda: ("node", "v23.0.0")
        with patch.object(newer, "_call", return_value=helpers.registry("two")):
            newer.describe_registry()
        original._node_identity = newer._node_identity
        with self.assertRaisesRegex(CompilerError, "runtime binding changed"):
            original.describe_registry()
        self.assertEqual(original._registry_key_value, old_key)

    def test_each_independent_command_still_checks_all_contents_before_and_after(self):
        bridge = self.prepare_bridge()
        with patch.object(bridge, "_verify_native_sources", wraps=bridge._verify_native_sources) as sources, \
             patch.object(bridge, "_adapter_sha256", wraps=bridge._adapter_sha256) as adapter, \
             patch("gxp_editor.compiler.subprocess.run", return_value=self.command_response()) as run:
            first = bridge.validate_design({"actionData": []})
            second = bridge.validate_design({"actionData": []})
        self.assertEqual(run.call_count, 2, "Candidates must not reuse a successful command result")
        self.assertEqual(sources.call_count, 4, "Only the adjacent third scan is removed")
        self.assertEqual(adapter.call_count, 4)
        self.assertEqual(first["compiler_binding"], second["compiler_binding"])
        self.assertIsNone(bridge._verified_call)

    def test_direct_call_receipt_cannot_skip_a_later_independent_binding_check(self):
        bridge = self.prepare_bridge()
        with patch("gxp_editor.compiler.subprocess.run", return_value=self.command_response()):
            result = bridge._call({"command": "validate_design"})
        path = self.root / "bridge.js"
        stat = path.stat()
        path.write_bytes(path.read_bytes().replace(b"test", b"else"))
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        with self.assertRaisesRegex(CompilerError, "Compiler bridge changed"):
            bridge._bind_registry_hash(result)

    def test_mock_response_without_private_receipt_takes_full_binding_path(self):
        bridge = self.prepare_bridge()
        result = json.loads(self.command_response().stdout)
        with patch.object(bridge, "_call", return_value=result), \
             patch.object(bridge, "_verify_native_sources", wraps=bridge._verify_native_sources) as sources:
            bridge.validate_design({"actionData": []})
        self.assertEqual(sources.call_count, 1)

    def test_same_length_same_mtime_change_during_command_is_rejected(self):
        bridge = self.prepare_bridge()
        path = self.root / "bridge.js"
        stat = path.stat()

        def mutate(*args, **kwargs):
            before = path.read_bytes()
            path.write_bytes(before.replace(b"test", b"else"))
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual(len(before), path.stat().st_size)
            return self.command_response()

        with patch("gxp_editor.compiler.subprocess.run", side_effect=mutate), \
             self.assertRaisesRegex(CompilerError, "changed during generation"):
            bridge.validate_design({"actionData": []})
        self.assertIsNone(bridge._verified_call)

    def test_adapter_drift_during_command_is_rejected_before_binding(self):
        bridge = self.prepare_bridge()
        with patch.object(bridge, "_adapter_sha256", side_effect=["a" * 64, "b" * 64]), \
             patch("gxp_editor.compiler.subprocess.run", return_value=self.command_response()), \
             self.assertRaisesRegex(CompilerError, "changed during generation"):
            bridge.validate_design({"actionData": []})
        self.assertIsNone(bridge._verified_call)

    def test_malformed_cached_registry_is_rebuilt_once(self):
        corrupt_values = [[], {"elements": []}, {"elements": {}}, {"elements": [None]},
                          {"command": "describe_registry", "elements": [{"key": []}]},
                          {"command": "describe_registry", "elements": [{"key": "SetComponentStatus", "input_params": 7}]}]
        for corrupt in corrupt_values:
            with self.subTest(corrupt=corrupt):
                _REGISTRY_CACHE.clear()
                original = self.bridge()
                with patch.object(original, "_call", return_value=helpers.registry("one")):
                    original.describe_registry()
                key = next(iter(_REGISTRY_CACHE))
                _REGISTRY_CACHE[key] = copy.deepcopy(corrupt)
                fresh = self.bridge()
                with patch.object(fresh, "_call", return_value=helpers.registry("two")) as call:
                    loaded = fresh.describe_registry()
                self.assertEqual(call.call_count, 1)
                self.assertEqual(loaded["elements"][0]["key"], "SetComponentStatus")

    def test_corrupt_component_contract_is_loaded_again(self):
        first = self.bridge()
        contract = {"command": "describe_component_contract", "component": {"type": "button", "events": []}}
        with patch.object(first, "_call", side_effect=[helpers.registry("one"), contract]):
            first.describe_component_contract("button")
        next(iter(_CONTRACT_CACHE.values()))["payload"]["component"]["events"] = ["corrupted"]
        fresh = self.bridge()
        with patch.object(fresh, "_call", return_value=contract) as call:
            result = fresh.describe_component_contract("button")
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result["component"]["events"], [])


if __name__ == "__main__":
    unittest.main()
