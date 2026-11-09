"""The blocker window only lists state and opens the existing human handler."""
import importlib.util
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

try:
    import tkinter as tk
except ImportError:
    tk = None

SPEC = importlib.util.spec_from_file_location("project_session_manager_window", Path(__file__).resolve().parents[1] / "scripts/manage_project_sessions.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class SessionHandlerContractTests(unittest.TestCase):
    def test_open_handler_only_delegates_to_existing_human_confirmation(self):
        import sys
        from types import ModuleType
        service = ModuleType("gxp_editor.service")
        service.EditorService = Mock()
        row = {"id": "synthetic", "domain": "action", "kind": "session", "conversation_id": "owner"}
        with patch.dict(sys.modules, {"gxp_editor.service": service}):
            MODULE.SessionManagerWindow._open_handler(row)
        service.EditorService.assert_called_once_with(conversation_id="owner")
        service.EditorService.return_value.abort_edit_session.assert_called_once_with("synthetic")
        service.EditorService.return_value.release_local_session.assert_not_called()

    def test_form_and_orphan_transactions_require_their_original_handler(self):
        for row in ({"domain": "form", "id": "form"},
                    {"domain": "action", "kind": "transaction", "id": "orphan"}):
            with self.subTest(row=row), self.assertRaises(RuntimeError):
                MODULE.SessionManagerWindow._open_handler(row)


@unittest.skipIf(tk is None, "Tk unavailable")
class ProjectSessionManagerTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError:
            self.skipTest("Tk display unavailable")
        self.addCleanup(self.root.destroy)
        self.row = {"id": "a" * 32, "domain": "action", "action_name": "Synthetic old approval",
                    "action_code": "CODE", "state": "awaiting_approval", "conversation_id": "dialog"}
        self.loader = Mock(return_value=[self.row])
        self.opener = Mock(return_value={"confirmation_window_opened": True})
        self.window = MODULE.SessionManagerWindow(self.root, loader=self.loader, opener=self.opener)

    def test_initial_display_does_not_cancel_or_open_any_flow(self):
        self.assertEqual(1, len(self.window.table.get_children()))
        self.assertIn("Synthetic old approval", str(self.window.table.item("0", "values")))
        self.opener.assert_not_called()
        self.assertIn("1 个独立流程", self.window.message.get())

    def test_selection_opens_only_the_human_handler_then_can_refresh(self):
        self.window.table.selection_set("0")
        self.window.handle()
        deadline = time.monotonic() + 2
        while self.window.busy and time.monotonic() < deadline:
            self.window.poll()
            time.sleep(0.01)
        self.opener.assert_called_once_with(self.row)
        self.assertIn("选择处理方式", self.window.message.get())
        self.loader.return_value = []
        self.window.reload()
        self.assertIn("可以回到项目窗口切换数据库", self.window.message.get())
