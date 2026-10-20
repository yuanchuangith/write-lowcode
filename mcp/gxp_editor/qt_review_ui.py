"""Read-only Qt views of signed canvas and generated code snapshots."""
from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QApplication, QDialog, QHBoxLayout, QHeaderView, QPushButton,
    QSplitter, QTabBar, QTabWidget, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .canvas_review_model import review_groups
from .code_review_model import aligned_code_rows
from .component_preview import component_changes, control_flow_impacts, parameter_rows, plain_description
from .generated_code_review import canvas_context
from .qt_theme import label, text_view

COLORS = {"新增": "#248a3d", "删除": "#d70015", "修改": "#b36b00", "移动": "#0066cc", "修改并移动": "#b36b00", "受条件影响": "#7656ab", "可能受控制流影响": "#7656ab"}
ICON_DIRECTORY = Path(__file__).resolve().parents[2] / "assets/action-icons"


def node_icon(key: str) -> QIcon:
    key = {"If": "IfCondition", "globalVariables": "SetVariable"}.get(key, key)
    return QIcon(str(ICON_DIRECTORY / (key + ".png"))) if re.fullmatch(r"[A-Za-z0-9_]+", key) else QIcon()


def highlight_lines(view, rows, side):
    selections = []
    for index, row in enumerate(rows):
        if row["kind"] == "equal":
            continue
        selection = QTextEdit.ExtraSelection()
        selection.cursor = QTextCursor(view.document().findBlockByNumber(index))
        selection.format.setBackground(QColor("#e8f2ff" if row["generated_only"] else "#fff0f0" if side == "before" else "#eaf8ee"))
        selection.format.setProperty(QTextFormat.Property.FullWidthSelection, True)
        selections.append(selection)
    view.setExtraSelections(selections)


class ComponentReview(QWidget):
    group_changed = Signal(str, str)
    def __init__(self, registry=None, registry_note="", parent=None):
        super().__init__(parent)
        self.registry = registry or {}
        self.items = []
        self.lookup = {"before": {}, "after": {}}
        self.selected_change = None
        self.dialogs = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        toolbar = QHBoxLayout()
        self.note = label("选择元件查看参数差异，双击打开完整配置。分支可折叠。", "muted", wrap=True)
        toolbar.addWidget(self.note, 1)
        self.details_button = QPushButton("查看配置")
        self.details_button.setEnabled(False)
        self.details_button.clicked.connect(self.open_details)
        toolbar.addWidget(self.details_button)
        self.control_button = QPushButton("定位上级条件")
        self.control_button.setEnabled(False)
        self.control_button.clicked.connect(self.select_controller)
        toolbar.addWidget(self.control_button)
        layout.addLayout(toolbar)
        self.views = QTabWidget()
        layout.addWidget(self.views, 1)
        self.canvas_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.trees = {}
        for side, title in (("before", "修改前 · 草稿备份"), ("after", "修改后 · 待保存")):
            tree = QTreeWidget()
            tree.setHeaderLabels([title, "位置 / 变更"])
            tree.setColumnWidth(0, 320)
            tree.setUniformRowHeights(True)
            tree.setIconSize(QSize(22, 22))
            tree.setMinimumWidth(220)
            tree.itemSelectionChanged.connect(lambda key=side: self.select(key))
            tree.itemDoubleClicked.connect(lambda *_: self.open_details())
            self.trees[side] = tree
            self.canvas_splitter.addWidget(tree)
        self.views.addTab(self.canvas_splitter, "画布对比")
        self.parameters = QTreeWidget()
        self.parameters.setHeaderLabels(["参数", "修改前", "修改后"])
        self.parameters.header().setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        self.parameters.setColumnWidth(0, 200)
        self.parameters.setColumnWidth(1, 330)
        self.parameters.setColumnWidth(2, 330)
        self.parameters.itemDoubleClicked.connect(lambda *_: self.open_details())
        self.views.addTab(self.parameters, "参数差异")
        self.registry_note = label(registry_note, "muted", wrap=True)
        layout.addWidget(self.registry_note)

    @staticmethod
    def identity(side):
        return side.get("group_key", "globalVariables"), side["node"].get("key", "globalVariables")

    def render(self, before, after, *, changes=None):
        self.items = component_changes(before, after) if changes is None else changes
        self.impacts = control_flow_impacts(before, after, changes=self.items)
        self.impact_map = {self.identity(item["after"] or item["before"]): item for item in self.impacts}
        self.canvases = {"before": before, "after": after}
        change_map = {self.identity(item["after"] or item["before"]): item for item in self.items}
        for side, tree in self.trees.items():
            with QSignalBlocker(tree):
                tree.clear()
                self.lookup[side] = {}
                for group in review_groups(before, after):
                    group_item = QTreeWidgetItem(tree, [group["title"], ""])
                    identity = group["key"], "__group_metadata__"
                    group_change = change_map.get(identity)
                    if group_change:
                        group_item.setText(1, group_change["kind"] + "分组")
                        group_item.setForeground(1, QColor(COLORS[group_change["kind"]]))
                        group_item.setData(0, Qt.ItemDataRole.UserRole, identity)
                        self.lookup[side][identity] = group_item
                    nodes = {}
                    for index, node in enumerate(group[side]):
                        identity = group["key"], node["key"]
                        change = change_map.get(identity)
                        impact = self.impact_map.get(identity)
                        kind = change["kind"] if change else impact["kind"] if impact else ""
                        display_kind = kind + (" · " + impact["kind"] if impact and change else "")
                        parent = next((nodes[key] for key in reversed(node.get("depth") or []) if key in nodes), group_item)
                        entry = QTreeWidgetItem(parent, [node.get("title") or node["elementKey"], f"第 {index + 1} 行" + (f" · {display_kind}" if display_kind else "")])
                        entry.setData(0, Qt.ItemDataRole.UserRole, identity)
                        entry.setIcon(0, node_icon(node["elementKey"]))
                        description = plain_description(node)
                        entry.setToolTip(0, description or node["elementKey"])
                        if kind:
                            entry.setForeground(1, QColor(COLORS[kind]))
                        nodes[node["key"]] = entry
                        self.lookup[side][identity] = entry
                tree.expandAll()
        if self.items:
            first = self.items[0]
            self.select_canvas_node(*self.identity(first["after"] or first["before"]))
        else:
            self.selected_change = None
            self.details_button.setEnabled(False)
            self.control_button.setEnabled(False)
            self.parameters.clear()
            self.note.setText("画布参数未变更。仍可选择元件核对配置。")

    def select(self, side):
        selected = self.trees[side].currentItem()
        identity = selected.data(0, Qt.ItemDataRole.UserRole) if selected else None
        if identity:
            self.select_canvas_node(*identity)

    def select_canvas_node(self, group_key, node_key):
        if not node_key:
            node_key = next((key[1] for side in ("after", "before") for key in self.lookup[side] if key[0] == group_key), "")
        identity = group_key, node_key
        change = next((item for item in self.items if self.identity(item["after"] or item["before"]) == identity), None)
        impact = self.impact_map.get(identity)
        if change is None and impact:
            change = impact
        for side, tree in self.trees.items():
            with QSignalBlocker(tree):
                entry = self.lookup[side].get(identity)
                tree.setCurrentItem(entry)
                if entry:
                    tree.scrollToItem(entry)
                elif change:
                    # An added/deleted node has no peer; reveal the nearest
                    # shared neighbor instead of leaving the other pane at top.
                    existing = change["after"] or change["before"]
                    for direction in ("previous", "next"):
                        neighbor = existing.get(direction)
                        if not neighbor:
                            continue
                        group_nodes = next((group["data"] for group in self.canvases["after" if change["after"] else "before"].get("actionData", []) if group["key"] == group_key), [])
                        index = neighbor["line"] - 1
                        if 0 <= index < len(group_nodes):
                            anchor = self.lookup[side].get((group_key, group_nodes[index]["key"]))
                            if anchor:
                                tree.scrollToItem(anchor)
                                break
        if change is None:
            change = {"kind": "未修改", "before": None, "after": None}
            for side in ("before", "after"):
                for group in review_groups(self.canvases["before"], self.canvases["after"]):
                    names = {node["key"]: node.get("title") or node["elementKey"] for node in group[side]}
                    for index, node in enumerate(group[side]):
                        if (group["key"], node["key"]) == identity:
                            change[side] = {"group": group["title"], "group_key": group["key"], "line": index + 1, "node": node,
                                            "parents": [names.get(key, key) for key in node.get("depth") or []]}
        if change["after"] is None and change["before"] is None:
            return
        self.selected_change = change
        selected_side = change["after"] or change["before"]
        self.group_changed.emit(str(selected_side.get("group_key") or group_key), str(selected_side.get("group") or group_key))
        self.details_button.setEnabled(True)
        side = change["after"] or change["before"]
        notice = "；控制流依赖已改变，执行结果仍需场景验证" if impact else ""
        self.note.setText(f"{side['group']} · 第 {side.get('line') or '—'} 行 · {side['node'].get('title') or side['node']['elementKey']} · {change['kind']}{notice}")
        self.control_button.setEnabled(bool(impact))
        self.parameters.clear()
        for row in parameter_rows(change, self.registry):
            entry = QTreeWidgetItem(self.parameters, [row["heading"] + " · " + row["after_title"], row["before"], row["after"]])
            entry.setData(0, Qt.ItemDataRole.UserRole, row["key"])
            for column, key in ((1, "before"), (2, "after")):
                entry.setToolTip(column, row[key])
                if row["changed"]:
                    entry.setBackground(column, QColor("#fff4e0"))

    def select_controller(self):
        if not self.selected_change:
            return
        identity = self.identity(self.selected_change["after"] or self.selected_change["before"])
        impact = self.impact_map.get(identity)
        if impact:
            target = impact["affected_by"][-1]
            self.select_canvas_node(target["group_key"], target["node_key"])

    def open_details(self):
        if not self.selected_change:
            return
        change = self.selected_change
        side = change["after"] or change["before"]
        dialog = QDialog(self)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.setWindowTitle("元件配置 · " + (side["node"].get("title") or side["node"]["elementKey"]))
        dialog.resize(1100, 740)
        layout = QVBoxLayout(dialog)
        layout.addWidget(label("元件配置", "title"))
        layout.addWidget(label("对照草稿备份与待保存配置，所有内容均只读。", "muted"))
        splitter = QSplitter(Qt.Orientation.Horizontal)
        rows = parameter_rows(change, self.registry)
        for key, title in (("before", "修改前"), ("after", "修改后")):
            panel = QWidget()
            body = QVBoxLayout(panel)
            body.addWidget(label(title, "badge"))
            current = change[key]
            if current is None:
                content = "尚未存在（新增元件）" if key == "before" else "将删除此元件"
            else:
                node = current["node"]
                content = f"位置：{current['group']} → 第 {current.get('line') or '—'} 行\n元件类型：{node['elementKey']}\n所在层级：{' → '.join(current.get('parents') or []) or '分组顶层'}\n\n{plain_description(node)}\n"
                for row in rows:
                    content += f"\n{'● ' if row['changed'] else ''}{row['heading']} · {row[key + '_title']}\n{row[key]}\n"
                content += "\n完整原始节点\n" + json.dumps(node, ensure_ascii=False, indent=2)
            body.addWidget(text_view(content), 1)
            splitter.addWidget(panel)
        layout.addWidget(splitter, 1)
        close = QPushButton("关闭详情")
        close.clicked.connect(dialog.close)
        layout.addWidget(close, alignment=Qt.AlignmentFlag.AlignRight)
        self.dialogs.append(dialog)
        dialog.destroyed.connect(lambda: self.dialogs.remove(dialog) if dialog in self.dialogs else None)
        dialog.show()


class CodeReview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.sources = {"before": {}, "after": {}}
        self.context = None
        self._render_key = None
        self.changes = []
        self.index = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.addWidget(label("比较整个动作的完整代码；元件参数与表达式见“元件预览”。", "muted", wrap=True))
        toolbar = QHBoxLayout()
        self.language = QTabBar()
        self.language.setExpanding(False)
        self.language.setDrawBase(False)
        for title, field in (("JavaScript", "js_code"), ("C#", "csharp_code")):
            self.language.setTabData(self.language.addTab(title), field)
        self.language.currentChanged.connect(self.render)
        toolbar.addWidget(self.language)
        self.previous = QPushButton("上一处")
        self.next = QPushButton("下一处")
        self.previous.clicked.connect(lambda: self.navigate(-1))
        self.next.clicked.connect(lambda: self.navigate(1))
        toolbar.addWidget(self.previous)
        toolbar.addWidget(self.next)
        self.status = label("", "muted")
        toolbar.addWidget(self.status, 1)
        layout.addLayout(toolbar)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.texts = {}
        for side, title in (("before", "修改前 · 原草稿"), ("after", "修改后 · 待保存")):
            panel = QWidget()
            body = QVBoxLayout(panel)
            body.setContentsMargins(0, 4, 0, 0)
            heading = QHBoxLayout()
            heading.addWidget(label(title, "muted"), 1)
            copy = QPushButton("复制完整代码")
            copy.clicked.connect(lambda _=False, key=side: self.copy(key))
            heading.addWidget(copy)
            body.addLayout(heading)
            view = text_view(code=True)
            self.texts[side] = view
            body.addWidget(view, 1)
            splitter.addWidget(panel)
        for side, peer in (("before", "after"), ("after", "before")):
            self.texts[side].verticalScrollBar().valueChanged.connect(lambda value, key=peer: self.sync_scroll(key, value))
        layout.addWidget(splitter, 1)
        layout.addWidget(label("红 / 绿：代码差异　蓝：识别到的临时变量一致改名，仅作审阅提示。复制保留完整原始代码。", "muted", wrap=True))

    def set_canvas_context(self, before, after, *, changes=None):
        self.context = canvas_context(before, after)

    def update_sources(self, before, after):
        self.sources = {"before": {key: before.get(key) or "" for key in ("js_code", "csharp_code")},
                        "after": {key: after.get(key) or "" for key in ("js_code", "csharp_code")}}
        if not self.sources["before"]["js_code"] and not self.sources["after"]["js_code"]:
            self.language.setCurrentIndex(1)
        self.render()

    def render(self):
        field = self.language.tabData(self.language.currentIndex())
        before, after = self.sources["before"].get(field, ""), self.sources["after"].get(field, "")
        key = before, after, self.context, field
        if key == self._render_key:
            return
        self._render_key = key
        rows, self.changes = aligned_code_rows(before, after, context=self.context)
        self.index = 0
        for side, view in self.texts.items():
            view.setPlainText("\n".join((f"{row[side + '_number']:5} │ " if row[side + '_number'] is not None else "      │ ") + row[side] for row in rows))
            highlight_lines(view, rows, side)
        self.navigate(0)

    def navigate(self, direction):
        self.index = max(0, min(len(self.changes) - 1, self.index + direction))
        if self.changes:
            for view in self.texts.values():
                view.setTextCursor(QTextCursor(view.document().findBlockByNumber(self.changes[self.index] - 1)))
                view.centerCursor()
        self.status.setText(f"差异 {self.index + 1} / {len(self.changes)}" if self.changes else "代码一致")
        self.previous.setEnabled(bool(self.changes) and self.index > 0)
        self.next.setEnabled(self.index + 1 < len(self.changes))

    def sync_scroll(self, side, value):
        bar = self.texts[side].verticalScrollBar()
        with QSignalBlocker(bar):
            bar.setValue(value)

    def copy(self, side):
        QApplication.clipboard().setText(self.sources[side].get(self.language.tabData(self.language.currentIndex()), ""))
