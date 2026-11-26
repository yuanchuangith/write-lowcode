"""Hidden Tk smoke tests; every configuration, credential and network boundary is fake."""
from __future__ import annotations

import copy
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    import tkinter as tk
    from gxp_project_context.window import ProjectSettingsWindow
except ImportError:
    tk = None


@unittest.skipIf(tk is None, "Tk is not installed")
class ProjectWindowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {
            "GXP_LOWCODE_SHARED_CONFIG": str(base / "connection.json"),
            "GXP_PROJECT_CONFIG_ROOT": str(base / "configs"),
            "GXP_PROJECT_RUNTIME_ROOT": str(base / "runtime"),
            "GXP_LOWCODE_CONFIG": str(base / "look.json"),
            "GXP_LOWCODE_EDITOR_CONFIG": str(base / "write.json"),
            "GXP_LOWCODE_SCHEMA_CONFIG": str(base / "schema.json"),
            "GXP_LOWCODE_EDITOR_STATE": str(base / "editor.sqlite3"),
            "GXP_FORM_CONFIG_ROOT": str(base / "forms"),
        }))
        self.enterContext(patch("keyring.get_password", side_effect=AssertionError("No real credentials")))
        self.enterContext(patch("keyring.set_password", side_effect=AssertionError("No real credentials")))
        self.enterContext(patch("socket.socket.connect", side_effect=AssertionError("No network")))
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as exc:
            self.skipTest("Tk display is unavailable: " + str(exc))
        self.addCleanup(self.root.destroy)
        self.profiles = [self.profile("a", "Alpha"), self.profile("b", "Beta")]
        self.status = {"ok": True, "revision": "initial", "active_project_name": "Alpha",
                       "active_project_id": self.profiles[0]["project_id"], "active_edit_sessions": [],
                       "projects": self.profiles, "refresh": {"status": "partial", "resources": {
                           "schema": {"status": "ready"}, "cpm": {"status": "partial", "error_code": "MODEL_MISSING", "failures": [{"type": "model", "id": "fixture"}]}}}}
        self.backend = self.enterContext(patch("gxp_project_context.window.settings.settings_status", side_effect=lambda: copy.deepcopy(self.status)))
        self.enterContext(patch("gxp_project_context.window.load_profile", side_effect=lambda project_id, revision: copy.deepcopy(next(p for p in self.profiles if p["project_id"] == project_id))))
        self.window = ProjectSettingsWindow(self.root)
        self.pump()

    def profile(self, key, name):
        return {"project_id": key * 64, "revision": key * 64, "active": key == "a", "name": name, "environment": "development",
                "database": {"host": "fixture.invalid", "port": 3306, "database": name.lower()},
                "accounts": {"look": {"user": "reader", "credential_target": "fake-look"},
                             "write": {"user": "writer", "account_mode": "dedicated", "credential_target": "fake-write"}},
                "platform_binding": {"platform_url": "https://fixture.invalid", "app_id": "app", "site_out_id": "site", "account": "actor", "credential_target": "fake-platform", "form_config": {"contract_path": "fixture.json", "api_prefix": "/gxp2"}},
                "platform_version": {}, "refresh": {"timeout_seconds": 75, "http_concurrency": 2, "database_concurrency": 1}}

    def pump(self):
        deadline = time.monotonic() + 2
        while self.window.busy and time.monotonic() < deadline:
            self.window.poll()
            self.root.update_idletasks()
            time.sleep(0.005)
        self.assertFalse(self.window.busy, "Window background callback did not finish")

    def button(self, label, parent=None):
        pending = [self.root if parent is None else parent]
        while pending:
            widget = pending.pop()
            if widget.winfo_class() == "TButton" and widget.cget("text") == label:
                return widget
            pending.extend(widget.winfo_children())
        self.fail("Button missing: " + label)

    def dialog(self, title):
        return next((widget for widget in self.root.winfo_children()
                     if isinstance(widget, tk.Toplevel) and widget.title() == title), None)

    def test_hidden_window_selection_new_project_and_progress(self):
        self.assertEqual("withdrawn", self.root.state())
        self.assertEqual("Alpha", self.window.fields["name"].get())
        self.window.selector.current(1)
        self.window.selected()
        self.assertEqual("b" * 64, self.window.project_id)
        self.assertEqual("beta", self.window.fields["database"].get())
        progress = self.window.progress.get("1.0", "end")
        self.assertIn("部分可用", progress)
        self.assertIn("MODEL_MISSING", progress)
        self.button("新增项目").invoke()
        self.assertEqual("", self.window.project_id)
        self.assertEqual("3306", self.window.fields["port"].get())
        self.assertEqual("", self.window.fields["look_password"].get())

    def test_save_callback_clears_passwords_and_switch_button_uses_selected_project(self):
        self.window.fields["look_password"].set("synthetic-only")
        self.window.fields["write_user"].set("typed-writer")
        self.window.fields["write_password"].set("synthetic-write-only")
        with patch("gxp_project_context.window.settings.save_project", return_value={"ok": True, "project_id": "a" * 64}) as save:
            self.button("保存项目").invoke()
            self.pump()
        args, kwargs = save.call_args
        self.assertEqual("Alpha", args[0]["name"])
        self.assertEqual("initial", args[1])
        self.assertEqual("typed-writer", args[0]["accounts"]["write"]["user"])
        self.assertEqual({"look": "synthetic-only", "write": "synthetic-write-only"}, kwargs["passwords"])
        self.assertEqual("", self.window.fields["look_password"].get())
        self.assertEqual("", self.window.fields["write_password"].get())
        with patch("gxp_project_context.window.settings.switch_project", return_value={"ok": True}) as switch:
            self.button("切换并全量刷新").invoke()
            self.pump()
        switch.assert_called_once_with("a" * 64, "initial")

    def test_refresh_buttons_call_full_and_failed_only_paths(self):
        with patch("gxp_project_context.refresh.refresh_project", return_value={"ok": True}) as refresh:
            self.button("重新全量刷新").invoke()
            self.pump()
            refresh.assert_called_once_with("initial", retry_failed=False)
            refresh.reset_mock()
            self.button("重试未完成资料").invoke()
            self.pump()
            refresh.assert_called_once_with("initial", retry_failed=True)

    def test_login_link_round_trips_through_selection_copy_and_save(self):
        url = "https://fixture.invalid/custom/login-api"
        self.profiles[0]["platform_binding"]["login_url"] = url
        self.window.selector.current(0)
        self.window.selected()
        self.assertEqual(url, self.window.fields["login_url"].get())
        self.window.copy_project()
        self.assertEqual(url, self.window.fields["login_url"].get())
        with patch("gxp_project_context.window.settings.save_project", return_value={"ok": True, "project_id": "a" * 64}) as save:
            self.window.save()
            self.pump()
        self.assertEqual(url, save.call_args.args[0]["platform_binding"]["login_url"])

    def test_new_project_preserves_current_login_link_and_allows_clearing_it(self):
        self.window.fields["login_url"].set("https://fixture.invalid/custom/login-api")
        self.window.new_project()
        self.assertEqual("https://fixture.invalid/custom/login-api", self.window.fields["login_url"].get())
        self.window.fields["host"].set("new-fixture.invalid")
        self.window.fields["database"].set("new-fixture")
        self.window.fields["login_url"].set("")
        self.assertEqual("", self.window.profile()["platform_binding"]["login_url"])

    def test_legacy_form_login_link_is_loaded_when_top_level_field_is_absent(self):
        url = "https://fixture.invalid/legacy/auth"
        binding = self.profiles[0]["platform_binding"]
        binding["form_config"]["login_url"] = url
        self.window.selector.current(0)
        self.window.selected()
        self.assertEqual(url, self.window.fields["login_url"].get())
        self.window.fields["login_url"].set("")
        self.window.apply_platform_defaults({"platform_binding": binding})
        self.assertEqual(url, self.window.fields["login_url"].get())

    def test_copy_keeps_accounts_and_only_needs_new_database_destination(self):
        original_project = self.window.project_id
        self.button("复制当前项目").invoke()
        self.assertEqual("", self.window.project_id)
        self.assertEqual("reader", self.window.fields["look_user"].get())
        self.assertEqual("writer", self.window.fields["write_user"].get())
        self.assertEqual("", self.window.fields["look_password"].get())
        self.assertEqual("", self.window.fields["write_password"].get())
        self.window.fields["host"].set("other.invalid")
        self.window.fields["database"].set("another_db")
        profile = self.window.profile()
        self.assertEqual(original_project, profile["reuse_credentials_from"])
        self.assertEqual("", profile["accounts"]["write"]["expected_grants_sha256"])
        self.assertNotIn("credential_target", profile["platform_binding"]["form_config"])
        with patch("gxp_project_context.window.settings.save_project", return_value={"ok": True, "project_id": "b" * 64}) as save:
            self.button("保存项目").invoke()
            self.pump()
        self.assertEqual({}, save.call_args.kwargs["passwords"])

    def test_legacy_window_prefills_platform_metadata_and_preserves_secret_reference(self):
        self.status["projects"] = []
        self.status["active_project_id"] = "a" * 64
        self.status["settings"] = {"host": "db.invalid", "port": 3306, "database": "legacy",
                                   "look_user": "reader", "write_user": "writer", "account_mode": "shared_unrestricted"}
        self.status["platform_defaults"] = {"platform_binding": self.profile("a", "Alpha")["platform_binding"], "platform_version": {}}
        self.window.project_id = ""
        self.window.run("load", self.window.backend.settings_status) if hasattr(self.window, "backend") else self.window.run("load", lambda: self.status)
        self.pump()
        for key, expected in (("platform_url", "https://fixture.invalid"), ("app_id", "app"), ("site_out_id", "site"), ("account", "actor"), ("api_prefix", "/gxp2")):
            self.assertEqual(expected, self.window.fields[key].get())
        self.assertEqual("", self.window.fields["platform_password"].get())
        self.assertEqual("fake-platform", self.window.profile()["platform_binding"]["credential_target"])
        self.assertIn("不用重新填写", self.window.platform_summary.get())

    def pump_background(self):
        deadline = time.monotonic() + 2
        while self.window.background_busy and time.monotonic() < deadline:
            self.window.poll()
            self.root.update_idletasks()
            time.sleep(0.005)
        self.assertFalse(self.window.background_busy)

    def test_connection_failure_stays_visible_after_background_status_update(self):
        failure = "目标数据库连接测试失败，原项目保留。"
        self.window.run("switch", lambda: {"ok": False, "error": failure})
        self.pump()
        self.assertEqual(failure, self.window.message.get())
        self.status["revision"] = "background-must-not-change-form-revision"
        self.window.run("progress", lambda: copy.deepcopy(self.status))
        self.assertFalse(self.window.busy)
        self.assertEqual("readonly", str(self.window.selector.cget("state")))
        self.pump_background()
        self.assertEqual(failure, self.window.message.get())
        self.assertEqual("initial", self.window.revision)

    def test_session_manager_launch_error_shows_safe_diagnostic(self):
        def fail_to_open_manager():
            raise RuntimeError("Runtime manifest changed after instance startup")

        self.window.run("sessions", fail_to_open_manager)
        self.pump()
        self.assertIn("RuntimeError", self.window.message.get())
        self.assertIn("Runtime manifest changed after instance startup", self.window.message.get())

    def test_idle_window_does_not_keep_querying_progress(self):
        self.window._status_snapshot = {"refresh": None, "active_edit_sessions": []}
        self.backend.reset_mock()
        self.window.refresh_progress()
        self.backend.assert_not_called()
        self.assertFalse(self.window.background_busy)

    def test_idle_window_detects_a_job_started_from_another_entry(self):
        self.window._status_snapshot = {"refresh": {"status": "partial"}, "active_edit_sessions": []}
        self.status["refresh"]["status"] = "fetching"
        self.backend.reset_mock()
        for _ in range(5):
            self.window.refresh_progress()
        self.pump_background()
        self.backend.assert_called_once()
        self.assertEqual("fetching", self.window._status_snapshot["refresh"]["status"])

    def test_saved_project_selector_shows_actual_host_and_port(self):
        labels = self.window.selector.cget("values")
        self.assertIn("fixture.invalid:3306/alpha", labels[0])

    def test_active_marker_is_independent_of_selection_and_moves_after_switch(self):
        def markers():
            return [label.startswith("【当前生效】") for label in self.window.selector.cget("values")]

        alpha, beta = [profile["project_id"] for profile in self.profiles]
        self.assertEqual([True, False], markers())
        self.window.selector.current(1)
        self.window.selected()
        self.assertEqual(beta, self.window.project_id)
        self.assertEqual([True, False], markers())

        def switched(project_id, revision):
            self.assertEqual((beta, "initial"), (project_id, revision))
            self.status.update(active_project_id=beta, active_project_name="Beta", revision="switched")
            return {"ok": True, "project_id": beta}

        with patch("gxp_project_context.window.settings.switch_project", side_effect=switched):
            self.button("切换并全量刷新").invoke()
            self.pump()
        self.assertEqual([False, True], markers())
        self.assertEqual(1, self.window.selector.current())
        self.assertIn("Beta", self.window.current.get())
        self.assertNotEqual(alpha, self.window.project_id)

    def test_background_active_marker_update_keeps_the_target_form(self):
        self.window.selector.current(1)
        self.window.selected()
        self.window.fields["look_password"].set("typed-password")
        self.window.fields["host"].set("typed-target.invalid")
        self.window.message.set("目标表单尚未保存")
        self.status.update(active_project_id=self.profiles[1]["project_id"], active_project_name="Beta",
                           revision="background-update")
        self.window.run("progress", lambda: copy.deepcopy(self.status))
        self.pump_background()
        labels = self.window.selector.cget("values")
        self.assertFalse(labels[0].startswith("【当前生效】"))
        self.assertTrue(labels[1].startswith("【当前生效】"))
        self.assertEqual(1, self.window.selector.current())
        self.assertEqual("typed-password", self.window.fields["look_password"].get())
        self.assertEqual("typed-target.invalid", self.window.fields["host"].get())
        self.assertEqual("目标表单尚未保存", self.window.message.get())
        self.assertEqual("initial", self.window.revision)

    def test_failed_selection_restores_the_loaded_target(self):
        self.window.selector.current(1)
        with patch("gxp_project_context.window.load_profile", side_effect=ValueError):
            self.assertFalse(self.window.selected())
        self.assertEqual(0, self.window.selector.current())
        self.assertEqual(self.profiles[0]["project_id"], self.window.project_id)
        self.assertEqual("alpha", self.window.fields["database"].get())
        self.assertIn("所选项目配置无法加载", self.window.message.get())

    def test_remove_requires_second_button_confirmation_and_reselects_active(self):
        self.window.selector.current(1)
        self.assertTrue(self.window.selected())
        beta = self.profiles[1]["project_id"]

        def remove(project_id, revision):
            self.assertEqual((beta, "initial"), (project_id, revision))
            self.status.update(projects=self.profiles[:1], revision="removed")
            return {"ok": True, "removed": True, "project_id": beta, "revision": "removed"}

        title = "再次确认移除"
        with patch("gxp_project_context.window.messagebox.askyesno", side_effect=[False, True, True]) as ask, \
                patch("gxp_project_context.window.settings.remove_project", side_effect=remove) as backend:
            self.button("移除所选配置").invoke()
            self.assertIsNone(self.dialog(title))
            backend.assert_not_called()
            self.assertEqual(beta, self.window.project_id)

            self.button("移除所选配置").invoke()
            self.root.update_idletasks()
            dialog = self.dialog(title)
            self.assertIsNotNone(dialog)
            self.button("取消", parent=dialog).invoke()
            self.assertIsNone(self.dialog(title))
            backend.assert_not_called()

            self.button("移除所选配置").invoke()
            self.root.update_idletasks()
            dialog = self.dialog(title)
            self.assertIsNotNone(dialog)
            backend.assert_not_called()
            self.button("确认移除", parent=dialog).invoke()
            self.pump()
        self.assertIn("fixture.invalid:3306/beta", ask.call_args.args[1])
        backend.assert_called_once_with(beta, "initial")
        self.assertEqual(1, len(self.window.selector.cget("values")))
        self.assertEqual(self.profiles[0]["project_id"], self.window.project_id)
        self.assertIn("已从项目列表移除", self.window.message.get())

    def test_remove_never_prompts_for_current_or_unselected_config(self):
        with patch("gxp_project_context.window.messagebox.askyesno") as ask, \
                patch("gxp_project_context.window.settings.remove_project") as remove:
            self.button("移除所选配置").invoke()
            self.assertIn("当前生效", self.window.message.get())
            self.window.new_project()
            self.button("移除所选配置").invoke()
            self.assertIn("请选择", self.window.message.get())
        ask.assert_not_called()
        remove.assert_not_called()

    def test_save_current_connection_does_not_save_the_selected_target_form(self):
        self.window.selector.current(1)
        self.window.selected()
        self.window.fields["host"].set("typed-but-not-current.invalid")
        with patch("gxp_project_context.window.settings.save_current_project", return_value={"ok": True, "project_id": "a" * 64}) as save_current, \
             patch("gxp_project_context.window.settings.save_project", side_effect=AssertionError("Do not save the target form")):
            self.button("将原连接加入列表").invoke()
            self.pump()
        save_current.assert_called_once_with("initial")
        self.assertEqual("b" * 64, self.window.project_id)
        self.assertEqual("typed-but-not-current.invalid", self.window.fields["host"].get())

    def test_registering_original_connection_preserves_unsaved_new_project_and_passwords(self):
        self.window.new_project()
        for key, value in {"name": "Unsaved target", "host": "new-target.invalid", "database": "target",
                           "write_user": "typed-writer", "look_password": "synthetic-look-only",
                           "write_password": "synthetic-write-only", "platform_password": "synthetic-platform-only"}.items():
            self.window.fields[key].set(value)
        before = {key: variable.get() for key, variable in self.window.fields.items()}
        def registered(expected):
            self.assertEqual("initial", expected)
            self.status["revision"] = "after-registration"
            self.status["projects"] = list(reversed(self.profiles))
            return {"ok": True, "project_id": "a" * 64}
        with patch("gxp_project_context.window.settings.save_current_project", side_effect=registered), \
             patch("gxp_project_context.window.settings.save_project", side_effect=AssertionError("Do not save target form")):
            self.button("将原连接加入列表").invoke()
            self.pump()
        self.assertEqual(before, {key: variable.get() for key, variable in self.window.fields.items()})
        self.assertEqual("", self.window.project_id)
        self.assertEqual("新增项目", self.window.selector.get())
        self.assertEqual("after-registration", self.window.revision)
        self.assertIn("原连接已加入项目列表", self.window.message.get())

    def test_original_connection_error_keeps_new_project_inputs(self):
        self.window.new_project()
        self.window.fields["write_user"].set("typed-writer")
        self.window.fields["write_password"].set("synthetic-write-only")
        failure = "正在使用的原连接缺少 Write 账号，请点击“保存项目”保存当前填写的内容。"
        with patch("gxp_project_context.window.settings.save_current_project", return_value={"ok": False, "error": failure}):
            self.button("将原连接加入列表").invoke()
            self.pump()
        self.assertEqual(failure, self.window.message.get())
        self.assertEqual("typed-writer", self.window.fields["write_user"].get())
        self.assertEqual("synthetic-write-only", self.window.fields["write_password"].get())

    def test_unchanged_status_does_not_clear_or_redraw_progress_text(self):
        self.window.render_progress(copy.deepcopy(self.status))
        with patch.object(self.window.progress, "delete") as clear, patch.object(self.window.progress, "insert") as insert:
            self.window.render_progress(copy.deepcopy(self.status))
        clear.assert_not_called()
        insert.assert_not_called()

    def test_completed_refresh_shows_final_state_and_duration(self):
        status = copy.deepcopy(self.status)
        status["refresh"].update(started_at="2026-11-09T12:00:00+00:00",
                                 completed_at="2026-11-09T12:00:10+00:00")
        self.window.render_progress(status)
        text = self.window.progress.get("1.0", "end")
        self.assertIn("本次耗时：0分10秒", text)
        self.assertIn("本次刷新已结束", text)
        self.assertIn("MODEL_MISSING", text)

    def test_resource_start_is_visible_before_collection_finishes(self):
        status = copy.deepcopy(self.status)
        status["refresh"] = {"status": "fetching", "resources": {"schema": {"status": "fetching"}}}
        self.window.render_progress(status)
        text = self.window.progress.get("1.0", "end")
        self.assertIn("schema：正在获取", text)

    def test_stale_background_result_cannot_unlock_foreground_or_replace_message(self):
        old_epoch = self.window._view_epoch
        self.window.background_busy = True
        self.window.selector.current(1)
        self.window.selected()
        self.window.busy = True
        self.window.selector.configure(state="disabled")
        self.window.message.set("正在测试连接并切换项目…")
        self.window.results.put(("progress", copy.deepcopy(self.status), old_epoch))
        self.window.poll()
        self.assertTrue(self.window.busy)
        self.assertEqual("disabled", str(self.window.selector.cget("state")))
        self.assertEqual("正在测试连接并切换项目…", self.window.message.get())
        self.assertEqual("b" * 64, self.window.project_id)
        self.window.busy = False

    def test_polling_stops_after_refresh_finishes(self):
        self.window._status_snapshot = {"refresh": {"status": "fetching"}, "active_edit_sessions": []}
        self.status["refresh"]["status"] = "ready"
        self.backend.reset_mock()
        self.window.refresh_progress()
        self.pump_background()
        self.assertEqual(1, self.backend.call_count)
        self.window.refresh_progress()
        self.assertEqual(1, self.backend.call_count)


if __name__ == "__main__":
    unittest.main()
