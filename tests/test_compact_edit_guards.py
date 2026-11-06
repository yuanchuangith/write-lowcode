"""Exact legacy compatibility and compact guard security without business IO."""
from __future__ import annotations

import base64
from copy import deepcopy
import hashlib
import hmac
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_editor.compiler import CompilerBridge, CompilerError
from gxp_editor.edit_guards import EditGuardError, create_guard_ref, expand_guard_operations, parameter_value
from gxp_editor.hashing import canonical_json, relation_snapshot_hash, sha256_json


def legacy_guard_ref(session, conversation_id, operation, value, secret):
    """Explicit old-wire fixture, independent of the default token generator."""
    payload = {
        "purpose": "edit_guard_v1", "conversation_id": conversation_id, "session_id": session["id"],
        "owner_revision": session.get("owner_revision", 0), "backup_id": session["backup_id"],
        "design_id": session["design_id"], "baseline_content_hash": session["baseline"]["content_hash"],
        "relation_sha256": relation_snapshot_hash(session["baseline"].get("relations") or []),
        "compiler_hash": session["compiler_hash"],
        "operation": operation.get("operation"), "group_key": operation.get("action_group_key"),
        "node_key": operation.get("target_node_key"), "element_key": operation.get("expected_element_key"),
        "path": operation.get("path"), "item_key": operation.get("item_key"), "value_sha256": sha256_json(value),
    }
    raw = canonical_json(payload).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=") + "." + signature


def fixtures():
    session = {"id": "session-synthetic-0000000000000001", "owner_revision": 2,
               "backup_id": "backup-synthetic-0000000000000001", "design_id": "design-synthetic-0000000000000001",
               "compiler_hash": "c" * 64, "baseline": {"content_hash": "b" * 64, "relations": []}}
    node = {"key": "target", "elementKey": "Noop", "paramsValue": {"inputParams": {
        "settings": {"flag": False, "other": False}, "rows": [{"id": "a", "value": 1}, {"id": "b", "value": 1}]}, "outputParams": {}}}
    design = {"actionData": [{"key": "main", "data": [node, {**deepcopy(node), "key": "other"}]},
                              {"key": "other-group", "data": [deepcopy(node)]}], "globalVariables": []}
    operation = {"operation": "set_parameter", "action_group_key": "main", "target_node_key": "target",
                 "expected_element_key": "Noop", "path": "/inputParams/settings/flag", "value": True}
    return session, design, operation


class CompactEditGuardTests(unittest.TestCase):
    def setUp(self):
        self.session, self.design, self.operation = fixtures()
        self.owner = "conversation-synthetic-0000000001"
        self.secret = "synthetic-guard-secret"

    def token(self, operation=None, value=False, legacy=False):
        factory = legacy_guard_ref if legacy else create_guard_ref
        return factory(self.session, self.owner, operation or self.operation, value, self.secret)

    def expand(self, token=None, operation=None, session=None, owner=None, design=None, secret=None):
        operation = deepcopy(operation or self.operation)
        operation["guard_ref"] = self.token() if token is None else token
        return expand_guard_operations(session or self.session, owner or self.owner, design or self.design, [operation], secret or self.secret)

    def test_default_wire_has_only_version_and_256_bit_mac_but_expands_complete_value(self):
        token = self.token()
        self.assertRegex(token, r"^g2\.[a-f0-9]{64}$")
        self.assertEqual(67, len(token))
        result = self.expand(token)
        self.assertFalse(result[0]["expected_old_value"])
        self.assertNotIn("guard_ref", result[0])
        self.assertNotIn("expected_old_value", self.operation)

    def test_legacy_v1_still_expands_and_does_not_accept_tampered_or_wrong_context(self):
        token = self.token(legacy=True)
        self.assertFalse(self.expand(token)[0]["expected_old_value"])
        encoded, signature = token.split(".")
        payload = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        payload["purpose"] = "approval"
        forged = base64.urlsafe_b64encode(canonical_json(payload).encode()).decode().rstrip("=") + "." + signature
        for invalid in (forged, token[:-1] + ("0" if token[-1] != "0" else "1")):
            with self.assertRaises(EditGuardError):
                self.expand(invalid)
        with self.assertRaises(EditGuardError):
            self.expand(token, owner="different-owner")

    def test_v2_purpose_cannot_be_substituted_with_valid_v1_mac(self):
        legacy_signature = self.token(legacy=True).split(".")[1]
        with self.assertRaises(EditGuardError):
            self.expand("g2." + legacy_signature)

    def test_every_session_owner_baseline_relation_and_compiler_binding_is_enforced(self):
        token = self.token()
        for field in ("id", "owner_revision", "backup_id", "design_id", "compiler_hash"):
            session = deepcopy(self.session)
            session[field] = 99 if field == "owner_revision" else "changed"
            with self.subTest(field=field), self.assertRaises(EditGuardError):
                self.expand(token, session=session)
        for field, value in (("content_hash", "changed"), ("relations", [{"id": "changed"}])):
            session = deepcopy(self.session)
            session["baseline"][field] = value
            with self.subTest(baseline=field), self.assertRaises(EditGuardError):
                self.expand(token, session=session)
        for overrides in ({"owner": "another-owner"}, {"secret": "rotated-secret"}):
            with self.subTest(overrides=overrides), self.assertRaises(EditGuardError):
                self.expand(token, **overrides)

    def test_equal_values_at_different_target_group_path_and_element_do_not_share_guard(self):
        token = self.token()
        for field, value in (("action_group_key", "other-group"), ("target_node_key", "other"),
                             ("path", "/inputParams/settings/other"), ("expected_element_key", "Other")):
            operation = {**self.operation, field: value}
            with self.subTest(field=field), self.assertRaises(EditGuardError):
                self.expand(token, operation=operation)
        changed = deepcopy(self.design)
        changed["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["settings"]["flag"] = True
        with self.assertRaises(EditGuardError):
            self.expand(token, design=changed)

    def test_array_item_identity_and_complete_nested_old_value_are_preserved(self):
        operation = {**self.operation, "operation": "set_array_item", "path": "/inputParams/rows",
                     "item_key": {"field": "id", "value": "a"}, "value": {"id": "a", "value": 2}}
        old = {"id": "a", "value": 1}
        token = self.token(operation, old)
        result = self.expand(token, operation)
        self.assertEqual(old, result[0]["expected_old_value"])
        with self.assertRaises(EditGuardError):
            self.expand(token, {**operation, "item_key": {"field": "id", "value": "b"}})
        result[0]["expected_old_value"]["value"] = 999
        self.assertEqual(old, parameter_value(self.design, "main", "target", operation["path"], operation["item_key"]))

    def test_malformed_tampered_and_oversized_tokens_fail_closed(self):
        valid = self.token()
        for token in (None, 12, "", "g2", "g2.", "g2." + "a" * 63, "g2." + "a" * 65,
                      "g2." + "A" * 64, "g2." + "z" * 64, "g2.." + "a" * 64,
                      "!bad!." + "a" * 64, "x" * 16385, valid[:-1] + ("0" if valid[-1] != "0" else "1")):
            operation = {**self.operation, "guard_ref": token}
            with self.subTest(token_type=type(token).__name__), self.assertRaises(EditGuardError):
                expand_guard_operations(self.session, self.owner, self.design, [operation], self.secret)

    def test_guard_and_explicit_old_value_or_nonparameter_operation_cannot_be_combined(self):
        for change in ({"expected_old_value": False}, {"operation": "replace_params"}):
            with self.assertRaises(EditGuardError):
                self.expand(operation={**self.operation, **change})

    def test_same_batch_intermediate_values_do_not_bypass_sequential_preflight(self):
        operation = {**self.operation, "guard_ref": self.token()}
        expanded = expand_guard_operations(self.session, self.owner, self.design, [operation, operation], self.secret)
        CompilerBridge.preflight_operations(self.design, expanded[:1])
        with self.assertRaises(CompilerError):
            CompilerBridge.preflight_operations(self.design, expanded)
        intermediate_token = self.token(value=True)
        later = {**self.operation, "value": False, "guard_ref": intermediate_token}
        with self.assertRaises(EditGuardError):
            expand_guard_operations(self.session, self.owner, self.design, [operation, later], self.secret)

    def test_eight_identical_templates_shrink_without_changing_expanded_operations(self):
        before, after = [], []
        for index in range(8):
            operation = {**self.operation, "path": "/inputParams/settings/field" + str(index), "value": True}
            self.design["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["settings"]["field" + str(index)] = False
            before.append({**operation, "guard_ref": self.token(operation, legacy=True)})
            after.append({**operation, "guard_ref": self.token(operation)})
        self.assertEqual(expand_guard_operations(self.session, self.owner, self.design, before, self.secret),
                         expand_guard_operations(self.session, self.owner, self.design, after, self.secret))
        old_bytes = len(canonical_json(before).encode())
        new_bytes = len(canonical_json(after).encode())
        self.assertLess(new_bytes, old_bytes * .3)


if __name__ == "__main__":
    unittest.main()
