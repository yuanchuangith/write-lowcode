"""Persistent form/action/group breadcrumb for all review windows."""
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QPushButton, QSizePolicy, QVBoxLayout

from .action_context import context_details
from .qt_theme import label


class ActionContextBanner(QFrame):
    def __init__(self, context=None, parent=None, *, compact=False):
        super().__init__(parent)
        self.context = context or {}
        self.current_group = ""
        self.compact = compact
        self.setObjectName("card")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 8 if compact else 9, 14, 8 if compact else 10)
        layout.setSpacing(5 if compact else 6)
        self.toolbar = toolbar = QHBoxLayout()
        kind = {"form": "表单动作", "public": "公共动作"}.get(self.context.get("action_type"), "动作归属待确认")
        if compact:
            toolbar.setSpacing(8)
            self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            toolbar.addStretch(1)
        else:
            toolbar.addWidget(label("你正在修改 · " + kind + " · 未发布草稿", "eyebrow"), 1)
        self.copy_button = QPushButton("复制定位信息")
        self.copy_button.setStyleSheet("padding: 4px 10px;")
        self.copy_button.clicked.connect(self.copy_identity)
        toolbar.addWidget(self.copy_button)
        layout.addLayout(toolbar)
        columns = QHBoxLayout()
        columns.setSpacing(12)
        if self.context.get("action_type") == "public":
            owner, page_id = "公共动作", "调用方表单需单独定位"
        else:
            owner = self.context.get("form_name") or "所属表单待确认"
            page_id = self.context.get("page_route") or self.context.get("page_id") or "缺少页面标识"
            if not self.context.get("form_name"):
                page_id += " · " + {"ambiguous": "匹配到多个页面", "unavailable": "页面信息不可读", "missing": "未找到对应页面"}.get(self.context.get("page_resolution"), "未解析名称")
        self.form_label, self.page_label = self._column(columns, "所属表单 / 页面", owner, page_id)
        columns.addWidget(label("→", "muted"))
        self.action_label, self.code_label = self._column(columns, kind if compact else "动作", self.context.get("action_name") or "动作名称待确认", self.context.get("action_code") or "缺少动作编号")
        columns.addWidget(label("→", "muted"))
        self.group_label, self.group_key_label = self._column(columns, "当前分组", "尚未选择分组", "跟随数据流图选择")
        layout.addLayout(columns)
        self.setToolTip(context_details(self.context))

    def _column(self, parent, title, value, detail):
        if self.compact:
            layout = QHBoxLayout()
            layout.setSpacing(6)
            layout.addWidget(label("表单 / 页面" if title == "所属表单 / 页面" else title, "eyebrow"))
            text = label(value)
            text.setStyleSheet("font-size: 13px; font-weight: 600; color: #17253d;")
            text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text.setMinimumWidth(0)
            text.setToolTip(f"{value}\n{detail}")
            secondary = label(detail, "muted")
            layout.addWidget(text, 1)
            layout.addWidget(secondary)
            secondary.hide()
            parent.addLayout(layout, 1)
            return text, secondary
        layout = QVBoxLayout()
        layout.setSpacing(1)
        layout.addWidget(label(title, "eyebrow"))
        text = label(value, wrap=True)
        text.setStyleSheet("font-size: 15px; font-weight: 600; color: #17253d;")
        secondary = label(detail, "muted", wrap=True)
        secondary.setStyleSheet("font-size: 11px;")
        layout.addWidget(text)
        layout.addWidget(secondary)
        parent.addLayout(layout, 1)
        return text, secondary

    def set_group(self, group_key, title):
        self.current_group = group_key
        self.group_label.setText(title or group_key or "尚未选择分组")
        self.group_key_label.setText(group_key or "跟随数据流图选择")
        if self.compact:
            self.group_label.setToolTip(f"{self.group_label.text()}\n{self.group_key_label.text()}")

    def bind_reviews(self, notebook, logic_review, component_review):
        def update(*_):
            if notebook.currentWidget() is logic_review:
                self.set_group(*getattr(logic_review.flow, "selected_group", ("", "")))
            elif notebook.currentWidget() is component_review:
                change = component_review.selected_change or {}
                side = change.get("after") or change.get("before") or {}
                self.set_group(str(side.get("group_key") or ""), str(side.get("group") or ""))
        logic_review.flow.group_changed.connect(update)
        component_review.group_changed.connect(update)
        notebook.currentChanged.connect(update)
        update()

    def copy_identity(self):
        text = context_details(self.context)
        if self.current_group:
            text += "\n当前分组：" + self.group_label.text() + "（" + self.current_group + "）"
        QApplication.clipboard().setText(text)
