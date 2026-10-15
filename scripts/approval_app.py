"""Local Qt review application; SQL approval and execution remain human actions."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

from PySide6.QtCore import QObject, QSignalBlocker, QThread, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QFrame, QHBoxLayout, QListWidget,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)
from gxp_editor.approval import approval_is_current, approve_statement, load_artifact, save_artifact
from gxp_editor.approval_display import (
    STATEMENT_LABELS, canvas_change_summary, execution_result_message, parameter_details,
    reference_repair_summary, render_full_sql, statement_scope,
)
from gxp_editor.compiler import CompilerBridge
from gxp_editor.component_preview import component_changes
from gxp_editor.config import load_config
from gxp_editor.execution_lock import ApprovalWindowBusyError, approval_window_lock
from gxp_editor.hashing import sha256_text
from gxp_editor.qt_review_ui import CodeReview, ComponentReview
from gxp_editor.qt_theme import application, label, text_view
from gxp_editor.single_row_checker import SingleRowCheckError
from gxp_editor.state import StateError, StateStore
from gxp_editor.transaction import TransactionPlanError, validate_transaction_artifact
from gxp_editor.writer import execute_approved_transaction


class MessageBoxes:
    @staticmethod
    def showinfo(title, text, parent=None):
        application()
        QMessageBox.information(parent, title, text)

    @staticmethod
    def showerror(title, text, parent=None):
        application()
        QMessageBox.critical(parent, title, text)

    @staticmethod
    def askyesno(title, text, parent=None):
        application()
        return QMessageBox.question(
            parent, title, text, QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) == QMessageBox.StandardButton.Yes


messagebox = MessageBoxes()


class ExecutionThread(QThread):
    completed = Signal(object)
    failed = Signal(str)

    def __init__(self, transaction_id, parent=None):
        super().__init__(parent)
        self.transaction_id = transaction_id

    def run(self):
        try:
            self.completed.emit(execute_approved_transaction(self.transaction_id))
        except Exception as exc:
            self.failed.emit(str(exc))


class ApprovalShell(QMainWindow):
    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.setMinimumSize(640, 420)

    def fit_to_screen(self):
        # availableGeometry already accounts for Windows scaling and the taskbar.
        available = self.screen().availableGeometry()
        width = max(320, available.width() - 48)
        height = max(240, available.height() - 64)
        self.setMinimumSize(min(640, width), min(420, height))
        self.resize(min(1240, width), min(880, height))
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def closeEvent(self, event):
        if self.controller._executing:
            self.controller.progress.setText("事务正在执行，请等待结果。")
            event.ignore()
            return
        self.controller.timer.stop()
        self.controller._release_window_lock()
        event.accept()


def card():
    frame = QFrame()
    frame.setObjectName("card")
    return frame


class ApprovalWindow(QObject):
    def __init__(self, transaction_id: str, conversation_id: str | None = None, owner_revision: int | None = None):
        super().__init__()
        self.transaction_id = transaction_id
        self.conversation_id = conversation_id
        self.owner_revision = owner_revision
        self.config = load_config()
        self.state = StateStore()
        self.transaction = self.state.get_transaction(transaction_id)
        self.path = Path(self.transaction["artifact_path"])
        self._executing = False
        self._window_lock = approval_window_lock(self.path)
        self._window_lock.__enter__()
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        self.transaction = self.state.get_transaction(self.transaction_id)
        if self.transaction["state"] != "awaiting_approval":
            raise StateError("该审批已执行或已关闭，请从当前对话核对结果或重新生成计划。")
        self.artifact = load_artifact(self.path)
        validate_transaction_artifact(self.artifact)
        self.session = self.state.get_session(self.artifact["session_id"])
        self._check_owner(self.session)
        self.index = 0
        self._review_blocked = False
        self.qt_app = application()
        self.root = ApprovalShell(self)
        self.timer = QTimer(self.root)
        self.timer.timeout.connect(self.refresh_approval_state)
        self.root.setWindowTitle(f"GXP 草稿保存审批 · {self.session_marker()}")
        registry, registry_note = self.load_preview_registry()
        self._build_ui(registry, registry_note)
        self.render()
        self.root.fit_to_screen()
        self.timer.start(1000)

    def _check_owner(self, session):
        if session.get("transaction_id") != self.transaction_id or session["state"] not in {"awaiting_approval", "restore_pending"}:
            raise StateError("该会话已结束或已有其他审批，原审批窗口不能继续操作。")
        if (self.conversation_id is not None and (session.get("conversation_id") or "") != self.conversation_id
                or self.owner_revision is not None and session.get("owner_revision", 0) != self.owner_revision):
            raise StateError("该审批流程已转交给其他对话。请使用当前归属对话打开。")

    def _build_ui(self, registry, registry_note):
        body = QWidget()
        self.root.setCentralWidget(body)
        shell = QVBoxLayout(body)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        page = QVBoxLayout(content)
        page.setContentsMargins(0, 0, 0, 0)
        page.setSpacing(0)
        heading = QVBoxLayout()
        heading.setContentsMargins(24, 20, 24, 12)
        heading.addWidget(label("GXP  /  DRAFT REVIEW", "eyebrow"))
        row = QHBoxLayout()
        row.addWidget(label("草稿审批", "title"), 1)
        self.badge = label("等待审批", "badge")
        row.addWidget(self.badge)
        heading.addLayout(row)
        self.header = label(f"{self.session['action_name']}（{self.artifact['action_code']}）", "subtitle", wrap=True)
        heading.addWidget(self.header)
        page.addLayout(heading)
        metrics = QHBoxLayout()
        metrics.setSpacing(12)
        metrics.setContentsMargins(24, 0, 24, 14)
        self.approved_metric = label("", "metricValue")
        self.changed_metric = label("", "metricValue")
        for value, title in (
            (self.approved_metric, "SQL 审批进度"), (self.changed_metric, "画布变更"),
            (label("仅保存草稿", "metricValue"), "手动发布后生效"),
        ):
            frame = card()
            layout = QVBoxLayout(frame)
            layout.setContentsMargins(16, 10, 16, 10)
            layout.addWidget(value)
            layout.addWidget(label(title, "muted"))
            metrics.addWidget(frame, 1)
        page.addLayout(metrics)
        workspace = QSplitter(Qt.Orientation.Horizontal)
        sidebar = card()
        side_layout = QVBoxLayout(sidebar)
        side_layout.setContentsMargins(10, 14, 10, 10)
        side_layout.addWidget(label("待审批语句", "eyebrow"))
        self.statement_list = QListWidget()
        self.statement_list.setObjectName("statements")
        self.statement_list.setMinimumWidth(160)
        self.statement_list.currentRowChanged.connect(self.select_statement)
        side_layout.addWidget(self.statement_list, 1)
        side_layout.addWidget(label("每条语句单独批准\n全部批准后可保存", "muted", wrap=True))
        workspace.addWidget(sidebar)
        review = QWidget()
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(10)
        self.notebook = QTabWidget()
        self.notebook.setDocumentMode(True)
        self.component_review = ComponentReview(registry, registry_note)
        self.code_review = CodeReview()
        self.notebook.addTab(self.component_review, "元件预览")
        self.notebook.addTab(self.code_review, "代码对比")
        sql_page = QWidget()
        sql_layout = QVBoxLayout(sql_page)
        sql_layout.setContentsMargins(14, 14, 14, 14)
        toolbar = QHBoxLayout()
        self.wrap_sql = QCheckBox("自动换行")
        self.wrap_sql.toggled.connect(self.toggle_wrap)
        toolbar.addWidget(self.wrap_sql)
        toolbar.addStretch()
        for title, handler in (("复制完整 SQL", self.copy_sql), ("导出 SQL…", self.export_sql)):
            button = QPushButton(title)
            button.clicked.connect(handler)
            toolbar.addWidget(button)
        sql_layout.addLayout(toolbar)
        self.text = text_view(code=True)
        sql_layout.addWidget(self.text, 1)
        self.notebook.addTab(sql_page, "SQL 语句")
        self.details_tabs = QTabWidget()
        self.changes_text = text_view()
        self.raw_changes_text = text_view(code=True)
        self.params_text = text_view(code=True)
        self.validation_text = text_view()
        for title, view in (
            ("本次改动", self.changes_text), ("原始节点", self.raw_changes_text),
            ("模板与参数", self.params_text), ("校验与指纹", self.validation_text),
        ):
            self.details_tabs.addTab(view, title)
        repairs = (self.artifact.get("compiler_evidence") or {}).get("reference_repairs") or []
        if repairs:
            self.details_tabs.addTab(text_view(reference_repair_summary(repairs)), "引用修复来源")
        self.notebook.addTab(self.details_tabs, "审批详情")
        review_layout.addWidget(self.notebook, 1)
        scope = card()
        scope_layout = QVBoxLayout(scope)
        scope_layout.setContentsMargins(14, 10, 14, 10)
        scope_layout.addWidget(label("当前语句范围", "eyebrow"))
        self.summary = text_view()
        self.summary.setMaximumHeight(96)
        self.summary.setMinimumHeight(64)
        scope_layout.addWidget(self.summary)
        review_layout.addWidget(scope)
        workspace.addWidget(review)
        workspace.setStretchFactor(0, 0)
        workspace.setStretchFactor(1, 1)
        workspace.setSizes([215, 970])
        area = QHBoxLayout()
        area.setContentsMargins(24, 0, 24, 16)
        area.addWidget(workspace)
        page.addLayout(area, 1)
        self.content_scroll.setWidget(content)
        shell.addWidget(self.content_scroll, 1)
        footer = QFrame()
        footer.setObjectName("footer")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(16, 10, 16, 12)
        status = QHBoxLayout()
        self.progress = label("", "muted", wrap=True)
        self.progress.setMaximumHeight(38)
        status.addWidget(self.progress, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedWidth(110)
        status.addWidget(self.progress_bar)
        footer_layout.addLayout(status)
        actions = QHBoxLayout()
        self.close_button = QPushButton("稍后处理")
        self.close_button.setToolTip("关闭窗口，保留待审批内容，不执行 SQL。")
        self.close_button.clicked.connect(self.close)
        self.approve_button = QPushButton("批准当前语句")
        self.approve_button.setObjectName("approve")
        self.approve_button.clicked.connect(self.approve_current)
        self.execute_button = QPushButton("执行并保存草稿")
        self.execute_button.setObjectName("primary")
        self.execute_button.clicked.connect(self.execute)
        actions.addWidget(self.close_button)
        actions.addStretch()
        for button in (self.approve_button, self.execute_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        footer_layout.addLayout(actions)
        shell.addWidget(footer)

    def _release_window_lock(self):
        lock = self._window_lock
        self._window_lock = None
        if lock is not None:
            lock.__exit__(None, None, None)

    def close(self):
        root = getattr(self, "root", None)
        if root is not None:
            root.close()
        if not self._executing:
            self._release_window_lock()

    def select_statement(self, index):
        if 0 <= index < len(self.artifact["statements"]):
            self.index = index
            self.render()

    def navigate(self, direction):
        self.select_statement(max(0, min(len(self.artifact["statements"]) - 1, self.index + direction)))

    def toggle_wrap(self):
        self.text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth if self.wrap_sql.isChecked() else QPlainTextEdit.LineWrapMode.NoWrap)

    def copy_sql(self):
        QApplication.clipboard().setText(render_full_sql(self.current()))
        self.progress.setText("已复制当前语句的完整 SQL")

    def export_sql(self):
        path, _ = QFileDialog.getSaveFileName(self.root, "导出当前语句的完整 SQL", f"gxp-{self.transaction_id[:8]}-{self.index + 1}.sql", "SQL 文件 (*.sql);;所有文件 (*)")
        if path:
            try:
                Path(path).write_text(render_full_sql(self.current()) + "\n", encoding="utf-8")
                self.progress.setText("完整 SQL 已导出")
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc), parent=self.root)

    def render(self):
        statements = self.artifact["statements"]
        statement = self.current()
        states = tuple(approval_is_current(self.artifact, item) for item in statements)
        self._last_approval_state = states
        count = sum(states)
        self.badge.setText("正在保存…" if self._executing else "全部已批准" if all(states) else "等待人工审批")
        self.approved_metric.setText(f"{count} / {len(statements)}")
        self.progress_bar.setRange(0, len(statements))
        self.progress_bar.setValue(count)
        self.progress.setText(f"当前第 {self.index + 1} 条 · 已批准 {count}/{len(statements)} 条")
        with QSignalBlocker(self.statement_list):
            self.statement_list.clear()
            for index, item in enumerate(statements):
                self.statement_list.addItem(f"{'✓' if states[index] else '○'}  语句 {index + 1}\n{STATEMENT_LABELS.get(item['kind'], item['kind'])}")
            self.statement_list.setCurrentRow(self.index)
        self.summary.setPlainText(statement_scope(statement))
        self.text.setPlainText(render_full_sql(statement))
        self.params_text.setPlainText(parameter_details(statement))
        self.validation_text.setPlainText(json.dumps({
            "事务 ID": self.transaction_id, "会话": self.session_marker(),
            "语句 SHA-256": statement["statement_hash"], "SQL SHA-256": statement["sql_sha256"],
            "Design ID": statement["design_id"], "预计影响行数": statement["expected_rows"],
            "修改前哈希": statement["old_hash"], "修改后哈希": statement["new_hash"],
            "运行引用校验（提示）": self.artifact["runtime_validation"],
            "数据库账户安全": self.artifact["database_safety"],
            "编译与引用校验": (self.artifact.get("compiler_evidence") or {}).get("validation"),
            "说明": "完整 SQL 为审阅副本，执行使用已签名模板及绑定参数。此次仅保存草稿，需手动发布。本程序不调用后端编辑/发布接口、不修改 Redis 或 DLL/PDB。",
        }, ensure_ascii=False, indent=2))
        sources = self.session["baseline"]["data"], self.artifact["desired_design"]["data"]
        if getattr(self, "_canvas_sources", None) != sources:
            before, after = (json.loads(source) for source in sources)
            changes = component_changes(before, after)
            self.component_review.render(before, after, changes=changes)
            self.code_review.set_canvas_context(before, after, changes=changes)
            self.changes_text.setPlainText(canvas_change_summary(before, after, changes=changes))
            self.raw_changes_text.setPlainText(self.change_details(changes))
            self.changed_metric.setText(f"{len(changes)} 项")
            self._canvas_sources = sources
        self.code_review.update_sources(self.session["baseline"], self.artifact["desired_design"])
        ready = self.transaction["state"] == "awaiting_approval" and not self.artifact.get("execution_started") and not self._executing and not self._review_blocked
        self.approve_button.setEnabled(ready and not states[self.index])
        self.execute_button.setEnabled(ready and all(states))
        self.close_button.setEnabled(not self._executing)

    def refresh_approval_state(self):
        if self._executing:
            return
        try:
            self.transaction = self.state.get_transaction(self.transaction_id)
            self._check_owner(self.state.get_session(self.artifact["session_id"]))
            current = tuple(approval_is_current(self.artifact, item) for item in self.artifact["statements"])
            if current != self._last_approval_state or self.transaction["state"] != "awaiting_approval":
                self.render()
        except Exception as exc:
            self._block_review(str(exc))

    def _block_review(self, error):
        self._review_blocked = True
        self.approve_button.setEnabled(False)
        self.execute_button.setEnabled(False)
        self.badge.setText("审批已停止")
        self.progress.setText(error)
        self.timer.stop()

    def _validate_current(self):
        self.transaction = self.state.get_transaction(self.transaction_id)
        self._check_owner(self.state.get_session(self.artifact["session_id"]))
        validate_transaction_artifact(self.artifact)
        on_disk = load_artifact(self.path)
        validate_transaction_artifact(on_disk)
        if on_disk["transaction_hash"] != self.artifact["transaction_hash"]:
            raise TransactionPlanError("审批内容已改变，请重新生成并检查。")
        if self.transaction["state"] != "awaiting_approval" or on_disk.get("execution_started") or self.artifact.get("execution_started"):
            raise StateError("该事务已经尝试执行或已关闭，必须重新生成事务并逐条审批。")

    def approve_current(self):
        if self._executing or self._review_blocked:
            return
        try:
            self._validate_current()
            if not messagebox.askyesno("确认审批", f"批准当前第 {self.index + 1} 条 SQL？\n\n{STATEMENT_LABELS.get(self.current()['kind'], self.current()['kind'])}\n\n批准后不会立即执行。", parent=self.root):
                return
            self._validate_current()
            approve_statement(self.config, self.artifact, self.current()["id"])
            save_artifact(self.path, self.artifact)
            if self.index + 1 < len(self.artifact["statements"]):
                self.index += 1
            self.render()
        except Exception as exc:
            self._block_review(str(exc))
            messagebox.showerror("已阻止审批", str(exc), parent=self.root)

    def execute(self):
        if self._executing or self._review_blocked:
            return
        try:
            self._validate_current()
            if not all(approval_is_current(self.artifact, item) for item in self.artifact["statements"]):
                raise StateError("请先逐条批准全部 SQL，过期语句需重新批准。")
            if self.artifact["database_safety"].get("shared_account") and not messagebox.askyesno("共享账户风险确认", "当前使用共享数据库账户。固定 SQL、设计锁和审批只能限制本插件内的操作，不能缩小该账户在其他客户端中的权限。确认继续吗？", parent=self.root):
                return
            if not messagebox.askyesno("执行并保存草稿", "所有 SQL 已分别批准。现在将重新锁定并校验草稿，执行短事务并保存。\n\n保存后仍需在平台手动发布。确认执行？", parent=self.root):
                return
            self._validate_current()
            self._executing = True
            self.timer.stop()
            self.render()
            self.progress.setText("正在校验并保存，请等待执行结果…")
            self.worker = ExecutionThread(self.transaction_id, self.root)
            self.worker.completed.connect(self._execution_success)
            self.worker.failed.connect(self._execution_failure)
            self.worker.start()
        except Exception as exc:
            self._executing = False
            self.timer.start(1000)
            messagebox.showerror("暂时无法执行", str(exc), parent=self.root)
            self.render()

    @Slot(object)
    def _execution_success(self, result):
        self.worker.wait()
        self._executing = False
        messagebox.showinfo("执行结果", execution_result_message(result), parent=self.root)
        self.close()

    @Slot(str)
    def _execution_failure(self, error):
        self.worker.wait()
        self._executing = False
        try:
            self.transaction = self.state.get_transaction(self.transaction_id)
            if self.transaction["state"] in {"failed_no_write", "rolled_back"}:
                retained = self.state.get_session(self.transaction["session_id"])["state"] == "saved"
                messagebox.showerror("恢复未完成，已回到原保存状态" if retained else "保存未完成，已释放会话占用", error + ("\n本次恢复未生效，可核对原保存结果或重新发起恢复审批。" if retained else "\n本次未写入或已经回滚，请重新生成计划和审批。"), parent=self.root)
                self.close()
                return
            self.artifact = load_artifact(self.path)
            self._block_review("执行结果需要核对，请回到当前对话恢复会话。")
            messagebox.showerror("事务未完成", error, parent=self.root)
        except Exception as exc:
            self._block_review("无法读取执行结果，请回到当前对话恢复会话。")
            messagebox.showerror("结果需要核对", f"{error}\n{exc}", parent=self.root)

    def run(self):
        self.root.fit_to_screen()
        self.root.show()
        try:
            self.qt_app.exec()
        finally:
            self.close()

    def session_marker(self) -> str:
        parts = []
        if self.session.get("conversation_id"):
            parts.append(f"对话 {self.session['conversation_id']}")
        if self.session.get("id"):
            parts.append(f"会话 {self.session['id'][:8]}")
        parts.append(f"审批 {self.transaction_id[:8]}")
        return " · ".join(parts)

    def load_preview_registry(self) -> tuple[dict[str, Any], str]:
        """Read display titles from the exact bridge bound to this approval.

        Missing metadata falls back to actual parameter keys; display metadata
        never changes the signed artifact or controls approval readiness.
        """
        expected = (self.artifact.get("compiler_evidence") or {}).get("compiler_binding") or {}
        if not expected:
            return {}, "未绑定元件标题元数据；按实际参数 Key 展示完整配置。"
        try:
            bridge = CompilerBridge(self.config)
            if sha256_text(bridge.path.read_text(encoding="utf-8")) != expected.get("bridge_sha256"):
                raise ValueError("bridge changed")
            registry = bridge._call({"command": "describe_registry"}, timeout_seconds=8)
            if (
                registry.get("command") != "describe_registry"
                or bridge._stable_registry_hash(registry) != expected.get("registry_hash")
                or registry.get("rule_source_sha256") != expected.get("rule_source_sha256")
                or registry.get("frontend_version") != expected.get("frontend_version")
            ):
                raise ValueError("registry changed")
            return {element["key"]: element for element in registry["elements"]}, "配置来自草稿备份与待保存画布；参数名称来自本次审批绑定的真实元件注册表。"
        except Exception:
            return {}, "未能核对元件标题元数据；按实际参数 Key 展示，配置值保持完整。"

    def change_details(self, changes=None) -> str:
        if changes is None:
            before = json.loads(self.session["baseline"]["data"])
            after = json.loads(self.artifact["desired_design"]["data"])
            changes = component_changes(before, after)
        chunks = []
        for item in changes:
            side = item["after"] or item["before"]
            node = side["node"]
            old = item["before"]["node"] if item["before"] else None
            new = item["after"]["node"] if item["after"] else None
            chunks.append(f"{item['kind']} · {side['group']} · 第 {side['line']} 行 · {node.get('title') or node['elementKey']}\n节点：{node.get('key', 'globalVariables')}\n修改前：\n{json.dumps(old, ensure_ascii=False, indent=2)}\n修改后：\n{json.dumps(new, ensure_ascii=False, indent=2)}")
        return "\n\n".join(chunks) or "画布节点未发生变更。"

    def current(self) -> dict[str, Any]:
        return self.artifact["statements"][self.index]

def main() -> int:
    if len(sys.argv) not in {2, 4}:
        raise SystemExit("Usage: approval_app.py <transaction_id> [conversation_id owner_revision]")
    try:
        ApprovalWindow(
            sys.argv[1], sys.argv[2] if len(sys.argv) == 4 else "",
            int(sys.argv[3]) if len(sys.argv) == 4 else None,
        ).run()
    except ApprovalWindowBusyError as exc:
        messagebox.showinfo("审批窗口已打开", str(exc))
        return 2
    except StateError as exc:
        messagebox.showerror("审批会话不可用", str(exc))
        return 1
    except (TransactionPlanError, SingleRowCheckError) as exc:
        messagebox.showerror("修改已被检查器阻止", str(exc))
        return 1
    except Exception as exc:
        messagebox.showerror("审批窗口未能打开", f"{exc}\n\n请在当前对话调用 abort_edit_session 打开处理窗口；可取消未执行审批，或保留数据并释放本地流程。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

