"""Native, read-only visual comparison of data flow and observed state."""
from __future__ import annotations

import math
import difflib
import html
import json
import re

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox, QDialog, QGraphicsObject, QGraphicsScene, QGraphicsTextItem, QGraphicsView,
    QHBoxLayout, QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from .logic_flow_model import MISSING, field_names, field_states, flow_comparison, flow_options, output_states, trace_events, value_text
from .logic_review_model import review_display_context
from .qt_theme import label

COLORS = {"blue": ("#eef5ff", "#3875cb"), "green": ("#edf9f0", "#27864c"),
          "red": ("#fff0ef", "#c64e46"), "amber": ("#fff8e9", "#a97718"),
          "purple": ("#f4efff", "#845ab5"), "neutral": ("#f7f8fb", "#717b8f")}


def difference_html(before, after, version):
    """Highlight the actual changed text while retaining every character."""
    values = [str(before), str(after)]
    side = 0 if version == "baseline" else 1
    tokens = [re.findall(r"[\w$]+|\s+|[^\w\s]", value) for value in values]
    if max(map(len, tokens)) > 6000:
        return '<div style="white-space:pre-wrap">' + html.escape(values[side]) + '</div>'
    parts = []
    for operation, old_start, old_end, new_start, new_end in difflib.SequenceMatcher(
        None, tokens[0], tokens[1], autojunk=False,
    ).get_opcodes():
        start, end = (old_start, old_end) if side == 0 else (new_start, new_end)
        text = html.escape("".join(tokens[side][start:end]))
        if operation != "equal" and text:
            background, color = ("#ffe0dc", "#9b251e") if side == 0 else ("#d9f1df", "#176336")
            text = f'<span style="background-color:{background};color:{color};font-weight:600">{text}</span>'
        parts.append(text)
    return '<div style="white-space:pre-wrap">' + "".join(parts) + '</div>'


class FlowCard(QGraphicsObject):
    activated = Signal(object)
    details_requested = Signal(object)

    def __init__(self, role, title, body="", *, width=220, height=128, color="neutral", target=None, detail="", diamond=False,
                 full_text=False, body_html=None, compact=False, inline_changes=None, version="candidate", detail_payload=None):
        super().__init__()
        self.card_width, self.card_height = width, height
        self.color, self.target, self.diamond = color, target, diamond
        self.detail_payload, self.version = detail_payload, version
        self.inline_changes = list(inline_changes or [])
        self.inline_change_items = []
        self.details_button = None
        self.marker_item = None
        self.text_items = []
        self.setToolTip(detail or "\n".join(str(value) for value in (role, title, body) if value))
        if target or detail_payload is not None:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        role_item = self._text(role, 10 if compact else 9, COLORS[color][1], True)
        role_item.setPos(16, 10)
        title_item = self._text(str(title) if full_text else self._short(title, 100), 12, "#17253d", True)
        title_item.setPos(16, 32)
        body_item = self._text(str(body) if full_text else self._short(body, 150), 11 if full_text else 9, "#53637b")
        if body_html is not None:
            body_item.setHtml(body_html)
        if detail_payload is not None:
            # Keep graph blocks compact; the dialog retains the original values.
            self._limit_lines(title_item, 2 if self.inline_changes else 3)
            self._limit_lines(body_item, 1 if self.inline_changes else 4)
        body_item.setPos(16, 16 + role_item.boundingRect().height() if compact else max(64, 32 + title_item.boundingRect().height() + 3))
        short_marker = detail_payload.get("short_marker") if isinstance(detail_payload, dict) else None
        if short_marker:
            self.marker_item = self._text(str(short_marker), 8, "#6d7688")
            self._limit_lines(self.marker_item, 1)
            self.marker_item.setToolTip(str(detail_payload.get("marker") or short_marker))
            self.marker_item.setPos(16, title_item.y() + title_item.boundingRect().height() + 3)
            body_item.setY(max(body_item.y(), self.marker_item.y() + self.marker_item.boundingRect().height() + 3))
        self.card_height = max(height, body_item.y() + body_item.boundingRect().height() + 12)
        if detail_payload is not None:
            self._add_inline_changes(body_item.y() + body_item.boundingRect().height() + 10)

    def _limit_lines(self, item, lines):
        from PySide6.QtGui import QFontMetricsF

        original = item.toPlainText()
        max_height = QFontMetricsF(item.font()).lineSpacing() * lines + 2
        if item.boundingRect().height() <= max_height:
            return False
        lower, upper = 0, len(original)
        while lower < upper:
            middle = (lower + upper + 1) // 2
            item.setPlainText(original[:middle].rstrip() + "…")
            if item.boundingRect().height() <= max_height:
                lower = middle
            else:
                upper = middle - 1
        item.setPlainText(original[:lower].rstrip() + "…")
        return True

    @staticmethod
    def _description_change(change):
        return str(change.get("path", "")).split(".")[-1] == "description" or "元件说明" in str(change.get("field", ""))

    def _add_inline_changes(self, top):
        from PySide6.QtWidgets import QGraphicsProxyWidget

        changes = self.inline_changes
        primary = [change for change in changes if not self._description_change(change)]
        descriptions = len(changes) - len(primary)
        shown = primary or changes
        complete = len(shown) <= 3 and all(
            len(str(change.get(side, ""))) <= 100 for change in shown for side in ("before", "after")
        )
        side = "before" if self.version == "baseline" else "after"
        text_color = "#9b251e" if self.version == "baseline" else "#176336"

        def add_items(full):
            y = top
            for change in shown if full else shown[:2]:
                field = str(change.get("field", "改动")).split("/")[-1].strip()
                heading = str(change.get("kind", "修改")) + " · " + self._short(field, 28)
                value = str(change.get(side, ""))
                item = self._text("", 9, text_color)
                if full:
                    item.setHtml("<b>" + html.escape(heading) + "</b>" + difference_html(
                        change.get("before", ""), change.get("after", ""), self.version,
                    ))
                else:
                    # Plain text preserves literal HTML/code while shortening only the preview.
                    item.setPlainText(heading + "\n" + self._short(value, 80))
                    self._limit_lines(item, 3)
                item.setPos(16, y)
                self.inline_change_items.append(item)
                y += item.boundingRect().height() + 7
            return y

        y = add_items(complete)
        # The graph must stay readable even when a few values wrap over many lines.
        if complete and y + (22 if primary and descriptions else 0) > 282:
            for item in self.inline_change_items:
                self.text_items.remove(item)
                item.setParentItem(None)
                if item.scene() is not None:
                    item.scene().removeItem(item)
            self.inline_change_items = []
            complete = False
            y = add_items(False)

        note = ""
        if primary and descriptions:
            note = "另有元件说明更新"
        remaining = max(0, len(shown) - len(self.inline_change_items))
        if remaining:
            note = f"还有 {remaining} 项改动，点击查看全部" + ("（含说明）" if descriptions else "")
        elif shown and not complete:
            note = "长内容已收起，点击查看全部"
        if note:
            note_item = self._text(note, 8, "#6d7688")
            self._limit_lines(note_item, 1)
            note_item.setPos(16, y)
            y += note_item.boundingRect().height() + 4

        self.details_button = QPushButton(f"查看全部 · {len(changes)} 项改动" if changes else "查看完整内容")
        self.details_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.details_button.setFixedSize(int(self.card_width - 32), 29)
        self.details_button.setStyleSheet(
            "QPushButton { background: rgba(255,255,255,185); border: 1px solid #c7cfdb;"
            " border-radius: 6px; color: #285d96; font-size: 11px; padding: 2px 4px; }"
            "QPushButton:hover { background: #e7f1ff; border-color: #6b9bd6; }"
        )
        self.details_button.clicked.connect(lambda: self.details_requested.emit(self.detail_payload))
        self.details_proxy = QGraphicsProxyWidget(self)
        self.details_proxy.setWidget(self.details_button)
        self.card_height = min(330, max(self.card_height, y + 41))
        self.details_proxy.setPos(16, self.card_height - 39)

    @staticmethod
    def _short(text, limit):
        text = str(text)
        return text if len(text) <= limit else text[:limit - 1] + "…"

    def _text(self, text, size, color, bold=False):
        item = QGraphicsTextItem(self)
        item.setPlainText(str(text))
        item.setTextWidth(self.card_width - 32)
        font = QFont("Microsoft YaHei UI", size)
        font.setBold(bold)
        item.setFont(font)
        item.setDefaultTextColor(QColor(color))
        item.document().setDocumentMargin(0)
        item.setAcceptedMouseButtons(Qt.MouseButton.NoButton)
        self.text_items.append(item)
        return item

    def boundingRect(self):
        return QRectF(-1, -1, self.card_width + 2, self.card_height + 2)

    def paint(self, painter, option, widget=None):
        background, border = COLORS[self.color]
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(border), 1.3))
        painter.setBrush(QColor(background))
        painter.drawRoundedRect(QRectF(0, 0, self.card_width, self.card_height), 12, 12)
        if self.diamond:
            painter.setBrush(QColor(border))
            x = self.card_width - 18
            painter.drawPolygon(QPolygonF([QPointF(x, 12), QPointF(x + 5, 17), QPointF(x, 22), QPointF(x - 5, 17)]))

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and (self.target or self.detail_payload is not None):
            if self.detail_payload is not None:
                self.details_requested.emit(self.detail_payload)
            else:
                self.activated.emit(self.target)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and (self.target or self.detail_payload is not None):
            event.accept()
            return
        super().mousePressEvent(event)


class FlowView(QGraphicsView):
    def __init__(self, scene):
        super().__init__(scene)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setBackgroundBrush(QColor("#ffffff"))
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
        self.setMinimumHeight(280)
        self.user_zoom = False

    def fit_width(self):
        width = self.sceneRect().width()
        if width > 0:
            scale = max(0.55, min(1.15, (self.viewport().width() - 12) / width))
            self.resetTransform()
            self.scale(scale, scale)
        self.user_zoom = False

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.user_zoom:
            self.fit_width()

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.12 if event.angleDelta().y() > 0 else 1 / 1.12
            scale = self.transform().m11() * factor
            if 0.35 <= scale <= 2.5:
                self.scale(factor, factor)
                self.user_zoom = True
            event.accept()
            return
        super().wheelEvent(event)


class LogicFlowReview(QWidget):
    navigate_requested = Signal(str, str, str)
    group_changed = Signal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.report, self.before, self.after = {}, {}, {}
        self.action_context = {}
        self.node_locations = {"baseline": {}, "candidate": {}}
        self.display_context = None
        self.cases, self.specs, self.options, self.cards = [], {}, [], []
        self.change_cards, self.change_rows = [], []
        self.block_payloads = []
        self.comparison_dialog = None
        self.field_rows = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 10, 8, 0)
        self.intent = label("尚无原始要求", "subtitle", wrap=True)
        layout.addWidget(self.intent)
        controls = QHBoxLayout()
        controls.addWidget(label("查看场景"))
        self.case_picker = QComboBox()
        self.case_picker.setMinimumWidth(200)
        self.case_picker.currentIndexChanged.connect(self.render)
        controls.addWidget(self.case_picker, 1)
        controls.addWidget(label("逻辑链"))
        self.flow_picker = QComboBox()
        self.flow_picker.currentIndexChanged.connect(self.render)
        controls.addWidget(self.flow_picker, 1)
        self.compare_button = QPushButton("全部改动对比")
        self.compare_button.clicked.connect(self.show_comparison)
        controls.addWidget(self.compare_button)
        fit = QPushButton("适合宽度")
        fit.clicked.connect(lambda: self.view.fit_width())
        controls.addWidget(fit)
        layout.addLayout(controls)
        self.input_summary = label("", "muted", wrap=True)
        layout.addWidget(self.input_summary)
        self.scene = QGraphicsScene(self)
        self.view = FlowView(self.scene)
        layout.addWidget(self.view, 1)
        trace_controls = QHBoxLayout()
        self.trace_toggle = QPushButton("展开实测调用轨迹")
        self.trace_toggle.setCheckable(True)
        self.trace_toggle.toggled.connect(self.render)
        trace_controls.addWidget(self.trace_toggle)
        self.trace_page = QComboBox()
        self.trace_page.setVisible(False)
        self.trace_page.currentIndexChanged.connect(self.render)
        trace_controls.addWidget(self.trace_page)
        trace_controls.addStretch()
        trace_controls.addWidget(label("点击区块查看完整内容与对比 · Ctrl + 滚轮缩放", "muted"))
        layout.addLayout(trace_controls)

    def set_report(self, report, *, before=None, after=None, action_context=None, display_context=None):
        self.report, self.before, self.after = report or {}, before or {}, after or {}
        self.action_context = action_context or self.report.get("action_context") or {}
        self.display_context = (review_display_context(self.before, self.after)
                                if display_context is None else display_context)
        self.node_locations = {"baseline": self.display_context["before_locations"],
                               "candidate": self.display_context["after_locations"]}
        spec = self.report.get("spec") or {}
        scenarios = self.report.get("scenarios") or {}
        self.cases = scenarios.get("cases", []) if isinstance(scenarios, dict) else scenarios
        self.specs = {case.get("id"): case for case in (spec.get("scenarios") or {}).get("cases", [])}
        self.options = flow_options(self.before, self.after, self.report, display_context=self.display_context)
        for picker in (self.case_picker, self.flow_picker):
            picker.blockSignals(True)
            picker.clear()
        for case in self.cases:
            status = (case.get("candidate") or {}).get("status")
            prefix = {"passed": "符合 | ", "failed": "不符合 | "}.get(status, "待验证 | ")
            self.case_picker.addItem(prefix + str(case.get("title") or case.get("id")))
        if not self.cases:
            self.case_picker.addItem("尚无已执行场景 · 待验证")
        for option in self.options:
            self.flow_picker.addItem(option["title"])
        if not self.options:
            self.flow_picker.addItem("尚无可解析的节点关系")
        for picker in (self.case_picker, self.flow_picker):
            picker.blockSignals(False)
        # Open the scene where baseline/candidate behavior differs when possible.
        selected = next((index for index, case in enumerate(self.cases)
                         if (case.get("baseline") or {}).get("status") != (case.get("candidate") or {}).get("status")), 0)
        self.case_picker.blockSignals(True)
        self.case_picker.setCurrentIndex(selected)
        self.case_picker.blockSignals(False)
        self.render()

    def _text(self, text, x, y, *, size=11, color="#53637b", bold=False, width=1080):
        item = self.scene.addText(str(text), QFont("Microsoft YaHei UI", size))
        font = item.font()
        font.setBold(bold)
        item.setFont(font)
        item.setDefaultTextColor(QColor(color))
        item.setTextWidth(width)
        item.setPos(x, y)
        return item

    def _card(self, role, title, body, x, y, **kwargs):
        collection = kwargs.pop("collection", self.cards)
        card = FlowCard(role, title, body, **kwargs)
        card.setPos(x, y)
        card.activated.connect(self._navigate)
        card.details_requested.connect(self.show_block_comparison)
        self.scene.addItem(card)
        collection.append(card)
        return card

    def show_comparison(self):
        if not self.change_rows:
            return
        self._show_comparison(self.change_rows)

    def show_block_comparison(self, payload):
        self._show_comparison(payload.get("changes") or [], block=payload)

    def _target_marker(self, target, version=None):
        action = self.action_context.get("action_name") or "动作名称待确认"
        if self.action_context.get("action_code"):
            action += "（" + str(self.action_context["action_code"]) + "）"
        if not target or not target.get("node_key"):
            return action + " · 未关联独立画布元件", "无独立画布元件"
        identity = target.get("group_key"), target.get("node_key")
        versions = (version,) if version else ("candidate", "baseline")
        side = next((self.node_locations[key][identity] for key in versions if identity in self.node_locations[key]), None)
        if side is None:
            return action + " · " + str(target.get("group_key") or "") + " · 此版本不存在该元件", "此版本不存在该元件"
        node = side["node"]
        name = str(node.get("title") or node.get("elementKey") or target["node_key"])
        short = f"第 {side['line']} 行 · {name}"
        return action + " · " + str(side["group"]) + " · 画布" + short, short

    def _navigate_comparison_marker(self, url, rows, dialog):
        if url.scheme() != "gxp-marker":
            return
        index = url.path()
        if index.isdigit() and int(index) < len(rows):
            dialog.close()
            self._navigate(rows[int(index)].get("target"))

    def _show_comparison(self, rows, *, block=None):
        if self.comparison_dialog is not None:
            self.comparison_dialog.close()
            self.comparison_dialog.deleteLater()
            self.comparison_dialog = None
        dialog = QDialog(self)
        scope = block["role"] if block else "全部改动"
        dialog.setWindowTitle(scope + " · 修改前后完整对比")
        dialog.resize(1120, 760)
        layout = QVBoxLayout(dialog)
        marker = block.get("marker") if block else self._target_marker(self.options[self.flow_picker.currentIndex()] if self.options else None)[0]
        heading = label(str(marker) + "\n" + scope
                        + f" · {len(rows)} 项改动。高亮显示差异，可切换查看完整内容和配置原值。", wrap=True)
        heading.setMaximumHeight(66)
        layout.addWidget(heading)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.comparison_views = []
        self.comparison_raw_views = []
        self.comparison_full_views = []
        for version, title in (("baseline", "修改前"), ("candidate", "修改后")):
            panel = QWidget()
            content = QVBoxLayout(panel)
            content.addWidget(label(title, "subtitle"))
            tabs = QTabWidget()
            view = QTextBrowser()
            view.setOpenExternalLinks(False)
            view.setOpenLinks(False)
            view.anchorClicked.connect(lambda url, rows=rows, dialog=dialog: self._navigate_comparison_marker(url, rows, dialog))
            grouped = {}
            for index, change in enumerate(rows):
                native_target = change.get("target") or {}
                identity = native_target.get("group_key"), native_target.get("node_key")
                group = grouped.setdefault(identity, {"index": index, "target": native_target, "sections": []})
                heading = html.escape(str(change["kind"]) + " · " + str(change["field"]))
                body = difference_html(change["before"], change["after"], version)
                group["sections"].append(f'<h4>{heading}</h4><div style="white-space:pre-wrap">{body}</div>')
            sections = []
            for group in grouped.values():
                marker = html.escape(self._target_marker(group["target"], version)[0])
                sections.append(f'<h3><a href="gxp-marker:{group["index"]}" style="color:#285d96">{marker}</a></h3>'
                                + '<hr>'.join(group["sections"]))
            view.setHtml('<div style="font-size:12pt">' + ('<hr>'.join(sections) if sections else "本区块未改动。完整内容与配置可在旁边的页签查看。") + '</div>')
            tabs.addTab(view, "改动对比")
            entry = block.get(version) if block else None
            if block:
                full_view = QTextBrowser()
                full_view.setOpenExternalLinks(False)
                if entry is None:
                    full_view.setPlainText("此版本不存在该区块。")
                else:
                    full_view.setPlainText(str(entry.get("title") or "") + "\n\n" + str(entry.get("body") or ""))
                tabs.addTab(full_view, "完整内容")
                self.comparison_full_views.append(full_view)
                if not rows:
                    tabs.setCurrentWidget(full_view)
            raw_view = QPlainTextEdit()
            raw_view.setReadOnly(True)
            raw_sections = []
            if block:
                raw_sections.append("本区块配置\n" + (json.dumps(entry.get("identity"), ensure_ascii=False, indent=2)
                                    if entry is not None else "此版本不存在该区块"))
            for change in rows:
                value = change.get("before_value" if version == "baseline" else "after_value", MISSING)
                raw = "未配置此项" if value is MISSING else json.dumps(value, ensure_ascii=False, indent=2)
                raw_sections.append(self._target_marker(change.get("target"), version)[0] + "\n"
                                    + str(change["field"]) + "\n" + str(change["path"]) + "\n" + raw)
            raw_view.setPlainText("\n\n".join(raw_sections))
            tabs.addTab(raw_view, "配置原值")
            self.comparison_raw_views.append(raw_view)
            content.addWidget(tabs)
            self.comparison_views.append(view)
            splitter.addWidget(panel)
        layout.addWidget(splitter, 1)
        actions = QHBoxLayout()
        self.comparison_locate_button = QPushButton("定位画布")
        target = block.get("target") if block else None
        self.comparison_locate_button.setVisible(block is not None)
        self.comparison_locate_button.setEnabled(bool(target and target.get("group_key")))
        self.comparison_locate_button.setToolTip("跳转到当前区块对应的画布元件")
        def locate():
            dialog.close()
            self._navigate(target)
        self.comparison_locate_button.clicked.connect(locate)
        actions.addWidget(self.comparison_locate_button)
        actions.addStretch()
        close = QPushButton("关闭对比")
        close.clicked.connect(dialog.close)
        actions.addWidget(close)
        layout.addLayout(actions)
        self.comparison_dialog = dialog
        dialog.show()

    def _navigate(self, target):
        if target and target.get("group_key"):
            self.navigate_requested.emit(str(target["group_key"]), str(target.get("node_key") or ""), str(target.get("field_path") or ""))

    def _arrow(self, start, end, *, dashed=False, color="#a0aec0"):
        pen = QPen(QColor(color), 1.8)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.scene.addLine(start.x(), start.y(), end.x(), end.y(), pen)
        delta = end - start
        length = math.hypot(delta.x(), delta.y()) or 1
        direction = QPointF(delta.x() / length, delta.y() / length)
        normal = QPointF(-direction.y(), direction.x())
        self.scene.addPolygon(QPolygonF([end, end - direction * 7 + normal * 4, end - direction * 7 - normal * 4]), QPen(QColor(color)), QColor(color))

    def render(self, *_):
        if self.comparison_dialog is not None:
            self.comparison_dialog.close()
            self.comparison_dialog.deleteLater()
            self.comparison_dialog = None
        self.scene.clear()
        self.cards = []
        self.change_cards = []
        case = self.cases[self.case_picker.currentIndex()] if self.cases and self.case_picker.currentIndex() >= 0 else {}
        spec = self.specs.get(case.get("id"), {})
        names = field_names(self.before, self.after, display_context=self.display_context)
        self.field_rows = field_states(case, spec, names)
        fields_by_id = {row["id"]: row for row in self.field_rows if row["attribute"] == "value"}
        requirements = self.report.get("requirements") or (self.report.get("spec") or {}).get("requirements") or []
        requirement_ids = case.get("requirement_ids") or spec.get("requirement_ids") or []
        selected_requirements = [row for row in requirements if not requirement_ids or row.get("id") in requirement_ids]
        self.intent.setText("你的要求：" + ("；".join(str(row.get("text") or row.get("source_text")) for row in selected_requirements) or "缺少已确认的业务预期 · 待验证"))
        environment = spec.get("environment") or {}
        mode = environment.get("page_mode")
        modes = {"add": "新增", "edit": "编辑"}
        inputs = ["页面模式：" + str(modes.get(mode, mode))] if mode is not None else []
        for identifier, attrs in (environment.get("fields") or {}).items():
            inputs.append(names.get(identifier, identifier) + " = " + FlowCard._short(value_text(attrs.get("value", MISSING)), 60))
        self.input_summary.setText("场景输入：" + ("  ·  ".join(inputs[:8]) or "未提供输入，字段变化待验证"))
        self.input_summary.setToolTip("\n".join(inputs))
        target = self.options[self.flow_picker.currentIndex()] if self.options and self.flow_picker.currentIndex() >= 0 else {}
        group_key = target.get("group_key") or ""
        group = next((group for data in (self.after, self.before) for group in data.get("actionData", []) if group.get("key") == group_key), {})
        self.selected_group = str(group_key), str(group.get("title") or group_key)
        self.group_changed.emit(*self.selected_group)
        comparison = flow_comparison(self.before, self.after, target, display_context=self.display_context)
        self.change_rows = comparison["changes"]
        self.compare_button.setEnabled(bool(self.change_rows))
        self._text("01  逻辑关系与改动", 12, 0, size=14, bold=True, color="#17253d")
        self._text("改动显示在对应区块内；内容较多时收起，点击区块查看全部与前后对比。虚线表示配置关系。", 12, 32, size=10)
        y = 68
        lanes = [comparison["baseline"], comparison["candidate"]]
        self.block_payloads = []
        for column, block_role in enumerate(("数据来源", "执行条件", "处理动作", "流向目标")):
            entries = [lane["cards"][column] if not lane["missing"] else None for lane in lanes]
            available = entries[1] or entries[0]
            payload = {"role": block_role, "baseline": entries[0], "candidate": entries[1],
                       "changes": (available or {}).get("changes") or [], "target": (available or {}).get("target")}
            payload["marker"], payload["short_marker"] = self._target_marker(payload["target"])
            for version, entry in zip(("baseline", "candidate"), entries):
                payload[version + "_marker"], payload[version + "_short_marker"] = self._target_marker((entry or {}).get("target"), version)
            self.block_payloads.append(payload)
        version_cards = {}
        # Lead with the result so the concrete new values are visible on entry.
        # Keep the collection in baseline/candidate order for other review tools.
        for index in (1, 0):
            lane = lanes[index]
            self._text("修改前" if index == 0 else "修改后", 12, y + 30, bold=True, color="#53637b" if index == 0 else "#3875cb", width=80)
            if lane["missing"]:
                card = self._card("配置关系待解析", lane["detail"], "保留完整证据供核对", 108, y, width=946, height=100, color="amber")
                version_cards[index] = [card]
                y += card.card_height + 20
                continue
            cards = []
            for column, entry in enumerate(lane["cards"]):
                changes = entry.get("changes") or []
                changed = bool(changes)
                color = ("amber" if index == 0 else "blue") if changed else "neutral"
                role = entry["role"] + (f" · {len(changes)} 项改动" if changed else " · 未改动")
                if column == 2 and not changed and lane["cards"][1].get("changes"):
                    color, role = "purple", "处理动作 · 受上级条件影响"
                title, body = entry["title"], entry["body"]
                version = "baseline" if index == 0 else "candidate"
                if column == 0 and entry.get("field"):
                    evidence = case.get(version) or {}
                    reads = [step for step in evidence.get("trace") or [] if step.get("api") == entry["field"] + ".value.read"]
                    body = ("读取 " + str(len(reads)) + " 次 · " + value_text(reads[-1].get("value", MISSING))) if reads else "本场景无读取记录"
                if column == 3 and entry.get("field") in fields_by_id:
                    row = fields_by_id[entry["field"]]
                    observed = row[version]
                    role = "目标字段 · 场景实测"
                    title = row["name"]
                    body = value_text(row["initial"]) + " → " + value_text(observed["value"]) + "\n" + observed["behavior"] + f" · 写 {observed['writes']} 次"
                    color = {"passed": "green", "failed": "red"}.get(observed["verdict"], "amber")
                cards.append(self._card(role, title, body, 108 + column * 244, y,
                                        width=216, height=116, color=color, target=entry.get("target"),
                                        detail=title + "\n" + body + "\n" + lane["detail"], diamond=column == 1,
                                        inline_changes=changes, version=version,
                                        detail_payload={**self.block_payloads[column],
                                                        "marker": self.block_payloads[column][version + "_marker"],
                                                        "short_marker": self.block_payloads[column][version + "_short_marker"]}))
            height = max(card.card_height for card in cards)
            version_cards[index] = cards
            for column in range(3):
                start_y = y + cards[column].card_height / 2
                end_y = y + cards[column + 1].card_height / 2
                self._arrow(QPointF(108 + column * 244 + 218, start_y), QPointF(108 + (column + 1) * 244 - 4, end_y), dashed=True)
            y += height + 18
        self.cards = version_cards.get(0, []) + version_cards.get(1, [])
        y += 10
        self._text("02  字段状态如何改变", 12, y, size=14, bold=True, color="#17253d")
        self._text("对比同一个场景。绿色 / 红色只表示该字段的断言；未声明预期或运行不完整时显示待验证。", 12, y + 32, size=10)
        y += 70
        for row in self.field_rows:
            self._text(row["name"] + ("  ·  " + row["id"] if row["name"] != row["id"] else ""), 12, y, bold=True, color="#17253d")
            y += 32
            cards = [self._card("场景初始值", value_text(row["initial"]), "分别运行修改前 / 后两版", 12, y, width=216, height=110)]
            for column, version in enumerate(("baseline", "candidate"), start=1):
                side = row[version]
                color = {"passed": "green", "failed": "red"}.get(side["verdict"], "amber" if row["asserted"] else "neutral")
                verdict = {"passed": "符合", "failed": "不符合"}.get(side["verdict"], "待验证" if row["asserted"] else "未声明预期")
                body = side["behavior"] + f" · 读 {side['reads']} / 写 {side['writes']} 次"
                if side["execution_status"] == "unverified":
                    body += " · 执行未完成验证"
                cards.append(self._card(("修改前结果" if version == "baseline" else "修改后结果") + " · " + verdict,
                    value_text(side["value"]), body, 12 + column * 280, y, width=248, height=110, color=color))
            cards.append(self._card("你的预期", value_text(row["expected"]) if row["asserted"] else "未声明此字段预期",
                                    "固定断言" if row["asserted"] else "仅展示观测结果", 852, y, width=216, height=110, color="purple" if row["asserted"] else "neutral"))
            height = max(card.card_height for card in cards)
            self._arrow(QPointF(230, y + height / 2), QPointF(286, y + height / 2))
            # Fan out from one input to two independent results; never draw
            # baseline -> candidate as if the versions ran sequentially.
            path = QPainterPath(QPointF(120, y + cards[0].card_height))
            path.lineTo(120, y + height + 10)
            path.lineTo(696, y + height + 10)
            self.scene.addPath(path, QPen(QColor("#a0aec0"), 1.6))
            self._arrow(QPointF(696, y + height + 10), QPointF(696, y + cards[2].card_height + 2))
            self._arrow(QPointF(824, y + height / 2), QPointF(846, y + height / 2), dashed=True, color="#b2a3c7")
            y += height + 32
        if not self.field_rows:
            card = self._card("尚无字段快照", "字段状态待验证", "请提供带字段输入与预期的场景；现有报告仍可在详细证据中查看。", 12, y, width=1056, color="amber")
            y += card.card_height + 20
        outputs = output_states(case, spec)
        if outputs:
            self._text("03  输出的数据", 12, y, size=14, bold=True, color="#17253d")
            self._text("这是离线场景观测到的元数据 / 返回值；是否发送到后端需运行环境验证。", 12, y + 32, size=10)
            y += 70
            for output in outputs:
                self._text(output["title"], 12, y, bold=True, color="#17253d")
                y += 32
                cards = []
                for column, version in enumerate(("baseline", "candidate")):
                    observed = output[version]
                    color = {"passed": "green", "failed": "red"}.get(observed["verdict"], "amber")
                    verdict = {"passed": "符合", "failed": "不符合"}.get(observed["verdict"], "待验证")
                    cards.append(self._card(("修改前" if version == "baseline" else "修改后") + " · " + verdict,
                        value_text(observed["value"]), output["key"], 12 + column * 366, y, width=322, height=110, color=color))
                cards.append(self._card("你的预期", value_text(output["expected"]) if output["expected"] is not MISSING else "未声明此项预期",
                                       "固定断言", 744, y, width=322, height=110, color="purple"))
                y += max(card.card_height for card in cards) + 20
        # Non-field assertions remain visible even when all field assertions pass.
        extras = []
        expected = case.get("expected", spec.get("expect", {}))
        if isinstance(expected, dict):
            extras = [key for key in expected if key != "fields"]
        if extras:
            self._text("还有其他验收项：" + "、".join(extras) + "；元数据、返回值和调用断言见详细证据。", 12, y, color="#845ab5", width=1056)
            y += 45
        traces = [trace_events(case.get(version) or {}, names) for version in ("baseline", "candidate")]
        pages = max(1, math.ceil(max((len(trace) for trace in traces), default=0) / 24))
        previous_page = max(0, min(self.trace_page.currentIndex(), pages - 1))
        self.trace_page.blockSignals(True)
        self.trace_page.clear()
        for page in range(pages):
            self.trace_page.addItem(f"轨迹 {page * 24 + 1}–{(page + 1) * 24} / 共 {max(map(len, traces), default=0)} 次调用")
        self.trace_page.setCurrentIndex(previous_page)
        self.trace_page.blockSignals(False)
        self.trace_page.setVisible(self.trace_toggle.isChecked() and pages > 1)
        if self.trace_toggle.isChecked():
            self.trace_toggle.setText("收起实测调用轨迹")
            self._text("实测调用轨迹", 12, y, size=14, bold=True, color="#17253d")
            self._text("实线箭头只表示先后顺序；相邻读写不等于已证明的数据依赖。", 12, y + 32, size=10)
            y += 70
            for version, trace in zip(("baseline", "candidate"), traces):
                self._text(("修改前" if version == "baseline" else "修改后") + f" · {len(trace)} 次调用", 12, y, bold=True)
                y += 34
                subset = trace[previous_page * 24:(previous_page + 1) * 24]
                if not subset:
                    self._text("此页无调用记录；无法据此判断分支命中。", 12, y, color="#a97718")
                    y += 40
                for offset in range(0, len(subset), 4):
                    items = subset[offset:offset + 4]
                    cards = [self._card(f"{entry['index'] + 1:02}  {entry['role']}", entry["title"], entry["body"],
                              12 + column * 280, y, width=216, height=100, color="blue" if entry["role"] == "读取" else "green" if entry["role"] == "写入" else "neutral",
                              detail=value_text(entry["detail"])) for column, entry in enumerate(items)]
                    height = max(card.card_height for card in cards)
                    for column in range(len(cards) - 1):
                        self._arrow(QPointF(230 + column * 280, y + height / 2), QPointF(286 + column * 280, y + height / 2))
                    y += height + 18
        else:
            self.trace_toggle.setText("展开实测调用轨迹")
        self.scene.setSceneRect(0, 0, 1092, y + 12)
        if not self.view.user_zoom:
            self.view.fit_width()
        self.view.verticalScrollBar().setValue(0)
