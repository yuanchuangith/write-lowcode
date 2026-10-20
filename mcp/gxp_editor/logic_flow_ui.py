"""Native, read-only visual comparison of data flow and observed state."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import (
    QComboBox, QGraphicsObject, QGraphicsScene, QGraphicsTextItem, QGraphicsView,
    QHBoxLayout, QPushButton, QVBoxLayout, QWidget,
)

from .logic_flow_model import MISSING, field_names, field_states, flow_lane, flow_options, output_states, same, trace_events, value_text
from .qt_theme import label

COLORS = {"blue": ("#eef5ff", "#3875cb"), "green": ("#edf9f0", "#27864c"),
          "red": ("#fff0ef", "#c64e46"), "amber": ("#fff8e9", "#a97718"),
          "purple": ("#f4efff", "#845ab5"), "neutral": ("#f7f8fb", "#717b8f")}


class FlowCard(QGraphicsObject):
    activated = Signal(object)

    def __init__(self, role, title, body="", *, width=220, height=128, color="neutral", target=None, detail="", diamond=False):
        super().__init__()
        self.card_width, self.card_height = width, height
        self.color, self.target, self.diamond = color, target, diamond
        self.text_items = []
        self.setToolTip(detail or "\n".join(str(value) for value in (role, title, body) if value))
        if target:
            self.setCursor(Qt.CursorShape.PointingHandCursor)
        role_item = self._text(role, 9, COLORS[color][1], True)
        role_item.setPos(16, 10)
        title_item = self._text(self._short(title, 100), 12, "#17253d", True)
        title_item.setPos(16, 32)
        body_item = self._text(self._short(body, 150), 9, "#53637b")
        body_item.setPos(16, max(64, 32 + title_item.boundingRect().height() + 3))
        self.card_height = max(height, body_item.y() + body_item.boundingRect().height() + 12)

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
        if event.button() == Qt.MouseButton.LeftButton and self.target:
            self.activated.emit(self.target)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.target:
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
        self.cases, self.specs, self.options, self.cards = [], {}, [], []
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
        trace_controls.addWidget(label("点击流程卡定位画布 · Ctrl + 滚轮缩放", "muted"))
        layout.addLayout(trace_controls)

    def set_report(self, report, *, before=None, after=None):
        self.report, self.before, self.after = report or {}, before or {}, after or {}
        spec = self.report.get("spec") or {}
        scenarios = self.report.get("scenarios") or {}
        self.cases = scenarios.get("cases", []) if isinstance(scenarios, dict) else scenarios
        self.specs = {case.get("id"): case for case in (spec.get("scenarios") or {}).get("cases", [])}
        self.options = flow_options(self.before, self.after, self.report)
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
        card = FlowCard(role, title, body, **kwargs)
        card.setPos(x, y)
        card.activated.connect(self._navigate)
        self.scene.addItem(card)
        self.cards.append(card)
        return card

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
        self.scene.clear()
        self.cards = []
        case = self.cases[self.case_picker.currentIndex()] if self.cases and self.case_picker.currentIndex() >= 0 else {}
        spec = self.specs.get(case.get("id"), {})
        names = field_names(self.before, self.after)
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
        self._text("01  逻辑如何改变", 12, 0, size=14, bold=True, color="#17253d")
        self._text("虚线为配置关系；字段值和读写次数来自当前场景实测。条件命中未单独采集。", 12, 32, size=10)
        y = 68
        target = self.options[self.flow_picker.currentIndex()] if self.options and self.flow_picker.currentIndex() >= 0 else {}
        group_key = target.get("group_key") or ""
        group = next((group for data in (self.after, self.before) for group in data.get("actionData", []) if group.get("key") == group_key), {})
        self.selected_group = str(group_key), str(group.get("title") or group_key)
        self.group_changed.emit(*self.selected_group)
        lanes = [flow_lane(data, target) for data in (self.before, self.after)]
        for index, lane in enumerate(lanes):
            self._text("修改前" if index == 0 else "修改后", 12, y + 30, bold=True, color="#53637b" if index == 0 else "#3875cb", width=80)
            if lane["missing"]:
                card = self._card("配置关系待解析", lane["detail"], "保留完整证据供核对", 108, y, width=946, height=100, color="amber")
                y += card.card_height + 20
                continue
            cards = []
            for column, entry in enumerate(lane["cards"]):
                other = lanes[1 - index]["cards"]
                changed = bool(other and not same(entry["identity"], other[column]["identity"]))
                color = ("amber" if index == 0 else "blue") if changed else "neutral"
                role = entry["role"] + (" · 已改变" if changed else "")
                if column == 2 and not changed and other and not same(lane["cards"][1]["identity"], other[1]["identity"]):
                    color, role = "purple", "处理动作 · 执行条件改变"
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
                                        detail=lane["detail"], diamond=column == 1))
            height = max(card.card_height for card in cards)
            for column in range(3):
                self._arrow(QPointF(108 + column * 244 + 218, y + height / 2), QPointF(108 + (column + 1) * 244 - 4, y + height / 2), dashed=True)
            y += height + 18
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
