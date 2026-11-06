from __future__ import annotations

import copy
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))

from gxp_form_editor.common import FormError, digest
from gxp_form_editor.service import FormService
from gxp_form_editor.state import Store


class FakeClient:
    def __init__(self):
        self.target = {"environment": "isolated-test", "base_url": "https://example.invalid",
                       "app_id": "app", "site_out_id": "site", "page_id": "page",
                       "route": "form-test", "data_version_id": "draft-1", "platform": "web"}
        self.snapshot = {"identity": self.target, "actor": "test-actor", "revision": "v1",
                         "revision_kind": "server", "associated": {},
                         "source_binding": {"adapter_hash": "fixture-adapter"},
                         "page": {"content": {"schema": {"properties": {
                             "field": {"x-component": "Input", "title": "Field", "required": False}
                         }}}}}
        self.property_catalog = {
            "schema_version": 1, "component_version": "fixture-1", "source_fingerprint": "fixture-source",
            "platform": "web", "components": {"Input": {"properties": {
                "/required": {"type": "boolean", "operation": "set_property"}
            }}}}
        self.ready = True
        self.executions = []
        self.fail_before = False
        self.fail_after = False
        self.before_execute = None

    def readiness(self):
        return {"read_ready": True, "write_ready": self.ready}

    def read_draft(self, target):
        if target != self.target:
            raise AssertionError("unexpected test target")
        return copy.deepcopy(self.snapshot)

    def catalog(self):
        return copy.deepcopy(self.property_catalog)

    def resources(self, snapshot, operations):
        return {}

    def build_requests(self, snapshot, candidate):
        return [{"id": "save", "method": "PUT", "path": "/inbiz/api/services/engines/v3/page",
                 "query": {}, "headers": {"If-Match": snapshot["revision"]},
                 "body": copy.deepcopy(candidate), "expected_statuses": [200]}]

    def execute_request(self, request):
        self.executions.append(copy.deepcopy(request))
        if self.before_execute:
            self.before_execute()
        if self.fail_before:
            raise FormError("fixture_uncertain", "No known response")
        self.snapshot["page"] = copy.deepcopy(request["body"])
        self.snapshot["revision"] += "+1"
        if self.fail_after:
            raise FormError("fixture_uncertain", "Request may have committed")
        return {"status": 200, "body": {"success": True}}

    def verify_saved(self, baseline, candidate, fresh, requests):
        return fresh["page"] == candidate and fresh["associated"] == baseline["associated"]


class FormWorkflowSecurityTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        self.client = FakeClient()
        self.secret = b"test" * 8
        self.launches = []

        def launch(kind, write_id):
            self.launches.append((kind, write_id))
            return {"window_starting": True}

        bootstrap = FormService(store=self.store, client=self.client, secret=self.secret, launcher=launch)
        self.owner = bootstrap.create_form_conversation()["conversation_id"]
        self.service = FormService(self.owner, store=self.store, client=self.client,
                                   secret=self.secret, launcher=launch)
        self.session_id = self.service.begin_form_edit_session(self.client.target)["session_id"]

    def plan(self):
        operations = [{"operation": "set_property", "component_id": "field", "expected_component_type": "Input",
                       "path": "/required", "expected_old_value": False, "value": True}]
        return self.service.plan_form_patch(self.session_id, operations, "Make Field required")["plan_id"]

    def approved(self):
        plan_id = self.plan()
        write_id = self.service.prepare_form_write(plan_id)["write_id"]
        self.service.approve_form_write(write_id)
        return write_id

    def assert_code(self, code, function, *args):
        with self.assertRaises(FormError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_other_registered_conversation_cannot_read_or_cancel_session(self):
        marker = self.service.create_form_conversation()["conversation_id"]
        other = FormService(marker, store=self.store, client=self.client, secret=self.secret)
        self.assert_code("not_owned", other.find_form_components, self.session_id)
        self.assert_code("not_owned", other.cancel_form_edit_session, self.session_id)
        self.assertEqual(self.client.executions, [])

    def test_preview_plan_cannot_prepare_when_platform_write_protocol_is_unverified(self):
        self.client.ready = False
        plan_id = self.plan()
        self.assert_code("platform_not_ready", self.service.prepare_form_write, plan_id)
        self.assertEqual(self.launches, [])
        self.assertEqual(self.client.executions, [])

    def test_added_optional_property_restores_absence_through_new_human_approval(self):
        operations = [{"operation": "set_property", "component_id": "field", "expected_component_type": "Input",
                       "path": "/required", "expected_old_exists": False, "expected_old_value": None, "value": True}]
        # Start from a real native draft without the optional property.
        self.service.cancel_form_edit_session(self.session_id)
        self.client.snapshot["page"]["content"]["schema"]["properties"]["field"].pop("required")
        original = copy.deepcopy(self.client.snapshot["page"])
        session = self.service.begin_form_edit_session(self.client.target)["session_id"]
        plan = self.service.plan_form_patch(session, operations, "Make the optional field required")["plan_id"]
        write = self.service.prepare_form_write(plan)["write_id"]
        self.service.approve_form_write(write)
        self.assertTrue(self.service.execute_form_write(write)["verified"])
        restored = self.service.plan_form_restore(session)
        self.assertEqual(len(self.client.executions), 1)
        write = self.service.prepare_form_write(restored["plan_id"])["write_id"]
        self.service.approve_form_write(write)
        self.assertTrue(self.service.execute_form_write(write)["verified"])
        self.assertEqual(self.client.snapshot["page"], original)

    def test_reopened_approved_review_preserves_signature_without_approving_again(self):
        write = self.approved()
        before = self.store.get("write", write, self.owner)
        reviewed = self.service.get_form_review(write)
        self.assertTrue(reviewed["approval_valid"])
        after = self.store.get("write", write, self.owner)
        self.assertEqual(before["revision"], after["revision"])
        self.assertEqual(before["artifact"], after["artifact"])

    def test_untrusted_session_path_cannot_create_files_during_recovery(self):
        self.assert_code("not_owned", self.service.recover_form_edit_session, "../../escape")
        self.assertFalse((self.store.root / "locks").exists())

    def test_expired_pending_review_is_replaced_without_reusing_approval(self):
        plan_id = self.plan()
        original = self.service.prepare_form_write(plan_id)["write_id"]
        write = self.store.get("write", original, self.owner)
        write["artifact"]["expires_at"] = 1
        self.store.replace("write", write, self.owner, write["revision"])
        replacement = self.service.prepare_form_write(plan_id)["write_id"]
        self.assertNotEqual(original, replacement)
        self.assertEqual(self.store.get("write", original, self.owner)["state"], "expired")
        self.assertIsNone(self.store.get("write", replacement, self.owner)["artifact"]["approval"])
        self.assertEqual(self.client.executions, [])

    def test_candidate_tampering_blocks_prepare_before_launch(self):
        plan_id = self.plan()
        plan = self.store.get("plan", plan_id, self.owner)
        plan["candidate"]["content"]["schema"]["properties"]["field"]["required"] = False
        self.store.replace("plan", plan, self.owner, plan["revision"])
        self.assert_code("plan_tampered", self.service.prepare_form_write, plan_id)
        self.assertEqual(self.launches, [])

    def test_signed_request_tampering_is_rejected_before_any_api_call(self):
        write_id = self.approved()
        write = self.store.get("write", write_id, self.owner)
        write["artifact"]["binding"]["requests"][0]["body"]["malicious"] = True
        self.store.replace("write", write, self.owner, write["revision"])
        self.assert_code("form_approval_changed", self.service.execute_form_write, write_id)
        self.assertEqual(self.client.executions, [])

    def test_cancelled_approval_cannot_execute(self):
        write_id = self.approved()
        self.service.cancel_form_edit_session(self.session_id)
        self.assert_code("not_approved", self.service.execute_form_write, write_id)
        self.assertEqual(self.client.executions, [])

    def test_execution_claim_blocks_duplicate_execution_and_concurrent_cancel(self):
        write_id = self.approved()
        entered, proceed = threading.Event(), threading.Event()

        def wait_for_competitor():
            entered.set()
            if not proceed.wait(3):
                raise AssertionError("test competitor did not release execution")

        self.client.before_execute = wait_for_competitor
        outcome = []

        def execute():
            try:
                outcome.append(self.service.execute_form_write(write_id))
            except BaseException as error:
                outcome.append(error)

        worker = threading.Thread(target=execute)
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            self.assert_code("form_busy", self.service.execute_form_write, write_id)
            self.assert_code("cannot_cancel", self.service.cancel_form_edit_session, self.session_id)
            self.assert_code("form_busy", self.service.recover_form_edit_session, self.session_id)
        finally:
            proceed.set()
            worker.join(4)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(self.client.executions), 1)
        self.assertIsInstance(outcome[0], dict)
        self.assertTrue(outcome[0]["verified"])
        self.assertTrue(outcome[0]["session_released"])

    def test_uncertain_commit_is_read_back_without_replaying_request(self):
        write_id = self.approved()
        self.client.fail_after = True
        result = self.service.execute_form_write(write_id)
        self.assertEqual(result["state"], "uncertain")
        recovered = self.service.recover_form_edit_session(self.session_id)
        self.assertTrue(recovered["verified"])
        self.assertTrue(recovered["session_released"])
        self.assertFalse(recovered["replayed"])
        self.assertEqual(len(self.client.executions), 1)

    def test_disappeared_executor_can_recover_committed_snapshot_without_api_replay(self):
        write_id = self.approved()
        write = self.store.get("write", write_id, self.owner)
        session = self.store.get("session", self.session_id, self.owner)
        plan = self.store.get("plan", session["plan_id"], self.owner)
        self.store.replace("write", {**write, "state": "executing"}, self.owner, write["revision"])
        self.store.replace("session", {**session, "state": "executing"}, self.owner, session["revision"])
        # Model the durable state left by a crashed executor after commit. No
        # executor owns the lifetime OS lock and recovery must only read back.
        self.client.snapshot["page"] = copy.deepcopy(plan["candidate"])
        self.client.snapshot["revision"] += "+committed"
        result = self.service.recover_form_edit_session(self.session_id)
        self.assertTrue(result["verified"])
        self.assertTrue(result["session_released"])
        self.assertFalse(result["replayed"])
        self.assertEqual(self.client.executions, [])

    def test_disappeared_executor_without_commit_retains_uncertain_state_and_page_lease(self):
        write_id = self.approved()
        write = self.store.get("write", write_id, self.owner)
        session = self.store.get("session", self.session_id, self.owner)
        self.store.replace("write", {**write, "state": "executing"}, self.owner, write["revision"])
        self.store.replace("session", {**session, "state": "executing"}, self.owner, session["revision"])
        result = self.service.recover_form_edit_session(self.session_id)
        self.assertEqual(result["state"], "uncertain")
        self.assertFalse(result["verified"])
        self.assertFalse(result["session_released"])
        self.assertTrue(result["page_matches_baseline"])
        self.assertEqual(self.client.executions, [])
        self.assert_code("page_busy", self.service.begin_form_edit_session, self.client.target)

    def test_page_route_alias_cannot_create_a_second_lease_for_the_same_draft(self):
        alias = {**self.client.target, "route": "another-route-alias"}
        snapshot = {**copy.deepcopy(self.client.snapshot), "identity": alias}
        backup = self.store.save_backup(snapshot, self.secret)
        self.assert_code("page_busy", self.store.create_session, self.owner, alias, snapshot, backup)

    def test_verified_existing_property_can_restore_through_new_approved_plan(self):
        write_id = self.approved()
        self.assertTrue(self.service.execute_form_write(write_id)["verified"])
        restored = self.service.plan_form_restore(self.session_id)
        restore_write = self.service.prepare_form_write(restored["plan_id"])["write_id"]
        self.service.approve_form_write(restore_write)
        outcome = self.service.execute_form_write(restore_write)
        self.assertTrue(outcome["verified"])
        self.assertTrue(outcome["session_released"])
        self.assertFalse(self.client.snapshot["page"]["content"]["schema"]["properties"]["field"]["required"])
        self.assertEqual(len(self.client.executions), 2)

    def test_signed_backup_must_match_mutable_session_snapshot(self):
        session = self.store.get("session", self.session_id, self.owner)
        session["snapshot"]["page"]["content"]["schema"]["properties"]["field"]["required"] = True
        self.store.replace("session", session, self.owner, session["revision"])
        with self.assertRaises(FormError):
            self.service.find_form_components(self.session_id)

    def test_recovery_refuses_rehashed_plan_that_no_longer_matches_human_approval(self):
        write_id = self.approved()
        self.client.fail_before = True
        self.assertEqual(self.service.execute_form_write(write_id)["state"], "uncertain")
        session = self.store.get("session", self.session_id, self.owner)
        plan = self.store.get("plan", session["plan_id"], self.owner)
        plan["candidate"] = copy.deepcopy(self.client.snapshot["page"])
        plan["candidate_hash"] = digest(plan["candidate"])
        plan["plan_hash"] = digest({key: value for key, value in plan.items()
                                    if key not in {"id", "domain", "owner", "revision", "created_at", "updated_at", "plan_hash"}})
        self.store.replace("plan", plan, self.owner, plan["revision"])
        with self.assertRaises(FormError):
            self.service.recover_form_edit_session(self.session_id)
        self.assertEqual(self.store.get("session", self.session_id, self.owner)["state"], "uncertain")
        self.assertEqual(len(self.client.executions), 1)


if __name__ == "__main__":
    unittest.main()
