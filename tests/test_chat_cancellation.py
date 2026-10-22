from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from tests.helpers import baseline
from gxp_editor.approval import save_artifact
from gxp_editor.execution_lock import ExecutionBusyError, execution_lock
from gxp_editor.service import EditorService, EditorServiceError
from gxp_editor.state import StateError, StateStore


class ChatCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / 'state.sqlite3')
        self.owner = self.store.create_conversation('当前对话')['conversation_id']
        self.other = self.store.create_conversation('其他对话')['conversation_id']
        self.session = self.store.create_session(baseline(), 'backup', 'compiler', conversation_id=self.owner)
        self.service = object.__new__(EditorService)
        self.service.state = self.store
        self.service._conversation_id = self.owner
        self.plan = self.store.create_plan(self.session['id'], 'plan.json', 'old', 'new', 'compiler', conversation_id=self.owner)
        self.path = self.root / 'approval.json'
        save_artifact(self.path, {'approvals': {'one': {'approved': True}}})
        self.transaction = self.store.create_transaction(self.session['id'], self.plan, 'write', str(self.path), conversation_id=self.owner)

    def preview(self):
        return self.service.inspect_session_cancellation(self.session['id'])

    def cancel(self, preview=None, confirmation='同意取消上述旧审核，保留业务数据和备份'):
        preview = self.preview() if preview is None else preview
        return self.service.cancel_unexecuted_session(self.session['id'], preview['cancellation_fingerprint'], confirmation)

    def test_preview_lists_exact_scope_without_mutating_it(self):
        preview = self.preview()
        self.assertTrue(preview['cancellable'])
        self.assertTrue(preview['requires_user_confirmation'])
        self.assertEqual(preview['plans'], [{'id': self.plan, 'status': 'frozen'}])
        self.assertEqual(preview['approvals'], [{'id': self.transaction, 'state': 'awaiting_approval'}])
        self.assertEqual(self.store.get_transaction(self.transaction)['state'], 'awaiting_approval')
        self.assertEqual(preview['cancellation_fingerprint'], self.preview()['cancellation_fingerprint'])

    def test_confirmed_cancellation_retains_artifacts_and_audits_the_quote(self):
        before = self.path.read_bytes()
        with patch.object(self.service, '_components', side_effect=AssertionError('No business connection')), \
             patch('gxp_editor.service.subprocess.Popen', side_effect=AssertionError('No UI required')):
            result = self.cancel()
        self.assertTrue(result['cancelled'])
        self.assertTrue(result['session_released'])
        self.assertFalse(result['write_executed'])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.store.get_session(self.session['id'])['backup_id'], 'backup')
        self.assertEqual(self.store.get_plan(self.plan)['status'], 'cancelled')
        self.assertEqual(self.store.get_transaction(self.transaction)['state'], 'cancelled')
        with self.store._connection() as db:
            audit = json.loads(db.execute('SELECT snapshot_json FROM session_closures').fetchone()[0])
        self.assertEqual(audit['event'], 'user_authorized_cancellation')
        self.assertEqual(audit['user_confirmation'], '同意取消上述旧审核，保留业务数据和备份')
        self.assertFalse(audit['business_data_written'])
        self.assertFalse(audit['verified'])
        with self.assertRaises(StateError):
            self.store.claim_transaction(self.transaction)

    def test_missing_confirmation_or_fingerprint_does_not_cancel(self):
        for fingerprint, quote in ((self.preview()['cancellation_fingerprint'], ''),
                                   (self.preview()['cancellation_fingerprint'], '  '),
                                   (self.preview()['cancellation_fingerprint'], True),
                                   ('', '同意'), ('x' * 64, '同意')):
            with self.subTest(quote=quote), self.assertRaises(EditorServiceError):
                self.service.cancel_unexecuted_session(self.session['id'], fingerprint, quote)
        self.assertEqual(self.store.get_transaction(self.transaction)['state'], 'awaiting_approval')

    def test_other_dialogue_and_stale_handoff_cannot_cancel(self):
        preview = self.preview()
        self.service._conversation_id = self.other
        with self.assertRaises(StateError):
            self.cancel(preview)
        self.service._conversation_id = self.owner
        self.store.transfer_session(self.session['id'], self.other, expected_owner=self.owner,
                                    expected_revision=0, expected_transaction_id=self.transaction)
        self.store.transfer_session(self.session['id'], self.owner, expected_owner=self.other,
                                    expected_revision=1, expected_transaction_id=self.transaction)
        with self.assertRaisesRegex(EditorServiceError, '已变化'):
            self.cancel(preview)

    def test_new_plan_requires_a_new_confirmation(self):
        other_session = self.store.create_session(baseline(id='other', ref_id='other'), 'backup', 'compiler', conversation_id=self.other)
        self.service._conversation_id = self.other
        old = self.service.inspect_session_cancellation(other_session['id'])
        plan = self.store.create_plan(other_session['id'], 'new.json', 'old', 'new', 'compiler', conversation_id=self.other)
        with self.assertRaisesRegex(EditorServiceError, '已变化'):
            self.service.cancel_unexecuted_session(other_session['id'], old['cancellation_fingerprint'], '同意')
        self.assertEqual(self.store.get_plan(plan)['status'], 'ready')

    def test_confirmed_locked_or_planned_session_without_approval_can_be_released(self):
        for state in ('locked', 'planned'):
            with self.subTest(state=state):
                session = self.store.create_session(baseline(id=state, ref_id=state), 'backup', 'compiler', conversation_id=self.other)
                if state == 'planned':
                    self.store.create_plan(session['id'], 'new.json', 'old', 'new', 'compiler', conversation_id=self.other)
                self.service._conversation_id = self.other
                preview = self.service.inspect_session_cancellation(session['id'])
                self.assertEqual(preview['approvals'], [])
                result = self.service.cancel_unexecuted_session(session['id'], preview['cancellation_fingerprint'], '同意取消上述旧流程')
                self.assertTrue(result['session_released'])

    def test_state_change_after_service_check_is_rejected_atomically(self):
        preview = self.preview()
        original = self.store.abort_session
        def claim_then_cancel(*args, **kwargs):
            self.store.claim_transaction(self.transaction)
            return original(*args, **kwargs)
        with patch.object(self.store, 'abort_session', side_effect=claim_then_cancel), self.assertRaises(StateError):
            self.cancel(preview)
        self.assertEqual(self.store.get_transaction(self.transaction)['state'], 'executing')

    def test_executor_lock_blocks_cancellation(self):
        preview = self.preview()
        with execution_lock(self.path), self.assertRaises(ExecutionBusyError):
            self.cancel(preview)
        self.assertEqual(self.store.get_transaction(self.transaction)['state'], 'awaiting_approval')

    def test_executed_uncertain_or_restore_states_are_not_chat_cancellable(self):
        for state in ('executing', 'saved', 'saved_state_pending', 'failed'):
            with self.subTest(state=state):
                self.store.update_transaction(self.transaction, state)
                self.assertFalse(self.preview()['cancellable'])
        self.store.update_transaction(self.transaction, 'awaiting_approval')
        for state in ('saved', 'verified', 'restore_pending', 'closed_unverified'):
            self.store.update_session(self.session['id'], state, conversation_id=self.owner)
            with self.subTest(state=state):
                self.assertFalse(self.preview()['cancellable'])

    def test_legacy_execution_flags_still_block_cancellation(self):
        for flag in ('execution_started', 'executed'):
            with self.subTest(flag=flag):
                save_artifact(self.path, {})
                preview = self.preview()
                save_artifact(self.path, {flag: True})
                self.assertFalse(self.preview()['cancellable'])
                with self.assertRaisesRegex(StateError, 'already started'):
                    self.cancel(preview)
        self.assertEqual(self.store.get_transaction(self.transaction)['state'], 'awaiting_approval')

    def test_missing_pending_artifact_can_be_cancelled(self):
        self.path.unlink()
        self.assertTrue(self.cancel()['cancelled'])

    def test_corrupt_pending_artifact_can_be_cancelled(self):
        self.path.write_text('bad-json', encoding='utf-8')
        self.assertTrue(self.cancel()['cancelled'])

    def test_nonobject_pending_artifact_can_be_cancelled(self):
        self.path.write_text('[]', encoding='utf-8')
        self.assertTrue(self.cancel()['cancelled'])

    def test_cancel_and_execution_claim_cannot_both_win(self):
        preview = self.preview()
        barrier = Barrier(2)
        def compete(operation):
            barrier.wait(timeout=5)
            try:
                self.cancel(preview) if operation == 'cancel' else self.store.claim_transaction(self.transaction)
                return operation
            except (StateError, EditorServiceError):
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(compete, ('cancel', 'execute')))
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_audit_and_cancellation_roll_back_together(self):
        with self.store._connection() as db:
            db.execute("CREATE TRIGGER reject_cancel BEFORE UPDATE ON sessions WHEN NEW.state='aborted' "
                       "BEGIN SELECT RAISE(ABORT, 'test rejection'); END")
        with self.assertRaisesRegex(Exception, 'test rejection'):
            self.cancel()
        self.assertEqual(self.store.get_transaction(self.transaction)['state'], 'awaiting_approval')
        self.assertEqual(self.store.get_plan(self.plan)['status'], 'frozen')
        with self.store._connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM session_closures').fetchone()[0], 0)
