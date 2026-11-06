from __future__ import annotations

import copy
from pathlib import Path
import sys
import unittest
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_form_editor.approval import approve, prepare_approval, signing_secret, verify
from gxp_form_editor.common import FormError, digest


class FormApprovalTests(unittest.TestCase):
    def setUp(self):
        self.secret = b"offline-form-approval-secret-0000000000"
        self.binding = {
            "conversation_id": "conversation-test", "session_id": "session-test",
            "target": {"environment": "test", "page_id": "page-test", "data_version_id": "draft-test"},
            "baseline_hash": "old", "candidate_hash": "new", "adapter_hash": "contract",
            "requests": [{"id": "save", "method": "PUT", "path": "/inbiz/api/services/engines/v3/page", "body": {"component": "{}"}}],
            "actor": {"id": "offline-actor"},
        }

    def artifact(self):
        artifact = prepare_approval(self.binding, now=100, ttl_seconds=300)
        return approve(artifact, self.secret, "local-reviewer", now=110)

    def test_human_signature_is_required_and_expiry_is_enforced(self):
        pending = prepare_approval(self.binding, now=100, ttl_seconds=300)
        with self.assertRaises(FormError):
            verify(pending, self.secret, now=120)
        signed = approve(pending, self.secret, "local-reviewer", now=110)
        self.assertTrue(verify(signed, self.secret, self.binding, now=120))
        with self.assertRaises(FormError):
            verify(signed, self.secret, now=400)

    def test_every_material_binding_and_request_body_is_signed(self):
        for key in ("conversation_id", "session_id", "target", "baseline_hash", "candidate_hash", "adapter_hash", "requests", "actor"):
            with self.subTest(key=key):
                signed = self.artifact()
                value = signed["binding"][key]
                if isinstance(value, dict):
                    value["tampered"] = True
                elif isinstance(value, list):
                    value[0]["body"] = {"component": "changed"}
                else:
                    signed["binding"][key] += "changed"
                signed["binding_hash"] = digest(signed["binding"])
                with self.assertRaises(FormError):
                    verify(signed, self.secret, now=120)

    def test_cross_domain_reviewer_expiry_or_secret_cannot_be_substituted(self):
        for key, value in (("domain", "action_design"), ("expires_at", 450), ("nonce", "other")):
            signed = self.artifact()
            signed[key] = value
            with self.assertRaises(FormError):
                verify(signed, self.secret, now=120)
        signed = self.artifact()
        signed["approval"]["reviewer"] = "another-person"
        with self.assertRaises(FormError):
            verify(signed, self.secret, now=120)
        with self.assertRaises(FormError):
            verify(self.artifact(), b"another-separate-domain-secret-00000", now=120)

    def test_signature_cannot_authorize_a_different_expected_binding(self):
        expected = copy.deepcopy(self.binding)
        expected["target"]["data_version_id"] = "another-draft"
        with self.assertRaises(FormError):
            verify(self.artifact(), self.secret, expected, now=120)

    def test_incomplete_request_scope_or_unbounded_ttl_is_rejected(self):
        for invalid in ({}, {**self.binding, "actor": None}, {**self.binding, "requests": []}):
            with self.assertRaises(FormError):
                prepare_approval(invalid)
        with self.assertRaises(FormError):
            prepare_approval(self.binding, ttl_seconds=3601)

    def test_concurrent_first_ui_start_creates_one_form_signing_secret(self):
        barrier = threading.Barrier(2)
        values = {}
        thread_state = threading.local()
        generated = []
        results = []
        errors = []
        def get_password(service, key):
            if not getattr(thread_state, "checked", False):
                thread_state.checked = True
                initial = values.get((service, key))
                barrier.wait(timeout=3)
                return initial
            return values.get((service, key))
        def set_password(service, key, value):
            generated.append(value)
            values[(service, key)] = value
        def worker():
            try:
                results.append(signing_secret())
            except Exception as exc:
                errors.append(exc)
        fake_keyring = SimpleNamespace(get_password=get_password, set_password=set_password)
        with tempfile.TemporaryDirectory() as root, patch.dict("sys.modules", {"keyring": fake_keyring}), patch.dict("os.environ", {"GXP_FORM_CONFIG_ROOT": root}):
            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(1, len(generated))
        self.assertEqual(2, len(results))
        self.assertEqual(results[0], results[1])

    def test_review_panels_show_complete_changes_and_requests_without_snapshot_duplication(self):
        from gxp_form_editor.ui import _review_panels
        artifact = self.artifact()
        value = {"target": self.binding["target"], "artifact": artifact, "requests": self.binding["requests"], "write": {"state": "pending"}, "session": {"snapshot": {"actor": self.binding["actor"], "native": {"private-repeat": "do-not-display"}, "page": {"private-repeat": "do-not-display"}}}, "changes": [{"component_id": "field-test", "component_type": "EformInput", "path": "/x-component-props/placeholder", "old_value": "old text", "value": "new text", "derived": True}]}
        panels = _review_panels(value)
        self.assertIn("旧值 → 新值", panels["changes"])
        self.assertIn("field-test", panels["changes"])
        self.assertIn("联动派生", panels["changes"])
        self.assertIn("old text", panels["changes"])
        self.assertIn("new text", panels["changes"])
        self.assertNotIn("do-not-display", "".join(panels.values()))
        self.assertIn("component", panels["requests"])
        self.assertIn("/inbiz/api/services/engines/v3/page", panels["requests"])


if __name__ == "__main__":
    unittest.main()
