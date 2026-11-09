"""Local multi-project settings. Passwords never leave this GUI process."""
from __future__ import annotations

import json
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from . import ProjectError
from . import settings
from .context import _base_config, load_profile, read_json, active_path, database_scope


class ProjectSettingsWindow:
    def __init__(self, root):
        self.root, self.busy, self.closed = root, False, False
        self.background_busy = False
        self._view_epoch = 0
        self._status_snapshot = None
        self._progress_text = None
        self.results = queue.Queue()
        self.revision, self.project_id = "", ""
        self.credential_source_id, self.legacy_source_id = "", ""
        self.projects = []
        self.message = tk.StringVar(root)
        self.current = tk.StringVar(root)
        self.credentials_summary = tk.StringVar(root)
        self.platform_summary = tk.StringVar(root)
        self.platform_credential_target, self.platform_credential_user = "", ""
        self.fields = {name: tk.StringVar(root) for name in (
            "name", "environment", "host", "port", "database", "look_user", "look_password", "write_user", "write_password",
            "account_mode", "expected_grants_sha256", "platform_url", "app_id", "site_out_id", "account", "platform_password",
            "contract_path", "api_prefix")}
        self.form_template, self.platform_version, self.refresh_config = {}, {}, {}
        self._build()
        self.run("load", settings.settings_status)
        root.after(100, self.poll)
        root.after(2000, self.refresh_progress)

    def _build(self):
        self.root.title("GXP 项目配置 · Look / Write / 表单")
        self.root.geometry("900x750")
        self.root.minsize(820, 650)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="项目配置与一键切换", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.current, wraplength=850).pack(anchor="w", pady=(6, 10))
        row = ttk.Frame(frame)
        row.pack(fill="x")
        self.selector = ttk.Combobox(row, state="readonly")
        self.selector.pack(side="left", fill="x", expand=True)
        self.selector.bind("<<ComboboxSelected>>", self.selected)
        ttk.Button(row, text="复制当前项目", command=self.copy_project).pack(side="left", padx=6)
        ttk.Button(row, text="新增项目", command=self.new_project).pack(side="left", padx=6)
        ttk.Button(row, text="重新加载", command=lambda: self.run("load", settings.settings_status)).pack(side="left")
        ttk.Button(row, text="处理未结束流程", command=self.manage_sessions).pack(side="left", padx=6)
        book = ttk.Notebook(frame)
        book.pack(fill="both", expand=True, pady=12)
        connection = ttk.Frame(book, padding=14)
        platform = ttk.Frame(book, padding=14)
        progress = ttk.Frame(book, padding=14)
        advanced = ttk.Frame(book, padding=14)
        platform_advanced = ttk.Frame(book, padding=14)
        book.add(connection, text="项目与数据库")
        book.add(platform, text="平台与表单")
        book.add(progress, text="刷新进度与缺项")
        book.add(advanced, text="高级账户设置")
        book.add(platform_advanced, text="平台高级参数")
        for tab in (connection, platform, advanced, platform_advanced):
            tab.columnconfigure(1, weight=1)
        for row, (key, label) in enumerate((
            ("name", "项目名称"), ("environment", "环境"), ("host", "服务器地址"), ("port", "端口"),
            ("database", "数据库名"))):
            self.field(connection, row, key, label)
        ttk.Label(connection, textvariable=self.credentials_summary, wraplength=760).grid(row=5, column=0, columnspan=2, sticky="w", pady=14)
        ttk.Label(connection, text="只需修改服务器地址和数据库名，原 Look / Write 账号、密码和账户模式全部保留。保存为独立项目后即可一键切换。", wraplength=760).grid(row=6, column=0, columnspan=2, sticky="w", pady=10)
        for row, (key, label) in enumerate((("look_user", "Look 账号"), ("look_password", "更换 Look 密码"),
            ("write_user", "Write 账号"), ("write_password", "更换 Write 密码"),
            ("account_mode", "Write 账户模式"), ("expected_grants_sha256", "已核验权限指纹"))):
            self.field(advanced, row, key, label)
        ttk.Label(advanced, text="已自动沿用原配置，无需修改本页。密码框用于主动更换密码；保持空白会继续使用原密码，不会清空原密码。", wraplength=760).grid(row=7, column=0, columnspan=2, sticky="w", pady=14)
        self.field(platform, 0, "platform_url", "项目网页地址")
        ttk.Label(platform, textvariable=self.platform_summary, wraplength=760).grid(row=1, column=0, columnspan=2, sticky="w", pady=14)
        ttk.Label(platform, text="已有的平台账号、密码、应用编号和连接参数自动沿用。仅当项目网页地址变化时修改上方地址。没有配置平台也可先切数据库，相关网页资料会显示缺项。", wraplength=760).grid(row=2, column=0, columnspan=2, sticky="w", pady=12)
        for row, (key, label) in enumerate((("account", "平台账号"), ("platform_password", "更换平台密码"),
            ("app_id", "AppId"), ("site_out_id", "SiteOutId"), ("api_prefix", "API 前缀"),
            ("contract_path", "已核验表单协议"))):
            self.field(platform_advanced, row, key, label)
        ttk.Button(platform_advanced, text="选择本机表单协议", command=self.choose_contract).grid(row=6, column=1, sticky="w", pady=6)
        ttk.Label(platform_advanced, text="本页沿用原配置，通常无需修改。AppId / SiteOutId 是平台内部编号，API 前缀是部署路径，表单协议是已核验的本机适配文件；未知时保持为空，不使用猜测值。", wraplength=760).grid(row=7, column=0, columnspan=2, sticky="w", pady=12)
        self.progress = tk.Text(progress, height=20, wrap="word", state="disabled")
        self.progress.pack(fill="both", expand=True)
        ttk.Button(progress, text="重新全量刷新", command=self.refresh_all).pack(side="left", pady=8)
        ttk.Button(progress, text="重试未完成资料", command=lambda: self.refresh_all(retry=True)).pack(side="left", padx=8, pady=8)
        ttk.Label(frame, textvariable=self.message, wraplength=850).pack(anchor="w", pady=(0, 8))
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="保存当前连接", command=self.save_current).pack(side="left", padx=(0, 10))
        ttk.Button(toolbar, text="保存项目", command=self.save).pack(side="left")
        ttk.Button(toolbar, text="切换并全量刷新", command=self.switch).pack(side="left", padx=10)
        ttk.Button(toolbar, text="关闭", command=self.close).pack(side="right")

    def field(self, parent, row, key, label):
        ttk.Label(parent, text=label, width=18).grid(row=row, column=0, sticky="w", pady=5)
        if key in {"environment", "account_mode"}:
            values = ("development", "test", "staging", "production", "unknown") if key == "environment" else ("dedicated", "shared_restricted", "shared_unrestricted")
            widget = ttk.Combobox(parent, textvariable=self.fields[key], values=values, state="readonly")
        else:
            widget = ttk.Entry(parent, textvariable=self.fields[key], show="●" if key.endswith("password") else "")
        widget.grid(row=row, column=1, sticky="ew", pady=5)

    def choose_contract(self):
        value = filedialog.askopenfilename(parent=self.root, title="选择该平台已核验的表单协议", filetypes=[("JSON", "*.json")])
        if value:
            self.fields["contract_path"].set(value)

    def new_project(self):
        if self.busy:
            return
        self._view_epoch += 1
        previous_source = self.project_id or self.credential_source_id or self.legacy_source_id
        retained = {key: self.fields[key].get() for key in ("look_user", "write_user", "account_mode", "environment", "platform_url", "app_id", "site_out_id", "account", "api_prefix", "contract_path")}
        self.project_id = ""
        if not previous_source:
            self.form_template, self.platform_version, self.refresh_config = {}, {}, {}
            self.platform_credential_target, self.platform_credential_user = "", ""
        self.credential_source_id = previous_source
        for key, value in self.fields.items():
            value.set({"port": "3306", "environment": "development", "account_mode": "dedicated"}.get(key, ""))
        if previous_source:
            for key, value in retained.items():
                self.fields[key].set(value)
        self.update_credentials_summary()
        self.update_platform_summary()
        self.selector.set("新增项目")
        self.message.set("填写项目的数据库和平台地址，保存后即可一键切换。")

    def update_credentials_summary(self):
        look, write = self.fields["look_user"].get(), self.fields["write_user"].get()
        self.credentials_summary.set("Look 账号：" + (look or "尚未配置") + "    Write 账号：" + (write or "尚未配置")
                                     + ("\n原账号、密码及账户模式自动保留，密码不显示，无需重新填写。" if look or write else "\n首次配置请在高级账户设置中输入账号和密码。"))

    def update_platform_summary(self):
        account = self.fields["account"].get()
        configured = all(self.fields[key].get() for key in ("platform_url", "app_id", "site_out_id"))
        self.platform_summary.set(("平台账号：" + account + "，沿用原密码。\n" if account else "") +
                                  ("已带入原平台的应用与站点参数，不用重新填写。" if configured else
                                   "原配置缺少部分平台参数；数据库切换仍可使用，网页快照与相关表单功能暂显示缺项。"))

    def apply_platform_defaults(self, defaults):
        binding = defaults.get("platform_binding") or {}
        self.form_template = dict(binding.get("form_config") or {})
        self.platform_version = dict(defaults.get("platform_version") or {})
        self.platform_credential_target = str(binding.get("credential_target") or "")
        self.platform_credential_user = str(binding.get("account") or "")
        for name in ("platform_url", "app_id", "site_out_id", "account"):
            self.fields[name].set(str(binding.get(name) or ""))
        for name in ("api_prefix", "contract_path"):
            self.fields[name].set(str(self.form_template.get(name) or ""))
        self.update_platform_summary()

    def copy_project(self):
        if self.busy:
            return
        self._view_epoch += 1
        source = self.project_id or self.credential_source_id or self.legacy_source_id
        if not source:
            self.message.set("请先选择已保存的项目，或配置当前连接，作为复制来源。")
            return
        self.credential_source_id, self.project_id = source, ""
        self.selector.set("复制项目 · 修改地址和库名后保存")
        self.fields["name"].set((self.fields["name"].get() or "项目") + " 副本")
        self.fields["expected_grants_sha256"].set("")
        self.form_template.pop("credential_target", None)
        for name in ("look_password", "write_password", "platform_password"):
            self.fields[name].set("")
        self.update_credentials_summary()
        self.update_platform_summary()
        self.message.set("已保留账号和已保存的密码引用。修改地址、库名后保存；平台网址不同也需在“平台与表单”页修改。")

    def selected(self, event=None):
        index = self.selector.current()
        if self.busy or not 0 <= index < len(self.projects):
            return
        self._view_epoch += 1
        try:
            entry = self.projects[index]
            profile = load_profile(entry["project_id"], entry["revision"])
            self.project_id = profile["project_id"]
            self.credential_source_id = self.project_id
            values = {"name": profile["name"], "environment": profile["environment"], **profile["database"], **profile["platform_binding"],
                      "look_user": profile["accounts"]["look"]["user"], "write_user": profile["accounts"]["write"]["user"],
                      "account_mode": profile["accounts"]["write"]["account_mode"],
                      "expected_grants_sha256": profile["accounts"]["write"].get("expected_grants_sha256", "")}
            self.form_template = dict(profile["platform_binding"].get("form_config") or {})
            self.platform_credential_target = str(profile["platform_binding"].get("credential_target") or "")
            self.platform_credential_user = str(profile["platform_binding"].get("account") or "")
            self.platform_version = dict(profile.get("platform_version") or {})
            self.refresh_config = dict(profile.get("refresh") or {})
            for key in ("api_prefix", "contract_path"):
                values[key] = self.form_template.get(key, "")
            for key, variable in self.fields.items():
                variable.set(str(values.get(key, "")))
            self.update_credentials_summary()
            self.update_platform_summary()
        except Exception:
            self.message.set("项目配置无法加载，请检查本机配置版本。")

    def profile(self):
        value = {key: var.get().strip() for key, var in self.fields.items() if not key.endswith("password")}
        try:
            port = int(value["port"])
        except ValueError:
            raise ProjectError("PROJECT_FIELDS_INVALID", "端口必须为整数。")
        form = {**self.form_template, "api_prefix": value["api_prefix"]}
        if value["contract_path"]:
            form["contract_path"] = value["contract_path"]
        result = {"name": value["name"], "environment": value["environment"],
                "database": {"host": value["host"], "port": port, "database": value["database"]},
                "accounts": {"look": {"user": value["look_user"]}, "write": {"user": value["write_user"],
                    "account_mode": value["account_mode"], "expected_grants_sha256": value["expected_grants_sha256"]}},
                "platform_binding": {"platform_url": value["platform_url"], "app_id": value["app_id"],
                    "site_out_id": value["site_out_id"], "account": value["account"], "form_config": form},
                "platform_version": self.platform_version, "refresh": self.refresh_config}
        source = self.credential_source_id or self.legacy_source_id
        if self.platform_credential_target and value["account"] == self.platform_credential_user:
            result["platform_binding"]["credential_target"] = self.platform_credential_target
        if source and (database_scope(result["database"]) != source or not self.project_id):
            result["reuse_credentials_from"] = source
            result["accounts"]["write"]["expected_grants_sha256"] = ""
        return result

    def save(self):
        if self.busy:
            return
        try:
            profile, expected = self.profile(), self.revision
            passwords = {role: self.fields[key].get() for role, key in (("look", "look_password"), ("write", "write_password"), ("platform", "platform_password")) if self.fields[key].get()}
            self.run("save", lambda: settings.save_project(profile, expected, passwords=passwords))
        except ProjectError as error:
            self.message.set(str(error))

    def save_current(self):
        expected = self.revision
        self.run("save_current", lambda: settings.save_current_project(expected))

    def switch(self):
        if not self.project_id:
            self.message.set("请先保存项目配置。")
            return
        project_id, expected = self.project_id, self.revision
        self.run("switch", lambda: settings.switch_project(project_id, expected))

    def refresh_all(self, retry=False):
        from .refresh import refresh_project
        expected = self.revision
        self.run("refresh", lambda: refresh_project(expected, retry_failed=retry))

    def manage_sessions(self):
        from .session_launcher import open_session_manager
        self.run("sessions", open_session_manager)

    def run(self, action, function):
        background = action == "progress"
        if self.busy or background and self.background_busy:
            return
        if background:
            self.background_busy = True
        else:
            self._view_epoch += 1
            self.busy = True
            self.selector.configure(state="disabled")
            self.message.set({"load": "正在读取配置…", "save": "正在保存项目配置…", "save_current": "正在保留原配置并加入项目列表…", "switch": "正在测试连接并切换项目…", "refresh": "正在启动刷新…", "sessions": "正在打开未结束流程…"}.get(action, "正在处理…"))
        epoch = self._view_epoch
        def worker():
            try:
                result = function()
            except ProjectError as error:
                result = {"ok": False, "error": str(error), "error_code": error.code}
            except Exception:
                result = {"ok": False, "error": "操作未完成，请检查本机配置；密码不会显示在错误中。"}
            self.results.put((action, result, epoch))
        threading.Thread(target=worker, daemon=True, name="gxp-project-settings").start()

    def render_progress(self, status):
        labels = {"pending": "等待刷新", "fetching": "正在刷新", "ready": "完整可用", "partial": "部分可用", "unavailable": "资料不可用"}
        refresh = status.get("refresh") or {}
        lines = ["当前项目：" + status.get("active_project_name", "未选择"), "刷新状态：" + labels.get(refresh.get("status"), refresh.get("status", "尚未刷新"))]
        for name, item in refresh.get("resources", {}).items():
            lines.append("\n" + name + "：" + labels.get(item.get("status"), item.get("status", "未知")))
            if item.get("error_code"):
                lines.append("  " + str(item["error_code"]))
            failures = item.get("failures") or []
            lines.extend("  " + json.dumps(failure, ensure_ascii=False)[:300] for failure in failures[:30])
            for category, detail in (item.get("resources") or {}).items():
                if not isinstance(detail, dict) or detail.get("status") == "ready":
                    continue
                lines.append("  " + category + "：" + str(detail.get("status", "incomplete"))
                             + (" · " + str(detail["error_code"]) if detail.get("error_code") else ""))
                if detail.get("received") is not None or detail.get("expected") is not None:
                    lines.append("    已获取 " + str(detail.get("received", "未知")) + " / 应获取 " + str(detail.get("expected", "未知")))
        text = "\n".join(lines)
        if text != self._progress_text:
            position = self.progress.yview()[0]
            self.progress.configure(state="normal")
            self.progress.delete("1.0", "end")
            self.progress.insert("1.0", text)
            self.progress.yview_moveto(position)
            self.progress.configure(state="disabled")
            self._progress_text = text
        sessions = status.get("active_edit_sessions") or []
        connection = status.get("settings") or {}
        destination = str(connection.get("host") or "") + ":" + str(connection.get("port") or "") + "/" + str(connection.get("database") or "")
        current = "当前项目：" + status.get("active_project_name", "未选择") + (" · " + destination if connection.get("host") else "") + (f" · 有 {len(sessions)} 项编辑或待核实流程，结束后可切换" if sessions else "")
        if current != self.current.get():
            self.current.set(current)

    def poll(self):
        if self.closed:
            return
        try:
            action, result, epoch = self.results.get_nowait()
        except queue.Empty:
            self.root.after(100, self.poll)
            return
        if not isinstance(result, dict):
            result = {"ok": False, "error": "无法读取操作结果，请重试。"}
        if action == "progress":
            self.background_busy = False
            if epoch == self._view_epoch and not self.busy and result.get("ok", True):
                self._status_snapshot = result
                self.render_progress(result)
            # A background status read never dismisses an operation error,
            # changes the form revision, or unlocks a foreground operation.
            self.root.after(100, self.poll)
            return
        self.busy = False
        self.selector.configure(state="readonly")
        if not result.get("ok", True):
            self.message.set(str(result.get("error") or "操作未完成。")[:700])
        elif action in {"load", "progress"}:
            self._status_snapshot = result
            self.revision = result["revision"]
            self.render_progress(result)
            if action == "load":
                self.projects = result.get("projects", [])
                self.selector.configure(values=[p["name"] + " · " + p["database"]["host"] + ":" + str(p["database"]["port"]) + "/" + p["database"]["database"] for p in self.projects])
                selected = next((i for i, p in enumerate(self.projects) if p["project_id"] == self.project_id), next((i for i, p in enumerate(self.projects) if p.get("active")), -1))
                if selected >= 0:
                    self.selector.current(selected)
                    self.selected()
                else:
                    self.new_project()
                    self.legacy_source_id = result.get("active_project_id") or ""
                    legacy = result.get("settings") or {}
                    for key in ("host", "port", "database", "look_user", "write_user", "account_mode", "expected_grants_sha256"):
                        if legacy.get(key):
                            self.fields[key].set(str(legacy[key]))
                    self.credentials_summary.set("")
                    self.update_credentials_summary()
                    self.apply_platform_defaults(result.get("platform_defaults") or {})
            self.message.set("项目资料按数据库隔离。选择已保存项目可一键切换。")
        else:
            if action in {"save", "save_current"}:
                self.project_id = result["project_id"]
                for key in ("look_password", "write_password", "platform_password"):
                    self.fields[key].set("")
            self.message.set("项目已保存，尚未激活。" if action == "save" else "已切换或启动刷新；请查看刷新进度和缺项。")
            self.run("load", settings.settings_status)
        self.root.after(100, self.poll)

    def refresh_progress(self):
        if self.closed:
            return
        status = self._status_snapshot or {}
        running = (status.get("refresh") or {}).get("status") in {"pending", "fetching"}
        has_flows = bool(status.get("active_edit_sessions"))
        if not self.busy and not self.background_busy and (running or has_flows):
            self.run("progress", settings.settings_status)
        self.root.after(2000, self.refresh_progress)

    def close(self):
        if self.busy:
            return
        self.closed = True
        for key in ("look_password", "write_password", "platform_password"):
            self.fields[key].set("")
        self.root.destroy()
