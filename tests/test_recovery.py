from __future__ import annotations

import tempfile
import subprocess
import sys
import os
import signal
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import MCP_ROOT, baseline, editor_config, transaction
from gxp_editor.approval import save_artifact, transaction_binding_hash
from gxp_editor.execution_lock import ExecutionBusyError, execution_lock
from gxp_editor.recovery import recover_session
from gxp_editor.state import StateError, StateStore


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = StateStore(self.root / 'state.sqlite3')
        self.session = self.store.create_session(baseline(), 'backup', 'compiler')
        plan = self.store.create_plan(self.session['id'], 'plan.json', 'old', 'new', 'compiler')
        self.artifact = transaction()
        self.artifact['session_id'] = self.session['id']
        self.artifact['plan_id'] = plan
        self.artifact['execution_lock_version'] = 1
        self.artifact['transaction_hash'] = transaction_binding_hash(self.artifact)
        self.path = self.root / 'tx.json'
        save_artifact(self.path, self.artifact)
        self.tx = self.store.create_transaction(self.session['id'], plan, 'write', str(self.path), transaction_id='transaction-1')
        self.store.claim_transaction(self.tx)
        self.repo = Mock()
        self.repo.load_draft_by_id.return_value = self.snapshot(self.artifact['new_content_hash'])
        self.repo._relations.return_value = []
        self.database_session = Mock()
        self.repo.database.write_transaction.return_value.__enter__ = Mock(return_value=self.database_session)
        self.repo.database.write_transaction.return_value.__exit__ = Mock(return_value=False)

    def recover(self, **kwargs):
        with patch('gxp_editor.recovery.load_config', return_value=editor_config()), patch(
            'gxp_editor.recovery.EditorRepository', return_value=self.repo
        ):
            return recover_session(self.session['id'], state=self.store, **kwargs)

    def snapshot(self, content_hash):
        row = baseline()
        if content_hash == self.artifact['new_content_hash']:
            row.update(self.artifact['desired_design'])
            row['last_modification_time'] = self.artifact['desired_last_modification_time']
        row['content_hash'] = content_hash
        return row

    def test_lost_commit_acknowledgement_recovers_committed_result(self):
        self.store.update_transaction(self.tx, 'failed', 'commit acknowledgement lost')
        self.assertTrue(self.recover()['verified'])
        self.assertEqual(self.store.get_transaction(self.tx)['state'], 'saved')
        self.assertEqual(self.store.get_session(self.session['id'])['state'], 'verified')
        self.repo.load_draft_by_id.assert_called_once_with('design-1', for_update=True, session=self.database_session)
        self.repo._relations.assert_called_once_with('design-1', self.database_session)
        self.database_session.execute.assert_not_called()

    def test_dead_executor_can_be_recovered_but_live_executor_cannot(self):
        with execution_lock(self.path):
            with self.assertRaises(ExecutionBusyError):
                self.recover()
        self.assertEqual(self.recover()['outcome'], 'saved')

    def test_executor_lock_is_released_when_process_dies(self):
        code = "from gxp_editor.execution_lock import execution_lock; import sys,os;\nwith execution_lock(sys.argv[1]):\n print(os.getpid(), flush=True)\n sys.stdin.read()\n"
        process = subprocess.Popen([sys.executable, '-c', code, str(self.path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, env={**os.environ, "PYTHONPATH": str(MCP_ROOT)})
        try:
            executor_pid = int(process.stdout.readline().strip())
            with self.assertRaises(ExecutionBusyError):
                self.recover()
            # Windows venv python.exe may be a launcher with a separate child.
            os.kill(executor_pid, signal.SIGTERM)
            process.wait(timeout=5)
            self.assertEqual(self.recover()['outcome'], 'saved')
        finally:
            process.stdin.close()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

    def test_session_handling_buttons_visible_at_minimum_window_size(self):
        import importlib.util
        import tkinter as tk
        from tests.helpers import PLUGIN_ROOT
        spec = importlib.util.spec_from_file_location('session_window_test', PLUGIN_ROOT / 'scripts' / 'abort_session_app.py')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        root_factory = tk.Tk
        def hidden_root():
            root = root_factory()
            root.withdraw()
            return root
        with patch.object(app, 'StateStore', return_value=self.store), patch.object(app.tk, 'Tk', side_effect=hidden_root):
            window = app.AbortSessionWindow(self.session['id'])
        try:
            window.root.geometry('650x350+10000+10000')
            window.root.deiconify()
            window.root.update()
            def buttons(widget):
                for child in widget.winfo_children():
                    if child.winfo_class() == 'TButton':
                        yield child
                    yield from buttons(child)
            for button in buttons(window.root):
                self.assertTrue(button.winfo_ismapped())
                top = button.winfo_rooty() - window.root.winfo_rooty()
                self.assertLessEqual(top + button.winfo_height(), window.root.winfo_height())
        finally:
            window.root.destroy()

    def test_verified_original_releases_occupancy_without_reusing_approval(self):
        self.repo.load_draft_by_id.return_value = self.snapshot(self.artifact['old_content_hash'])
        self.assertEqual(self.recover()['outcome'], 'not_applied')
        self.assertIsNone(self.store.active_session())
        with self.assertRaises(StateError):
            self.store.claim_transaction(self.tx)

    def test_changed_data_requires_manual_choice_to_keep_and_close(self):
        self.repo.load_draft_by_id.return_value = self.snapshot('external-change')
        self.assertEqual(self.recover()['outcome'], 'changed')
        self.assertIsNotNone(self.store.active_session())
        self.assertEqual(self.recover(close_changed=True)['outcome'], 'closed_changed')
        self.assertIsNone(self.store.active_session())

    def test_relations_are_verified_as_well_as_draft(self):
        self.repo._relations.return_value = [{'id': 'unexpected-relation'}]
        self.assertEqual(self.recover()['outcome'], 'changed')

    def test_legacy_executing_transaction_is_not_assumed_dead(self):
        del self.artifact['execution_lock_version']
        save_artifact(self.path, self.artifact)
        with self.assertRaisesRegex(StateError, '旧版执行记录'):
            self.recover()
        self.assertIsNotNone(self.store.active_session())

    def test_previously_committed_data_reverted_is_not_classified_unwritten(self):
        self.store.update_transaction(self.tx, 'saved')
        self.repo.load_draft_by_id.return_value = self.snapshot(self.artifact['old_content_hash'])
        self.assertEqual(self.recover()['outcome'], 'changed')

    def test_unexecuted_restore_can_be_cancelled_and_retried(self):
        self.store.update_transaction(self.tx, 'saved')
        self.store.update_session(self.session['id'], 'verified')
        restore_path = self.root / 'restore.json'
        save_artifact(restore_path, {})
        restore = self.store.create_transaction(self.session['id'], None, 'restore', str(restore_path))
        self.store.abort_session(self.session['id'])
        self.assertEqual(self.store.get_transaction(restore)['state'], 'cancelled')
        self.assertEqual(self.store.get_session(self.session['id'])['transaction_id'], self.tx)
        self.store.create_transaction(self.session['id'], None, 'restore', str(restore_path))

    def test_failed_restore_keeps_previous_saved_result_available(self):
        self.store.update_transaction(self.tx, 'saved')
        self.store.update_session(self.session['id'], 'verified')
        restore_path = self.root / 'restore.json'
        save_artifact(restore_path, {})
        restore = self.store.create_transaction(self.session['id'], None, 'restore', str(restore_path))
        self.store.claim_transaction(restore)
        self.store.release_failed_transaction(restore, 'rolled_back', 'confirmed rollback')
        current = self.store.get_session(self.session['id'])
        self.assertEqual((current['state'], current['transaction_id']), ('saved', self.tx))

    def test_missing_pending_artifact_can_still_be_cancelled(self):
        self.store.update_transaction(self.tx, 'awaiting_approval')
        self.path.unlink()
        self.store.abort_session(self.session['id'])
        self.assertIsNone(self.store.active_session())
