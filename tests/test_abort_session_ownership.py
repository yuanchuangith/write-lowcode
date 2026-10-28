from __future__ import annotations

import importlib.util
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import PLUGIN_ROOT, baseline
from tests.test_abort_session_responsiveness import drain_operations
from gxp_editor.state import StateError, StateStore


class AbortSessionOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = StateStore(Path(self.temp.name) / "state.sqlite3")
        self.owner = self.state.create_conversation("对话一")["conversation_id"]
        self.other = self.state.create_conversation("对话二")["conversation_id"]
        self.session = self.state.create_session(baseline(), "backup", "compiler", conversation_id=self.owner)
        spec = importlib.util.spec_from_file_location("abort_session_ownership", PLUGIN_ROOT / "scripts" / "abort_session_app.py")
        self.app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app)
        self.window = object.__new__(self.app.AbortSessionWindow)
        self.window.state = self.state
        self.window.session_id = self.session["id"]
        self.window.conversation_id = self.owner
        self.window.owner_revision = 0
        self.window.root = Mock()
        self.window._initialize_operations()

    def transfer(self, destination):
        current = self.state.get_session(self.session["id"])
        return self.state.transfer_session(
            self.session["id"], destination, expected_owner=current["conversation_id"],
            expected_revision=current["owner_revision"],
        )

    def test_stale_launch_and_unmarked_cli_do_not_open_ui(self):
        for args in (
            ["abort_session_app.py", self.session["id"]],
            ["abort_session_app.py", self.session["id"], self.other, "0"],
            ["abort_session_app.py", self.session["id"], self.owner, "1"],
        ):
            with self.subTest(args=args), patch.object(self.app, "StateStore", return_value=self.state), \
                 patch.object(self.app.sys, "argv", args), patch.object(self.app.tk, "Tk") as create_root, \
                 patch.object(self.app.messagebox, "showerror") as error:
                self.assertEqual(self.app.main(), 1)
                self.assertIn("已转交", error.call_args.args[1])
                create_root.assert_not_called()

    def test_window_title_identifies_its_owner(self):
        root_factory = tk.Tk
        def hidden_root():
            root = root_factory()
            root.withdraw()
            return root
        with patch.object(self.app, "StateStore", return_value=self.state), patch.object(self.app.tk, "Tk", side_effect=hidden_root):
            try:
                window = self.app.AbortSessionWindow(self.session["id"], self.owner, 0)
            except tk.TclError as exc:
                self.skipTest(f"Tk display unavailable: {exc}")
        try:
            self.assertIn(self.owner, window.root.title())
            self.assertIn(self.session["id"][:8], window.root.title())
        finally:
            window.root.destroy()

    def test_transferred_window_cannot_cancel_reopen_recover_or_finish(self):
        self.transfer(self.other)
        with patch.object(self.app.messagebox, "showerror") as error, \
             patch.object(self.app, "EditorService") as service, patch("gxp_editor.recovery.recover_session") as recover:
            for action in (self.window.abort, self.window.reopen, self.window.recover):
                with self.subTest(action=action.__name__):
                    action()
                    drain_operations(self.window)
                    self.assertIn("已转交", error.call_args.args[1])
            with self.assertRaises(StateError):
                self.window._finish()
            service.assert_not_called()
            recover.assert_not_called()
        current = self.state.get_session(self.session["id"])
        self.assertEqual((current["state"], current["conversation_id"]), ("locked", self.other))
        self.window.root.destroy.assert_not_called()

    def test_transfer_away_and_back_does_not_restore_stale_window(self):
        self.transfer(self.other)
        self.transfer(self.owner)
        with patch.object(self.app.messagebox, "showerror") as error:
            self.window.abort()
            drain_operations(self.window)
            self.assertIn("已转交", error.call_args.args[1])
        self.assertEqual(self.state.get_session(self.session["id"])["state"], "locked")

    def test_cancel_rechecks_owner_atomically_at_mutation(self):
        original = self.window._owned_session
        def transfer_after_check():
            session = original()
            self.transfer(self.other)
            return session
        with patch.object(self.window, "_owned_session", side_effect=transfer_after_check), \
             patch.object(self.app.messagebox, "showerror") as error:
            self.window.abort()
            drain_operations(self.window)
            error.assert_called_once()
        self.assertEqual(self.state.get_session(self.session["id"])["state"], "locked")

    def test_finish_rechecks_owner_atomically_at_mutation(self):
        self.state.update_session(self.session["id"], "verified", conversation_id=self.owner, owner_revision=0)
        original = self.window._owned_session
        def transfer_after_check():
            session = original()
            self.transfer(self.other)
            return session
        with patch.object(self.window, "_owned_session", side_effect=transfer_after_check):
            with self.assertRaises(StateError):
                self.window._finish()
        self.assertEqual(self.state.get_session(self.session["id"])["state"], "verified")

    def test_reopen_uses_scoped_service_and_snapshot_revision(self):
        self.state.update_session(self.session["id"], "awaiting_approval", transaction_id="tx-1", conversation_id=self.owner, owner_revision=0)
        service = Mock()
        service.reopen_approval.return_value = {"approval_window_opened": True}
        with patch.object(self.app, "EditorService", return_value=service) as factory:
            self.window.reopen()
            drain_operations(self.window)
            factory.assert_called_once_with(conversation_id=self.owner)
            service.reopen_approval.assert_called_once_with("tx-1", owner_revision=0)
        self.window.root.destroy.assert_called_once()

    def test_failed_reopen_keeps_handling_window(self):
        self.state.update_session(self.session["id"], "awaiting_approval", transaction_id="tx-1", conversation_id=self.owner, owner_revision=0)
        service = Mock()
        service.reopen_approval.return_value = {"approval_window_opened": False, "approval_window_error": "launch failed"}
        with patch.object(self.app, "EditorService", return_value=service), patch.object(self.app.messagebox, "showerror") as error:
            self.window.reopen()
            drain_operations(self.window)
            self.assertIn("launch failed", error.call_args.args[1])
        self.window.root.destroy.assert_not_called()

    def test_recovery_forwards_owner_and_finishes_verified_result(self):
        self.state.update_session(self.session["id"], "saved", conversation_id=self.owner, owner_revision=0)
        def verified(*args, **kwargs):
            self.state.update_session(self.session["id"], "verified", conversation_id=self.owner, owner_revision=0)
            return {"outcome": "saved"}
        with patch("gxp_editor.recovery.recover_session", side_effect=verified) as recover, \
             patch.object(self.app.messagebox, "showinfo"):
            self.window.recover()
            drain_operations(self.window)
            recover.assert_called_once_with(
                self.session["id"], state=self.state, conversation_id=self.owner, owner_revision=0,
            )
        self.assertEqual(self.state.get_session(self.session["id"])["state"], "finished")

    def test_transfer_during_confirmation_prevents_second_recovery(self):
        self.state.update_session(self.session["id"], "saved", conversation_id=self.owner, owner_revision=0)
        def transfer_during_prompt(*args, **kwargs):
            self.transfer(self.other)
            return True
        with patch("gxp_editor.recovery.recover_session", return_value={"outcome": "changed"}) as recover, \
             patch.object(self.app.messagebox, "askyesno", side_effect=transfer_during_prompt), \
             patch.object(self.app.messagebox, "showerror") as error:
            self.window.recover()
            drain_operations(self.window)
            self.assertIn("已转交", error.call_args.args[1])
            self.assertEqual(recover.call_count, 1)
        self.assertEqual(self.state.get_session(self.session["id"])["state"], "saved")


if __name__ == "__main__":
    unittest.main()
