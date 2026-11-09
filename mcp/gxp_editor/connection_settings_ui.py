"""Manual entry for shared development database settings (stdlib Tk only)."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any


class ConnectionSettingsWindow:
    """Keep secrets inside this process and run connection checks off the UI thread."""

    def __init__(self, root: tk.Tk, backend: Any = None, *, status: dict | None = None):
        self.root = root
        self.backend = backend or importlib.import_module(f"{__package__}.connection_settings")
        self.revision = ""
        self.busy = False
        self.closed = False
        self.results: queue.Queue = queue.Queue()
        self.projects: list[dict] = []
        self.selected_project_id = ""
        self.project_name = tk.StringVar(root)
        self.current_connection = tk.StringVar(root)
        self.fields = {name: tk.StringVar(root) for name in (
            "host", "port", "database", "look_user", "write_user",
            "look_password", "write_password",
        )}
        self.message = tk.StringVar(root)
        self.location = tk.StringVar(root)
        self.sessions = tk.StringVar(root)
        self._build()
        if status is not None:
            self._apply_status(status)
        else:
            self.reload()
        self.root.after(100, self._poll)

    def _build(self) -> None:
        self.root.title("GXP 开发库设置 · Look / Write")
        self.root.minsize(620, 530)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        style = ttk.Style(self.root)
        style.configure("Settings.TFrame", padding=22)
        style.configure("Settings.Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Settings.Hint.TLabel", foreground="#526174")
        style.configure("Settings.Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        frame = ttk.Frame(self.root, style="Settings.TFrame")
        frame.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        ttk.Label(frame, text="开发数据库设置", style="Settings.Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(frame, text="Look 与 Write 共用地址和库名，各自保留连接账号。",
                  style="Settings.Hint.TLabel").grid(row=1, column=0, columnspan=2,
                                                       sticky="w", pady=(7, 18))
        self.entries: list[ttk.Entry] = []
        for row, (name, label) in enumerate((
            ("host", "服务器地址"), ("port", "端口"), ("database", "数据库名"),
        ), start=2):
            self._field(frame, row, name, label)
        ttk.Separator(frame).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(13, 10))
        for row, (name, label) in enumerate((
            ("look_user", "Look 账号"), ("look_password", "Look 密码"),
            ("write_user", "Write 账号"), ("write_password", "Write 密码"),
        ), start=6):
            self._field(frame, row, name, label, password=name.endswith("password"))
        ttk.Label(frame,
                  text="密码留空时使用已保存的凭据；新密码仅保存到操作系统凭据库。\n"
                       "“测试连接”检查两个账号，测试本身不保存设置或密码。",
                  style="Settings.Hint.TLabel", wraplength=560).grid(
                      row=10, column=0, columnspan=2, sticky="w", pady=(9, 8))
        ttk.Label(frame, textvariable=self.sessions, style="Settings.Hint.TLabel",
                  wraplength=560).grid(row=11, column=0, columnspan=2, sticky="w", pady=(0, 8))
        self.message_label = ttk.Label(frame, textvariable=self.message, wraplength=560)
        self.message_label.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(2, 8))
        toolbar = ttk.Frame(frame)
        toolbar.grid(row=13, column=0, columnspan=2, sticky="ew", pady=(6, 12))
        toolbar.columnconfigure(1, weight=1)
        self.reload_button = ttk.Button(toolbar, text="重新加载", command=self.reload)
        self.reload_button.grid(row=0, column=0)
        self.test_button = ttk.Button(toolbar, text="测试连接", command=lambda: self.submit(test_only=True))
        self.test_button.grid(row=0, column=2, padx=(8, 0))
        self.save_button = ttk.Button(toolbar, text="保存设置", command=self.submit)
        self.save_button.grid(row=0, column=3, padx=8)
        self.cancel_button = ttk.Button(toolbar, text="关闭", command=self.close)
        self.cancel_button.grid(row=0, column=4)
        self.sessions_button = ttk.Button(toolbar, text="处理未结束流程", command=self.open_sessions)
        self.sessions_button.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))
        self.buttons = (self.reload_button, self.test_button, self.save_button, self.cancel_button, self.sessions_button)
        ttk.Label(frame, textvariable=self.location, style="Settings.Hint.TLabel",
                  wraplength=560).grid(row=14, column=0, columnspan=2, sticky="w")
        saved = ttk.LabelFrame(frame, text="已保存的数据库配置", padding=10)
        saved.grid(row=15, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        saved.columnconfigure(0, weight=1)
        self.project_selector = ttk.Combobox(saved, state="readonly")
        self.project_selector.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        self.project_selector.bind("<<ComboboxSelected>>", self.load_selected_project)
        self.switch_project_button = ttk.Button(saved, text="切换所选配置", command=self.switch_selected_project)
        self.switch_project_button.grid(row=0, column=1)
        name_row = ttk.Frame(saved)
        name_row.grid(row=1, column=0, columnspan=2, sticky="ew", pady=8)
        name_row.columnconfigure(1, weight=1)
        ttk.Label(name_row, text="配置名称").grid(row=0, column=0, padx=(0, 8))
        self.project_name_entry = ttk.Entry(name_row, textvariable=self.project_name)
        self.project_name_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        self.save_project_button = ttk.Button(name_row, text="另存配置", command=self.save_project)
        self.save_project_button.grid(row=0, column=2)
        ttk.Label(saved, text="选择只载入地址；切换才生效。另存配置保留原账号凭据，不测试连接。",
                  style="Settings.Hint.TLabel", wraplength=560).grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(saved, textvariable=self.current_connection, style="Settings.Hint.TLabel",
                  wraplength=560).grid(row=3, column=0, columnspan=2, sticky="w", pady=(5, 0))
        self.entries.append(self.project_name_entry)
        self.buttons += (self.switch_project_button, self.save_project_button)

    def _field(self, frame: ttk.Frame, row: int, name: str, label: str, *, password=False) -> None:
        ttk.Label(frame, text=label, width=13).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(frame, textvariable=self.fields[name], show="●" if password else "")
        entry.grid(row=row, column=1, sticky="ew", pady=5)
        self.entries.append(entry)

    def _set_message(self, message: str, *, error=False) -> None:
        self.message.set(message)
        self.message_label.configure(foreground="#a32323" if error else "#165a38")

    def _apply_status(self, status: dict) -> None:
        self.revision = str(status.get("revision") or "")
        settings = status.get("settings") or {}
        for name in ("host", "port", "database", "look_user", "write_user"):
            value = settings.get(name)
            self.fields[name].set(str(value) if value is not None else ("3306" if name == "port" else ""))
        self._clear_passwords()
        sessions = status.get("active_edit_sessions") or 0
        count = len(sessions) if isinstance(sessions, (list, tuple, dict)) else sessions
        self.sessions.set(
            f"有 {count} 个编辑会话尚未结束，请先完成或取消会话后再保存。" if count
            else "保存后，后续 Look / Write 调用自动使用新连接，不需要重新安装。"
        )
        path = status.get("config_path")
        self.location.set(f"设置文件：{path}" if path else "")
        self._current_summary(status)
        if isinstance(status.get("projects"), list):
            self._apply_projects(status["projects"])
        message = "已加载现有设置。" if status.get("configured") else "请填写连接信息并测试后保存。"
        legacy = status.get("legacy_connections") or {}
        targets = [tuple(str((legacy.get(role) or {}).get(key) or "").strip()
                         for key in ("host", "port", "database")) for role in ("look", "write")]
        if (not status.get("configured") and all(all(target) for target in targets)
                and (targets[0][0].casefold(), *targets[0][1:]) != (targets[1][0].casefold(), *targets[1][1:])):
            message = "原 Look / Write 目标库不同，保存将统一到当前显示的目标库。"
        self._set_message(message)

    def _current_summary(self, status: dict) -> None:
        values = status.get("settings") or {}
        self.current_connection.set("当前生效：" + str(values.get("host") or "未配置")
                                    + ":" + str(values.get("port") or "") + "/" + str(values.get("database") or ""))

    def _apply_projects(self, projects: list[dict]) -> None:
        self.projects = projects
        labels = []
        for project in projects:
            db = project.get("database") or {}
            labels.append(str(project.get("name") or db.get("database") or "未命名") + " · "
                          + str(db.get("host") or "") + "/" + str(db.get("database") or ""))
        self.project_selector.configure(values=labels)
        index = next((i for i, project in enumerate(projects) if project.get("project_id") == self.selected_project_id), -1)
        if index >= 0:
            self.project_selector.current(index)
        else:
            self.project_selector.set("")

    def _selected_project(self):
        index = self.project_selector.current()
        return self.projects[index] if 0 <= index < len(self.projects) else None

    def load_selected_project(self, event=None) -> None:
        if self.busy:
            return
        project = self._selected_project()
        if project is None:
            return
        self.selected_project_id = project["project_id"]
        self.project_name.set(str(project.get("name") or ""))
        for key in ("host", "port", "database"):
            self.fields[key].set(str((project.get("database") or {}).get(key) or ""))
        self._set_message("已载入所选地址，尚未切换；点击“切换所选配置”后生效。")

    def _load_status(self) -> dict:
        status = self.backend.settings_status()
        if status.get("ok", True) and callable(getattr(self.backend, "list_projects", None)):
            try:
                projects = self.backend.list_projects()
                if projects.get("ok", True):
                    status = {**status, "projects": projects.get("projects", [])}
            except Exception:
                pass  # The original connection controls remain usable.
        return status

    def _clear_passwords(self) -> None:
        self.fields["look_password"].set("")
        self.fields["write_password"].set("")

    def reload(self) -> None:
        if not self.busy:
            self._start("load", self._load_status)

    def open_sessions(self) -> None:
        if not self.busy:
            self._start("sessions", self.backend.open_sessions)

    def save_project(self) -> None:
        if self.busy:
            return
        changes = {key: self.fields[key].get().strip() for key in ("host", "port", "database")}
        try:
            changes["port"] = int(changes["port"])
            if not changes["host"] or not changes["database"] or not 1 <= changes["port"] <= 65535:
                raise ValueError
        except ValueError:
            self._set_message("请填写有效的服务器地址、端口和数据库名。", error=True)
            return
        name = self.project_name.get().strip() or f"{changes['host']}:{changes['port']}/{changes['database']}"
        revision = self.revision
        def operation():
            result = self.backend.save_project(name, revision, changes=changes)
            if result.get("ok"):
                try:
                    result = {**result, "refreshed_status": self._load_status()}
                except Exception:
                    pass
            return result
        self._start("project_save", operation)

    def switch_selected_project(self) -> None:
        if self.busy:
            return
        project = self._selected_project()
        if project is None:
            self._set_message("请先选择已保存的数据库配置。", error=True)
            return
        project_id, revision = project["project_id"], self.revision
        def operation():
            result = self.backend.switch_project(project_id, revision)
            if result.get("ok"):
                try:
                    result = {**result, "refreshed_status": self._load_status()}
                except Exception:
                    pass
            return result
        self._start("project_switch", operation)

    def submit(self, *, test_only=False) -> None:
        if self.busy:
            return
        changes = {name: self.fields[name].get().strip() for name in (
            "host", "port", "database", "look_user", "write_user",
        )}
        if not all(changes.values()):
            self._set_message("请填写服务器地址、端口、数据库名及两个账号。", error=True)
            return
        try:
            port = int(changes["port"])
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            self._set_message("端口应为 1 到 65535 之间的整数。", error=True)
            return
        changes["port"] = port
        look_password = self.fields["look_password"].get() or None
        write_password = self.fields["write_password"].get() or None
        revision = self.revision

        def operation():
            result = self.backend.update_settings(
                changes, revision, look_password=look_password,
                write_password=write_password, test_only=test_only,
            )
            if result.get("ok") and not test_only:
                # A save may succeed even if a subsequent status refresh fails.
                try:
                    result = {**result, "refreshed_status": self._load_status()}
                except Exception:
                    pass
            return result

        self._start("test" if test_only else "save", operation)

    def _start(self, action: str, operation) -> None:
        self.busy = True
        for widget in (*self.entries, *self.buttons):
            widget.configure(state="disabled")
        self.project_selector.configure(state="disabled")
        self._set_message({"load": "正在加载设置…", "test": "正在测试两个账号的连接…",
                           "save": "正在检查连接并保存设置…", "project_save": "正在另存配置…",
                           "project_switch": "正在测试并切换所选配置…", "sessions": "正在打开未结束流程窗口…"}[action])

        def run():
            try:
                self.results.put((action, operation(), False))
            except Exception:
                # Exception text can contain connection details or secrets.
                self.results.put((action, None, True))

        threading.Thread(target=run, name="gxp-database-settings", daemon=True).start()

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            action, result, failed = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self._poll)
            return
        self.busy = False
        for widget in (*self.entries, *self.buttons):
            widget.configure(state="normal")
        self.project_selector.configure(state="readonly")
        if failed or not isinstance(result, dict):
            self._set_message("操作未完成。请检查设置或重试；密码不会显示在错误信息中。", error=True)
        elif action == "load":
            if result.get("ok") is False:
                self._show_error(result)
            else:
                self._apply_status(result)
        elif not result.get("ok"):
            self._show_error(result)
        elif action == "test":
            self._set_message("两个账号连接测试成功，设置尚未保存。")
        elif action == "sessions":
            self._set_message("请在流程窗口确认处理，处理完点击刷新列表，再重新加载连接设置后切换。")
        elif action == "project_save":
            project = result.get("project") or {}
            self.selected_project_id = project.get("project_id") or result.get("project_id") or self.selected_project_id
            status = result.get("refreshed_status")
            if isinstance(status, dict):
                self.revision = str(status.get("revision") or self.revision)
                self._current_summary(status)
                self._apply_projects(status.get("projects", self.projects))
            else:
                self.revision = str(result.get("revision") or self.revision)
            self._set_message("配置已另存，当前连接未切换；选择配置后可点击“切换所选配置”。")
        else:
            status = result.get("refreshed_status")
            if isinstance(status, dict):
                self._apply_status(status)
            else:
                self.revision = str(result.get("revision") or self.revision)
                self._clear_passwords()
            self._set_message("已切换所选配置，后续 Look / Write 调用使用新连接。" if action == "project_switch"
                              else "设置已保存。后续 Look / Write 调用自动使用新连接，不需要重新安装。")
        self.root.after(100, self._poll)

    def _show_error(self, result: dict) -> None:
        error = result.get("error") or result.get("message") or "操作未完成，请检查设置后重试。"
        if isinstance(error, dict):
            error = error.get("message") or error.get("code") or "操作未完成，请检查设置后重试。"
        # The backend returns safe messages; also remove currently entered secrets.
        message = str(error)
        for name in ("look_password", "write_password"):
            password = self.fields[name].get()
            if password:
                message = message.replace(password, "[密码已隐藏]")
        self._set_message(message[:1200], error=True)

    def close(self) -> None:
        if self.busy:
            return
        self.closed = True
        self._clear_passwords()
        self.root.destroy()


def _write_startup_receipt() -> None:
    """Acknowledge an initialized window to its launcher without other data."""
    value = os.environ.pop("GXP_LOWCODE_SETTINGS_RECEIPT", "")
    if not value:
        return
    try:
        path = Path(value)
        temp_root = Path(tempfile.gettempdir()).resolve()
        if not path.is_absolute() or not path.parent.resolve().is_relative_to(temp_root):
            return
        # The launcher owns a private temporary directory and supplies a fresh
        # filename. Never follow an existing file or replace unrelated data.
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"ready": True}, stream)
    except (OSError, ValueError):
        pass


def main() -> int:
    root = tk.Tk()
    from gxp_project_context.window import ProjectSettingsWindow
    ProjectSettingsWindow(root)
    root.update_idletasks()
    root.deiconify()
    root.update()
    _write_startup_receipt()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
