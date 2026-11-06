"""Standalone Qt connection/review windows for the form configuration domain."""
from __future__ import annotations

import getpass
import json
from datetime import datetime, timedelta, timezone
import subprocess
import sys
from pathlib import Path
from typing import Any

from .common import FormError
from .platform import load_settings, login, save_settings, set_credential, settings_path


def _scripts() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts"


def _launch(script: str, arguments: list[str]) -> dict[str, Any]:
    python = Path(sys.executable)
    pythonw = python.with_name("pythonw.exe")
    executable = pythonw if sys.platform == "win32" and pythonw.is_file() else python
    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    process = subprocess.Popen([str(executable), str(_scripts() / script), *arguments], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=creationflags)
    return {"opened": True, "pid": process.pid, "domain": "form_config"}


def open_connection_window() -> dict[str, Any]:
    return _launch("form_connection_app.py", [])


def open_review_window(write_id: str, conversation_id: str) -> dict[str, Any]:
    return _launch("form_approval_app.py", ["--write-id", write_id, "--conversation-id", conversation_id])


def _qt():
    from PySide6 import QtCore, QtWidgets
    return QtCore, QtWidgets


def _review_panels(value: dict[str, Any]) -> dict[str, str]:
    """Review the actual differences and requests without duplicating a page."""
    artifact = value.get("artifact") or {}
    binding = artifact.get("binding") or {}
    actor = (value.get("session") or {}).get("snapshot", {}).get("actor", binding.get("actor"))
    target = value.get("target") or binding.get("target") or {}
    try:
        expires = datetime.fromtimestamp(float(artifact["expires_at"]), tz=timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S +08:00")
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        expires = "未提供"
    change_lines = ["目标页面", json.dumps(target, ensure_ascii=False, indent=2), "平台操作人", json.dumps(actor, ensure_ascii=False, indent=2), "审批到期时间：" + expires]
    for index, change in enumerate(value.get("changes", []), 1):
        change_lines.extend(["", f"{index}. 组件 {change.get('component_id')} ({change.get('component_type')})", "路径：" + str(change.get("path")), "修改类型：" + ("联动派生" if change.get("derived") else "显式修改"), "旧值 → 新值", "旧值（" + ("存在" if change.get("old_exists", True) else "原属性不存在") + "）：", json.dumps(change.get("old_value"), ensure_ascii=False, indent=2), "新值：", json.dumps(change.get("value"), ensure_ascii=False, indent=2)])
    requests = {"审批到期时间": expires, "完整实际 HTTP 请求": value.get("requests", [])}
    approval = artifact.get("approval") or {}
    metadata = {"domain": artifact.get("domain"), "binding_hash": artifact.get("binding_hash"), "created_at": artifact.get("created_at"), "expires_at": artifact.get("expires_at"), "binding": {key: child for key, child in binding.items() if key != "requests"}, "人工批准": {"reviewer": approval.get("reviewer"), "approved_at": approval.get("approved_at"), "signature_present": bool(approval.get("signature"))}, "状态": (value.get("write") or {}).get("state"), "approval_valid": value.get("approval_valid", False)}
    return {"changes": "\n".join(change_lines), "requests": json.dumps(requests, ensure_ascii=False, indent=2), "approval": json.dumps(metadata, ensure_ascii=False, indent=2)}


def run_connection_window() -> int:
    core, widgets = _qt()
    app = widgets.QApplication.instance() or widgets.QApplication(sys.argv[:1])
    active = {"busy": False, "thread": None, "result": None}

    class ConnectionDialog(widgets.QDialog):
        def reject(self):
            if not active["busy"]:
                super().reject()

        def closeEvent(self, event):
            if active["busy"]:
                event.ignore()
            else:
                event.accept()

    window = ConnectionDialog()
    window.setWindowTitle("GXP 表单配置编辑器 · 独立连接设置")
    window.resize(660, 480)
    layout = widgets.QVBoxLayout(window)
    notice = widgets.QLabel("平台凭据仅存本机凭据库。原生协议证据未核实完整时，编辑器保持禁止写入。")
    notice.setWordWrap(True)
    layout.addWidget(notice)
    current = load_settings()
    form = widgets.QFormLayout()
    fields: dict[str, Any] = {}
    for key, label, default in (
        ("environment", "环境标识", ""),
        ("base_url", "平台 Origin", ""),
        ("api_prefix", "GXP API 前缀", "/gxp2"),
        ("platform", "表单平台（web/wap）", "web"),
        ("contract_path", "本机原生协议 JSON 路径", ""),
        ("credential_target", "本机凭据条目标识", "Codex.GxpFormEditor.Platform"),
        ("auth_header", "凭据请求头（Cookie/Authorization）", "Cookie"),
    ):
        field = widgets.QLineEdit(str(current.get(key, default)))
        fields[key] = field
        form.addRow(label, field)
    account = widgets.QLineEdit()
    password = widgets.QLineEdit()
    password.setEchoMode(widgets.QLineEdit.EchoMode.Password)
    company = widgets.QLineEdit()
    organization = widgets.QLineEdit()
    form.addRow("平台账号", account)
    form.addRow("平台密码", password)
    form.addRow("companyId（可选）", company)
    form.addRow("orgIdentityId（可选）", organization)
    advanced = widgets.QCheckBox("高级：手工填写平台登录凭据")
    form.addRow(advanced)
    secret = widgets.QLineEdit()
    secret.setEchoMode(widgets.QLineEdit.EchoMode.Password)
    secret.setPlaceholderText("完整 Cookie 或 Authorization 值；仅存本机凭据库")
    secret.setVisible(False)
    form.addRow(secret)
    advanced.toggled.connect(secret.setVisible)
    layout.addLayout(form)
    location = widgets.QLabel(str(settings_path()))
    location.setWordWrap(True)
    layout.addWidget(location)
    status = widgets.QLabel("")
    status.setWordWrap(True)
    layout.addWidget(status)
    buttons = widgets.QDialogButtonBox(widgets.QDialogButtonBox.StandardButton.Save | widgets.QDialogButtonBox.StandardButton.Cancel)
    buttons.button(widgets.QDialogButtonBox.StandardButton.Save).setText("登录并保存")
    layout.addWidget(buttons)

    class LoginWorker(core.QThread):
        completed = core.Signal(object)

        def __init__(self, value, account_text, password_text, company_text, organization_text):
            super().__init__(window)
            self.value = value
            self.account_text = account_text
            self.password_text = password_text
            self.company_text = company_text
            self.organization_text = organization_text

        def run(self):
            try:
                login(self.value, self.account_text, self.password_text, self.company_text, self.organization_text)
                save_settings(self.value)
                self.completed.emit({"ok": True})
            except Exception as exc:
                self.completed.emit({"ok": False, "message": str(exc) if isinstance(exc, FormError) else "登录设置未保存，请检查本机凭据库和连接。"})
            finally:
                self.account_text = ""
                self.password_text = ""

    def set_busy(value):
        active["busy"] = value
        buttons.setEnabled(not value)
        for field in [*fields.values(), account, password, company, organization, advanced, secret]:
            field.setEnabled(not value)

    def login_complete(value):
        active["result"] = value

    def login_finished():
        set_busy(False)
        result = active["result"] or {}
        if result.get("ok"):
            window.accept()
        else:
            status.setText(result.get("message") or "登录未完成。")

    def save() -> None:
        try:
            value = {**current, **{key: field.text().strip() for key, field in fields.items()}}
            if value["platform"] not in {"web", "wap"} or value["auth_header"] not in {"Cookie", "Authorization"}:
                raise FormError("form_settings_invalid", "平台填写 web 或 wap；凭据请求头填写 Cookie 或 Authorization。")
            if not advanced.isChecked() and (account.text() or password.text()):
                value["auth_header"] = "Cookie"
                worker = LoginWorker(value, account.text(), password.text(), company.text(), organization.text())
                active["thread"] = worker
                active["result"] = None
                worker.completed.connect(login_complete)
                worker.finished.connect(login_finished)
                set_busy(True)
                status.setText("正在登录平台；账号密码不会写入配置文件。")
                password.clear()
                worker.start()
                return
            if advanced.isChecked() and secret.text():
                set_credential(value, secret.text())
            save_settings(value)
            secret.clear()
            window.accept()
        except Exception as exc:
            status.setText(str(exc) if isinstance(exc, FormError) else "设置未保存，请检查本机凭据库和文件权限。")

    buttons.accepted.connect(save)
    buttons.rejected.connect(lambda: window.reject() if not active["busy"] else None)

    window.show()
    return app.exec()


def run_review_window(write_id: str, conversation_id: str, service: Any = None) -> int:
    core, widgets = _qt()
    if service is None:
        from .service import FormService
        service = FormService(conversation_id=conversation_id)
    app = widgets.QApplication.instance() or widgets.QApplication(sys.argv[:1])
    active = {"busy": False, "approved": False, "thread": None, "session_id": None, "state": None}

    class ReviewDialog(widgets.QDialog):
        def reject(self):
            if not active["busy"]:
                super().reject()

        def closeEvent(self, event):
            if active["busy"]:
                event.ignore()
            else:
                event.accept()

    window = ReviewDialog()
    window.setWindowTitle("GXP 表单配置编辑器 · 人工审批")
    window.resize(1120, 800)
    layout = widgets.QVBoxLayout(window)
    title = widgets.QLabel("核对目标页面、版本、配置差异及全部保存请求，再点击批准。执行需要另行确认。")
    title.setWordWrap(True)
    layout.addWidget(title)
    tabs = widgets.QTabWidget()
    views: dict[str, Any] = {}
    for key, tab_title in (("changes", "配置变化"), ("requests", "实际 HTTP 请求"), ("approval", "审批信息")):
        view = widgets.QPlainTextEdit()
        view.setReadOnly(True)
        views[key] = view
        tabs.addTab(view, tab_title)
    review = views["changes"]
    layout.addWidget(tabs, 1)
    reviewer = widgets.QLineEdit(getpass.getuser())
    reviewer.setPlaceholderText("本机人工审批者")
    layout.addWidget(reviewer)
    read = widgets.QCheckBox("我已核对目标页面、版本、差异和全部请求")
    layout.addWidget(read)
    status = widgets.QLabel("")
    status.setWordWrap(True)
    layout.addWidget(status)
    row = widgets.QHBoxLayout()
    approve_button = widgets.QPushButton("人工批准")
    execute_button = widgets.QPushButton("执行已批准保存…")
    refresh_button = widgets.QPushButton("刷新核对")
    close_button = widgets.QPushButton("关闭")
    cancel_button = widgets.QPushButton("取消本次编辑")
    execute_button.setEnabled(False)
    for button in (approve_button, execute_button, refresh_button, cancel_button, close_button):
        row.addWidget(button)
    layout.addLayout(row)

    class Worker(core.QThread):
        completed = core.Signal(object)
        failed = core.Signal(object)

        def run(self):
            try:
                self.completed.emit(service.execute_form_write(write_id, conversation_id))
            except Exception as exc:
                self.failed.emit({"code": getattr(exc, "code", "execution_error"), "message": str(exc) if isinstance(exc, FormError) else "执行未取得确定结果，请刷新并使用恢复核对。"})

    def controls() -> None:
        busy = active["busy"]
        approve_button.setEnabled(not busy and active["state"] == "pending" and not active["approved"] and bool(active["session_id"]) and read.isChecked() and bool(reviewer.text().strip()))
        execute_button.setEnabled(not busy and active["approved"])
        refresh_button.setEnabled(not busy)
        reviewer.setEnabled(not busy and not active["approved"])
        read.setEnabled(not busy and not active["approved"])
        close_button.setEnabled(not busy)
        cancel_button.setEnabled(not busy and active["state"] in {"pending", "approved"} and bool(active["session_id"]))

    def refresh() -> None:
        try:
            value = service.get_form_review(write_id, conversation_id)
            active["session_id"] = value.get("session", {}).get("id")
            active["state"] = value.get("write", {}).get("state")
            for key, text in _review_panels(value).items():
                views[key].setPlainText(text)
            # A persisted signature may be resumed after the window closes;
            # only service-verified approval enables execution on reopening.
            active["approved"] = active["state"] == "approved" and value.get("approval_valid") is True
            status.setText("已读取当前审批材料。")
        except Exception as exc:
            review.clear()
            views["requests"].clear()
            views["approval"].clear()
            status.setText(str(exc) if isinstance(exc, FormError) else "审批材料读取失败。")
            active["approved"] = False
            active["session_id"] = None
            active["state"] = None
        controls()

    def approve_click() -> None:
        try:
            service.approve_form_write(write_id, conversation_id, reviewer.text().strip())
            active["approved"] = True
            active["state"] = "approved"
            status.setText("人工审批已签名；尚未执行保存。")
        except Exception as exc:
            active["approved"] = False
            status.setText(str(exc) if isinstance(exc, FormError) else "审批未完成。")
        controls()

    def complete(value: Any) -> None:
        active["busy"] = False
        active["approved"] = False
        active["state"] = value.get("state") if isinstance(value, dict) else None
        review.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
        status.setText("执行已结束，请核对结果；不自动重发请求。")
        controls()

    def execute_click() -> None:
        if active["busy"] or not active["approved"]:
            return
        answer = widgets.QMessageBox.question(window, "确认执行保存", "现在执行审批材料中的全部表单草稿保存请求？", widgets.QMessageBox.StandardButton.Yes | widgets.QMessageBox.StandardButton.No, widgets.QMessageBox.StandardButton.No)
        if answer != widgets.QMessageBox.StandardButton.Yes:
            return
        active["busy"] = True
        controls()
        status.setText("正在执行及回读核对；结果不明时需要恢复核对，请勿另开窗口重试。")
        thread = Worker(window)
        active["thread"] = thread
        thread.completed.connect(complete)
        thread.failed.connect(complete)
        thread.start()

    def changed() -> None:
        active["approved"] = False
        controls()

    def cancel_click() -> None:
        if active["busy"] or not active["session_id"]:
            return
        try:
            value = service.cancel_form_edit_session(active["session_id"])
            active["approved"] = False
            active["session_id"] = None
            review.setPlainText(json.dumps(value, ensure_ascii=False, indent=2))
            status.setText("本次编辑已取消，未发出保存请求。")
        except Exception as exc:
            status.setText(str(exc) if isinstance(exc, FormError) else "取消未完成，请刷新核对状态。")
        controls()

    read.toggled.connect(changed)
    reviewer.textChanged.connect(changed)
    approve_button.clicked.connect(approve_click)
    execute_button.clicked.connect(execute_click)
    refresh_button.clicked.connect(refresh)
    cancel_button.clicked.connect(cancel_click)
    close_button.clicked.connect(window.reject)

    refresh()
    window.show()
    return app.exec()
