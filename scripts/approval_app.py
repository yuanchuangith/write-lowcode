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
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QHBoxLayout, QListWidget,
    QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSplitter, QTabWidget, QVBoxLayout, QWidget,
)
from gxp_editor.approval import approval_is_current, approve_statement, load_artifact, save_artifact
from gxp_editor.action_context import action_context, context_summary, publication_notice, publication_target, queue_publication_targets
from gxp_editor.action_context_ui import ActionContextBanner
from gxp_editor.approval_display import (
    STATEMENT_LABELS, canvas_change_summary, execution_result_message, parameter_details,
    reference_repair_summary, render_full_sql, statement_scope,
)
from gxp_editor.compiler import CompilerBridge
from gxp_editor.component_preview import component_changes
from gxp_editor.config import load_config
from gxp_editor.execution_lock import ApprovalWindowBusyError, approval_queue_lock, approval_window_lock
from gxp_editor.hashing import sha256_text
from gxp_editor.qt_review_ui import CodeReview, ComponentReview
from gxp_editor.logic_review import require_write_business_review, validate_business_review
from gxp_editor.logic_review_ui import LogicReview
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

    def _load_transaction(self):
        self.transaction = self.state.get_transaction(self.transaction_id)
        if self.transaction["state"] != "awaiting_approval":
            raise StateError("该审批已执行或已关闭，请从当前对话核对结果或重新生成计划。")
        self.artifact = load_artifact(self.path)
        validate_transaction_artifact(self.artifact)
        self.session = self.state.get_session(self.artifact["session_id"])
        self._check_owner(self.session)
        self.index = self._next_pending_index() or 0
        self._review_blocked = False

    def _initialize(self):
        self._load_transaction()
        self.qt_app = application()
        self.root = ApprovalShell(self)
        self.timer = QTimer(self.root)
        self.timer.timeout.connect(self.refresh_approval_state)
        self._show_transaction()
        self.root.fit_to_screen()
        self.timer.start(1000)

    def _show_transaction(self):
        self._canvas_sources = None
        context = self.artifact.get("action_context") or action_context(self.session.get("baseline"))
        self.root.setWindowTitle(f"GXP 草稿保存审批 · {context_summary(context)} · {self.session_marker()}")
        registry, registry_note = self.load_preview_registry()
        self._build_ui(registry, registry_note)
        self.render()

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
        heading.setContentsMargins(16, 10, 16, 8)
        context = self.artifact.get("action_context") or action_context(self.session.get("baseline"))
        self.target_context = ActionContextBanner(context, compact=True)
        self.header = self.target_context.action_label
        toolbar = self.target_context.toolbar
        title = label("草稿审批")
        title.setStyleSheet("font-size: 20px; font-weight: 700;")
        toolbar.insertWidget(0, title)
        self.approved_metric = label("")
        self.changed_metric = label("")
        metrics = QHBoxLayout()
        metrics.setSpacing(6)
        for value, caption in ((self.approved_metric, "SQL 审批"), (self.changed_metric, "画布")):
            hint = label(caption, "muted")
            hint.setStyleSheet("font-size: 11px;")
            metrics.addWidget(hint)
            value.setStyleSheet("font-weight: 600;")
            metrics.addWidget(value)
        toolbar.insertLayout(toolbar.indexOf(self.target_context.copy_button), metrics)
        notice = label("仅保存草稿", "muted")
        notice.setStyleSheet("font-size: 11px;")
        notice.setToolTip("此次仅保存未发布草稿，手动发布后才会生效。")
        toolbar.insertWidget(toolbar.indexOf(self.target_context.copy_button), notice)
        self.badge = label("等待审批", "badge")
        self.badge.setStyleSheet("font-size: 11px; padding: 4px 8px;")
        toolbar.insertWidget(toolbar.indexOf(self.target_context.copy_button), self.badge)
        heading.addWidget(self.target_context)
        page.addLayout(heading)
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
        side_layout.addWidget(label("批准本条 → 自动看下一条\n全部批准 → 确认保存草稿\n每条语句只影响 1 行", "muted", wrap=True))
        workspace.addWidget(sidebar)
        review = QWidget()
        review_layout = QVBoxLayout(review)
        review_layout.setContentsMargins(0, 0, 0, 0)
        review_layout.setSpacing(10)
        self.notebook = QTabWidget()
        self.notebook.setDocumentMode(True)
        self.component_review = ComponentReview(registry, registry_note)
        self.code_review = CodeReview()
        self.logic_review = LogicReview()
        self.logic_review.navigate_requested.connect(self.navigate_review_target)
        self.notebook.addTab(self.logic_review, "业务逻辑验收")
        self.notebook.addTab(self.component_review, "元件预览")
        self.target_context.bind_reviews(self.notebook, self.logic_review, self.component_review)
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
        self.summary = text_view()
        self.details_tabs.insertTab(0, self.summary, "当前语句范围")
        workspace.addWidget(review)
        workspace.setStretchFactor(0, 0)
        workspace.setStretchFactor(1, 1)
        workspace.setSizes([215, 970])
        area = QHBoxLayout()
        area.setContentsMargins(16, 0, 16, 8)
        area.addWidget(workspace)
        page.addLayout(area, 1)
        self.content_scroll.setWidget(content)
        shell.addWidget(self.content_scroll, 1)
        footer = QFrame()
        footer.setObjectName("footer")
        footer_layout = QVBoxLayout(footer)
        footer_layout.setContentsMargins(16, 5, 16, 6)
        footer_layout.setSpacing(3)
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

    def navigate_review_target(self, group_key, node_key, field_path=""):
        self.notebook.setCurrentWidget(self.component_review)
        self.component_review.select_canvas_node(group_key, node_key)
        if field_path:
            self.component_review.views.setCurrentWidget(self.component_review.parameters)
            parts = field_path.replace("/", ".").split(".")
            section = next((index for index, part in enumerate(parts) if part in {"inputParams", "outputParams"}), None)
            field = parts[section + 1] if section is not None and len(parts) > section + 1 else parts[-1]
            for index in range(self.component_review.parameters.topLevelItemCount()):
                entry = self.component_review.parameters.topLevelItem(index)
                if entry.data(0, Qt.ItemDataRole.UserRole) == field:
                    self.component_review.parameters.setCurrentItem(entry)
                    self.component_review.parameters.scrollToItem(entry)
                    break

    def navigate(self, direction):
        self.select_statement(max(0, min(len(self.artifact["statements"]) - 1, self.index + direction)))

    def _next_pending_index(self, start=0):
        statements = self.artifact["statements"]
        for offset in range(len(statements)):
            index = (start + offset) % len(statements)
            if not approval_is_current(self.artifact, statements[index]):
                return index
        return None

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
        self.progress.setText(
            f"全部 {len(statements)} 条已批准 · 请确认保存草稿"
            if all(states) else
            f"当前第 {self.index + 1}/{len(statements)} 条 · 已批准 {count}/{len(statements)} · 每条仅影响 1 行"
        )
        self.approve_button.setText(
            "本条已批准" if states[self.index] else
            "批准本条，继续下一条" if len(statements) - count > 1 else "批准最后一条"
        )
        self.approve_button.setToolTip("只批准当前一条 SQL；自动切换到下一条待审批项，全部批准后仍需确认保存。")
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
            **({"接口配置来源（草稿快照，未实时查询接口目录）": self.artifact["compiler_evidence"]["interface_metadata"]}
               if (self.artifact.get("compiler_evidence") or {}).get("interface_metadata") else {}),
            "说明": "完整 SQL 为审阅副本，执行使用已签名模板及绑定参数。此次仅保存草稿，需手动发布。本程序不调用后端编辑/发布接口、不修改 Redis 或 DLL/PDB。",
        }, ensure_ascii=False, indent=2))
        sources = self.session["baseline"]["data"], self.artifact["desired_design"]["data"]
        if getattr(self, "_canvas_sources", None) != sources:
            before, after = (json.loads(source) for source in sources)
            changes = component_changes(before, after)
            self.component_review.render(before, after, changes=changes)
            self.logic_review.set_report(self.artifact.get("business_review"), before=before, after=after,
                                         action_context=self.target_context.context)
            self.code_review.set_canvas_context(before, after, changes=changes)
            self.changes_text.setPlainText(canvas_change_summary(before, after, changes=changes))
            self.raw_changes_text.setPlainText(self.change_details(changes))
            self.changed_metric.setText(f"{len(changes)} 项")
            self._canvas_sources = sources
        self.code_review.update_sources(self.session["baseline"], self.artifact["desired_design"])
        ready = self.transaction["state"] == "awaiting_approval" and not self.artifact.get("execution_started") and not self._executing and not self._review_blocked
        try:
            require_write_business_review(self.artifact)
        except ValueError as exc:
            ready = False
            self.progress.setText(f"仅可查看：业务验收证据不足或已过期，请重新规划。{exc}")
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
        require_write_business_review(self.artifact)
        on_disk = load_artifact(self.path)
        validate_transaction_artifact(on_disk)
        require_write_business_review(on_disk)
        if on_disk["transaction_hash"] != self.artifact["transaction_hash"]:
            raise TransactionPlanError("审批内容已改变，请重新生成并检查。")
        if self.transaction["state"] != "awaiting_approval" or on_disk.get("execution_started") or self.artifact.get("execution_started"):
            raise StateError("该事务已经尝试执行或已关闭，必须重新生成事务并逐条审批。")

    def approve_current(self):
        if self._executing or self._review_blocked:
            return
        try:
            self._validate_current()
            if approval_is_current(self.artifact, self.current()):
                return
            if not messagebox.askyesno("确认审批", f"只批准当前第 {self.index + 1}/{len(self.artifact['statements'])} 条 SQL？\n\n{STATEMENT_LABELS.get(self.current()['kind'], self.current()['kind'])} · 仅影响 1 行\n\n批准后自动显示下一条待审批项，不会立即执行。\n全部批准后仍需确认保存。", parent=self.root):
                return
            self._validate_current()
            approve_statement(self.config, self.artifact, self.current()["id"])
            save_artifact(self.path, self.artifact)
            next_index = self._next_pending_index(self.index + 1)
            if next_index is not None:
                self.index = next_index
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
            self.progress.setText("正在保存、核验并结束会话，请等待执行结果…")
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
        if result.get("state") != "saved" or not (result.get("verified") and result.get("session_released")):
            self._block_review("草稿已保存，核验收尾未完成，请回到当前对话恢复会话。")
            messagebox.showerror("保存结果待核对", execution_result_message(result), parent=self.root)
            return
        result = {**result, "publication_targets": result.get("publication_targets") or [
            publication_target(self.artifact.get("action_context") or action_context(self.session["baseline"]), self.session)]}
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


class ApprovalQueueWindow(ApprovalWindow):
    """Keep one shell open while reviewing independently signed action transactions."""

    def __init__(self, queue_id: str, conversation_id: str):
        QObject.__init__(self)
        self.queue_id = queue_id
        self.conversation_id = conversation_id
        self.config = load_config()
        self.state = StateStore()
        self._executing = False
        self._window_lock = None
        self._queue_lock = None
        self._active_review = False
        self._waiting_for_lock = False
        self._queue_stopped = False
        self._last_queue_result = ""
        self._queue_items = self.state.edit_queue(queue_id, conversation_id)
        lock = approval_queue_lock(queue_id)
        lock.__enter__()
        self._queue_lock = lock
        try:
            self.qt_app = application()
            self.root = ApprovalShell(self)
            self.timer = QTimer(self.root)
            self.timer.timeout.connect(self.refresh_approval_state)
            self._advance_queue()
            self.root.fit_to_screen()
            if not self._queue_stopped:
                self.timer.start(1000)
        except BaseException:
            self.close()
            raise

    def _release_window_lock(self):
        ApprovalWindow._release_window_lock(self)
        lock = self._queue_lock
        self._queue_lock = None
        if lock is not None:
            lock.__exit__(None, None, None)

    def _item_owned(self, item):
        return (item["conversation_id"] == self.conversation_id
                and item["owner_revision"] == item["queued_owner_revision"])

    def _item_status(self, item):
        if not self._item_owned(item):
            return "已转交"
        state = item.get("transaction_state") or item["state"]
        if item.get("plan_status") == "blocked" and state == "planned":
            return "验收不符合，待修正"
        return {"locked": "待规划", "planned": "待提交审批", "awaiting_approval": "待审批",
                "saved": "已保存", "verified": "已核对", "finished": "已完成", "aborted": "已取消",
                "cancelled": "已取消", "failed_no_write": "失败，未写入", "rolled_back": "失败，已回滚",
                "executing": "执行结果待核对", "failed": "执行结果待核对",
                "saved_state_pending": "保存结果待核对", "closed_unverified": "已关闭，未验证"}.get(state, state)

    def _queue_banner(self):
        banner = card()
        layout = QVBoxLayout(banner)
        layout.setContentsMargins(16, 8, 16, 8)
        self.queue_progress = label("", "eyebrow", wrap=True)
        layout.addWidget(self.queue_progress)
        self.queue_choices = QComboBox()
        self.queue_choices.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.queue_choices.setMinimumContentsLength(20)
        layout.addWidget(self.queue_choices)
        self.queue_result = label(self._last_queue_result or "每个动作分别审批和保存；已保存的动作不会随下一项失败回滚。", "muted", wrap=True)
        layout.addWidget(self.queue_result)
        self.queue_choices.activated.connect(self._select_queue_item)
        self._update_queue_banner()
        return banner

    def _update_queue_banner(self):
        items = self._queue_items
        saved = sum(self._item_owned(item) and item.get("transaction_state") == "saved" for item in items)
        position = next((index + 1 for index, item in enumerate(items)
                         if self._active_review and item.get("transaction_id") == self.transaction_id), None)
        self.queue_progress.setText(f"多表单连续审批 · {'动作 ' + str(position) + '/' + str(len(items)) + ' · ' if position else ''}已保存 {saved}/{len(items)}")
        with QSignalBlocker(self.queue_choices):
            self.queue_choices.clear()
            for item in items:
                title = f"{item['position']}. {context_summary(item['action_context'])} · {self._item_status(item)}"
                self.queue_choices.addItem(title, item.get("transaction_id"))
                self.queue_choices.setItemData(self.queue_choices.count() - 1, title, Qt.ItemDataRole.ToolTipRole)
                enabled = self._item_owned(item) and item.get("transaction_state") == "awaiting_approval"
                self.queue_choices.model().item(self.queue_choices.count() - 1).setEnabled(enabled)
            self.queue_choices.setCurrentIndex(position - 1 if position else -1)
        self.queue_choices.setEnabled(not self._executing and not self._queue_stopped)
        notice = publication_notice(queue_publication_targets(items, self.conversation_id))
        self.queue_result.setText("\n\n".join(part for part in (self._last_queue_result, notice) if part)
                                 or "每个动作分别审批和保存；已保存的动作不会随下一项失败回滚。")

    def _build_ui(self, registry, registry_note):
        super()._build_ui(registry, registry_note)
        self.root.centralWidget().layout().insertWidget(0, self._queue_banner())
        self.execute_button.setText("执行并保存本动作")

    def _show_waiting(self, message):
        self._active_review = False
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(self._queue_banner())
        self.badge = label("队列已暂停" if self._queue_stopped else "等待下一动作", "badge")
        layout.addWidget(self.badge)
        self.progress = label(message, "muted", wrap=True)
        layout.addWidget(self.progress)
        layout.addStretch()
        self.approve_button = QPushButton("等待审批内容")
        self.execute_button = QPushButton("等待审批内容")
        self.approve_button.setEnabled(False)
        self.execute_button.setEnabled(False)
        self.close_button = QPushButton("关闭窗口，保留队列")
        self.close_button.clicked.connect(self.close)
        layout.addWidget(self.close_button)
        self.root.setCentralWidget(body)
        self.root.setWindowTitle("GXP 多表单连续审批 · " + self.queue_id[:8])

    def _advance_queue(self, transaction_id=None):
        if self._executing or self._queue_stopped:
            return
        self._queue_items = self.state.edit_queue(self.queue_id, self.conversation_id)
        self._waiting_for_lock = False
        uncertain = next((item for item in self._queue_items if self._item_owned(item)
                          and (item.get("transaction_state") in {"executing", "failed", "saved_state_pending"}
                               or item.get("transaction_state") == "saved" and item["state"] != "finished")), None)
        if uncertain:
            self._queue_stopped = True
            self._show_waiting(f"{context_summary(uncertain['action_context'])} 的执行结果需要核对，请在当前对话恢复该会话后重新打开队列。")
            self.timer.stop()
            return
        pending = [item for item in self._queue_items if self._item_owned(item)
                   and item.get("transaction_state") == "awaiting_approval"]
        item = next((item for item in pending if transaction_id is None or item["transaction_id"] == transaction_id), None)
        ApprovalWindow._release_window_lock(self)
        if item is None:
            self._show_waiting("当前没有待审批动作。窗口会保持打开；同一队列新增审批内容后会自动显示，无需重新打开。")
            return
        self.transaction_id = item["transaction_id"]
        self.owner_revision = item["queued_owner_revision"]
        self.transaction = self.state.get_transaction(self.transaction_id)
        self.path = Path(self.transaction["artifact_path"])
        lock = approval_window_lock(self.path)
        try:
            lock.__enter__()
        except ApprovalWindowBusyError:
            self._waiting_for_lock = True
            self._show_waiting("下一个动作已有独立审批窗口。关闭该窗口后，本队列会自动继续。")
            return
        self._window_lock = lock
        try:
            self._load_transaction()
            if (self.artifact["transaction_id"] != self.transaction_id
                    or self.artifact["session_id"] != item["id"]
                    or self.artifact["design_id"] != item["design_id"]
                    or self.session.get("queue_id") != self.queue_id):
                raise StateError("队列动作与审批文件身份不一致，请重新规划。")
            self._active_review = True
            self._show_transaction()
        except Exception as exc:
            self._queue_stopped = True
            self._show_waiting(f"审批队列已停止：{exc}")
            self.timer.stop()

    def _select_queue_item(self, index):
        if self._executing or self._queue_stopped:
            return
        if 0 <= index < len(self._queue_items):
            item = self._queue_items[index]
            if self._item_owned(item) and item.get("transaction_state") == "awaiting_approval":
                self._advance_queue(item["transaction_id"])

    def refresh_approval_state(self):
        if self._executing or self._queue_stopped:
            return
        try:
            if self._active_review:
                super().refresh_approval_state()
                if self._review_blocked:
                    return
                if self.transaction["state"] != "awaiting_approval":
                    self._advance_queue()
                    return
                self._queue_items = self.state.edit_queue(self.queue_id, self.conversation_id)
                self._update_queue_banner()
            else:
                items = self.state.edit_queue(self.queue_id, self.conversation_id)
                if items != self._queue_items or self._waiting_for_lock:
                    self._advance_queue()
        except Exception as exc:
            self._block_review(str(exc))

    def _block_review(self, error):
        self._queue_stopped = True
        super()._block_review(error)
        self._update_queue_banner()

    def approve_current(self):
        if self._active_review and not self._queue_stopped:
            super().approve_current()

    def execute(self):
        if self._active_review and not self._queue_stopped:
            super().execute()
            self._update_queue_banner()

    @Slot(object)
    def _execution_success(self, result):
        self.worker.wait()
        self._executing = False
        self.worker.deleteLater()
        if (result.get("state") != "saved" or not result.get("verified") or not result.get("session_released")
                or self.state.get_transaction(self.transaction_id)["state"] != "saved"
                or self.state.get_session(self.transaction["session_id"])["state"] != "finished"):
            self._block_review("保存结果需要核对，请在当前对话恢复该会话。")
            return
        self._last_queue_result = "草稿已保存并核验，编辑会话已结束。以下已完成动作需要重新发布后生效。"
        self._advance_queue()
        if not self._queue_stopped:
            self.timer.start(1000)

    @Slot(str)
    def _execution_failure(self, error):
        self.worker.wait()
        self._executing = False
        self.worker.deleteLater()
        try:
            transaction = self.state.get_transaction(self.transaction_id)
            if transaction["state"] in {"failed_no_write", "rolled_back"}:
                messagebox.showerror("当前动作未保存", error + "\n本动作未写入或已经回滚，队列将继续显示其他待审批动作。", parent=self.root)
                self._last_queue_result = f"上一动作未保存：{self.session['action_name']}。{error}"
                self._advance_queue()
                if not self._queue_stopped:
                    self.timer.start(1000)
                return
            self._block_review("执行结果需要核对，请在当前对话恢复该会话。" + error)
        except Exception as exc:
            self._block_review(f"无法读取保存结果：{error}；{exc}")


class PlanReviewWindow(QMainWindow):
    """File-backed plan evidence; deliberately has no approval/execution controller."""

    def __init__(self, envelope: dict[str, Any]):
        self.qt_app = application()
        super().__init__()
        if not isinstance(envelope, dict) or not isinstance(envelope.get("business_review"), dict):
            raise ValueError("文件缺少业务验收报告。")
        validate_business_review(envelope["business_review"])
        before, after = envelope.get("before"), envelope.get("after")
        if not all(isinstance(value, dict) and isinstance(value.get("actionData"), list) for value in (before, after)):
            raise ValueError("计划报告缺少完整的修改前后画布。")
        context = envelope.get("action_context") or {}
        self.setWindowTitle("GXP 业务计划 · " + context_summary(context))
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.addWidget(label("业务计划审阅", "title"))
        layout.addWidget(label("先核对需求、前后规则和场景结果。此窗口只展示计划文件。", "muted", wrap=True))
        self.target_context = ActionContextBanner(context)
        layout.addWidget(self.target_context)
        self.notebook = QTabWidget()
        self.logic_review = LogicReview()
        self.component_review = ComponentReview({}, "来源：本次计划中的草稿备份与候选画布；参数按实际 Key 展示。")
        self.notebook.addTab(self.logic_review, "业务逻辑验收")
        self.notebook.addTab(self.component_review, "元件预览")
        self.target_context.bind_reviews(self.notebook, self.logic_review, self.component_review)
        self.logic_review.set_report(envelope["business_review"], before=before, after=after, action_context=context)
        self.component_review.render(before, after)
        self.logic_review.navigate_requested.connect(self.navigate_review_target)
        layout.addWidget(self.notebook, 1)
        close = QPushButton("关闭审阅")
        close.clicked.connect(self.close)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.setCentralWidget(body)
        available = self.screen().availableGeometry()
        self.resize(min(1240, max(320, available.width() - 48)), min(880, max(240, available.height() - 64)))
        frame = self.frameGeometry()
        frame.moveCenter(available.center())
        self.move(frame.topLeft())

    def navigate_review_target(self, group_key, node_key, field_path=""):
        ApprovalWindow.navigate_review_target(self, group_key, node_key, field_path)

    def run(self):
        self.show()
        self.qt_app.exec()


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--queue":
        try:
            ApprovalQueueWindow(sys.argv[2], sys.argv[3]).run()
        except ApprovalWindowBusyError:
            return 2  # The existing queue window picks up newly prepared actions.
        except Exception as exc:
            messagebox.showerror("连续审批窗口无法打开", str(exc))
            return 1
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--plan-review":
        try:
            envelope = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
            PlanReviewWindow(envelope).run()
        except Exception as exc:
            messagebox.showerror("业务计划报告无法打开", str(exc))
            return 1
        return 0
    if len(sys.argv) not in {2, 4}:
        raise SystemExit("Usage: approval_app.py <transaction_id> [conversation_id owner_revision] | --queue <queue_id> <conversation_id> | --plan-review <report.json>")
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

