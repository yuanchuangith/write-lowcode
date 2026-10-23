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
        self.buttons = (self.reload_button, self.test_button, self.save_button, self.cancel_button)
        ttk.Label(frame, textvariable=self.location, style="Settings.Hint.TLabel",
                  wraplength=560).grid(row=14, column=0, columnspan=2, sticky="w")

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
        message = "已加载现有设置。" if status.get("configured") else "请填写连接信息并测试后保存。"
        legacy = status.get("legacy_connections") or {}
        targets = [tuple(str((legacy.get(role) or {}).get(key) or "").strip()
                         for key in ("host", "port", "database")) for role in ("look", "write")]
        if (not status.get("configured") and all(all(target) for target in targets)
                and (targets[0][0].casefold(), *targets[0][1:]) != (targets[1][0].casefold(), *targets[1][1:])):
            message = "原 Look / Write 目标库不同，保存将统一到当前显示的目标库。"
        self._set_message(message)

    def _clear_passwords(self) -> None:
        self.fields["look_password"].set("")
        self.fields["write_password"].set("")

    def reload(self) -> None:
        if not self.busy:
            self._start("load", self.backend.settings_status)

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
                    result = {**result, "refreshed_status": self.backend.settings_status()}
                except Exception:
                    pass
            return result

        self._start("test" if test_only else "save", operation)

    def _start(self, action: str, operation) -> None:
        self.busy = True
        for widget in (*self.entries, *self.buttons):
            widget.configure(state="disabled")
        self._set_message({"load": "正在加载设置…", "test": "正在测试两个账号的连接…",
                           "save": "正在检查连接并保存设置…"}[action])

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
        else:
            status = result.get("refreshed_status")
            if isinstance(status, dict):
                self._apply_status(status)
            else:
                self.revision = str(result.get("revision") or self.revision)
                self._clear_passwords()
            self._set_message("设置已保存。后续 Look / Write 调用自动使用新连接，不需要重新安装。")
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
    ConnectionSettingsWindow(root)
    root.update_idletasks()
    root.deiconify()
    root.update()
    _write_startup_receipt()
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
