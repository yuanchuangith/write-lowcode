"""Read-only business acceptance evidence; compilation never implies a pass."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from .qt_theme import label, text_view
from .logic_review_model import change_display_context, review_display_context
from .component_preview import control_flow_impacts
from .logic_flow_ui import LogicFlowReview


MISSING = object()


def display_value(value: Any) -> str:
    if value is MISSING:
        return "未提供（待验证）"
    if value is None:
        return "null（空值）"
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)


def compact_value(value: Any) -> str:
    """Keep tables scannable; the selection detail retains the complete evidence."""
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
    else:
        text = display_value(value)
    return text if len(text) <= 360 else text[:357] + "…"


def scenario_inputs(spec):
    environment = spec.get("environment") or {}
    rows = []
    if "page_mode" in environment:
        rows.append("页面模式：" + str(environment["page_mode"]))
    for field, attrs in environment.get("fields", {}).items():
        rows.append(field + "：" + compact_value(attrs))
    extra = {key: value for key, value in environment.items() if key not in {"page_mode", "fields"}}
    if extra:
        rows.append(compact_value(extra))
    steps = spec.get("steps") or []
    if steps:
        def step_text(step):
            if step.get("group_key"):
                return str(step["group_key"])
            if step.get("event_name"):
                return str(step.get("component_id") or "页面") + "." + str(step["event_name"])
            return compact_value(step)
        rows.append("步骤：" + " → ".join(step_text(step) for step in steps))
    return "\n".join(rows)


def asserted_values(actual, expected):
    if isinstance(actual, dict) and isinstance(expected, dict):
        return {key: asserted_values(actual[key], value) if key in actual else "未观测到此值"
                for key, value in expected.items()}
    return actual


def evidence_status(evidence: Any) -> str:
    if not isinstance(evidence, dict):
        return "待验证"
    return {"pass": "通过", "passed": "通过", "fail": "不符合预期", "failed": "不符合预期"}.get(evidence.get("status"), "待验证")


class LogicReview(QWidget):
    navigate_requested = Signal(str, str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.report = {}
        self.selected_payload = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        toolbar = QHBoxLayout()
        self.status = label("尚无业务验收报告，业务行为待验证。", "subtitle", wrap=True)
        toolbar.addWidget(self.status, 1)
        self.locate = QPushButton("定位元件")
        self.locate.setEnabled(False)
        self.locate.clicked.connect(self.navigate)
        toolbar.addWidget(self.locate)
        self.locate_controller = QPushButton("定位影响来源")
        self.locate_controller.setEnabled(False)
        self.locate_controller.clicked.connect(self.navigate_controller)
        toolbar.addWidget(self.locate_controller)
        layout.addLayout(toolbar)
        self.views = QTabWidget()
        self.flow = LogicFlowReview()
        self.flow.navigate_requested.connect(self.navigate_requested.emit)
        self.views.addTab(self.flow, "数据流与字段状态")
        evidence = QWidget()
        evidence_layout = QVBoxLayout(evidence)
        evidence_layout.setContentsMargins(0, 0, 0, 0)
        self.views.addTab(evidence, "详细证据")
        layout.addWidget(self.views, 1)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.requirements = self._section(splitter, "需求与作用范围", ["需求", "原始要求", "目标", "逐条核对"])
        self.changes = self._section(splitter, "前后规则与影响", ["位置 / 改动", "修改前", "修改后", "影响 / 范围"])
        self.scenarios = self._section(splitter, "场景核对", ["场景 / 输入", "期望", "修改前实测", "修改后实测", "候选结果"])
        splitter.setSizes([110, 220, 220])
        evidence_layout.addWidget(splitter, 3)
        self.details = text_view()
        self.details.setMinimumHeight(100)
        self.details.setMaximumHeight(220)
        evidence_layout.addWidget(self.details, 1)
        layout.addWidget(label("场景结果仅证明报告列出的输入和断言；页面时机、异步行为与发布后的运行效果需另行验证。", "muted", wrap=True))

    def _section(self, splitter, title, columns):
        panel = QWidget()
        body = QVBoxLayout(panel)
        body.setContentsMargins(0, 4, 0, 0)
        body.addWidget(label(title, "eyebrow"))
        tree = QTreeWidget()
        tree.setHeaderLabels(columns)
        tree.setRootIsDecorated(False)
        tree.setWordWrap(True)
        tree.setTextElideMode(Qt.TextElideMode.ElideNone)
        tree.setColumnWidth(0, 240)
        for index in range(1, len(columns)):
            tree.setColumnWidth(index, 230)
        tree.itemSelectionChanged.connect(lambda current=tree: self.select(current))
        tree.itemDoubleClicked.connect(lambda *_: self.navigate())
        body.addWidget(tree)
        splitter.addWidget(panel)
        return tree

    @staticmethod
    def _targets(payload):
        targets = list(payload.get("targets") or [])
        if not targets and payload.get("group_key"):
            targets = [payload]
        if not targets:
            for version in ("candidate", "baseline"):
                trace = (payload.get(version) or {}).get("trace") or []
                if isinstance(trace, list):
                    targets.extend(item for item in trace if isinstance(item, dict) and item.get("group_key"))
        return targets

    @staticmethod
    def _add(tree, values, payload):
        cells = [compact_value(value) for value in values]
        entry = QTreeWidgetItem(tree, cells)
        entry.setData(0, Qt.ItemDataRole.UserRole, payload)
        for index, value in enumerate(values):
            entry.setToolTip(index, display_value(value))
        metrics = tree.fontMetrics()
        height = max(metrics.boundingRect(0, 0, max(80, tree.columnWidth(index) - 20), 2000,
            int(Qt.TextFlag.TextWordWrap), cell).height() for index, cell in enumerate(cells))
        entry.setSizeHint(0, QSize(0, height + 20))
        return entry

    def set_report(self, report, *, before=None, after=None):
        self.report = report if isinstance(report, dict) else {}
        self.selected_payload = None
        self.locate.setEnabled(False)
        self.locate_controller.setEnabled(False)
        self.details.clear()
        for tree in (self.requirements, self.changes, self.scenarios):
            tree.clear()
        report_scenarios = self.report.get("scenarios") or {}
        cases = report_scenarios.get("cases", []) if isinstance(report_scenarios, dict) else report_scenarios
        case_status = {case.get("id"): evidence_status(case.get("candidate")) for case in cases}
        statuses = [evidence_status(case.get("candidate")) for case in cases]
        spec = self.report.get("spec") or {}
        scenario_specs = {case.get("id"): case for case in (spec.get("scenarios") or {}).get("cases", [])}
        requirements = self.report.get("requirements") or spec.get("requirements") or []
        requirement_map = {requirement.get("id"): requirement for requirement in requirements}
        requirement_statuses = []
        for requirement in requirements:
            targets = self._targets(requirement)
            target_text = "\n".join(target.get("label") or f"{target.get('group_key', '')} / {target.get('node_key') or '整组'}" for target in targets)
            related = requirement.get("case_ids") or requirement.get("scenario_ids") or [case.get("id") for case in cases
                if requirement.get("id") in (case.get("requirement_ids") or scenario_specs.get(case.get("id"), {}).get("requirement_ids", []))]
            # A global pass or another requirement's cases prove nothing here.
            linked_statuses = [case_status.get(identifier, "待验证") for identifier in related]
            requirement_status = ("不符合预期" if "不符合预期" in linked_statuses else "通过"
                                  if linked_statuses and all(value == "通过" for value in linked_statuses) else "待验证")
            requirement_statuses.append(requirement_status)
            entry = self._add(self.requirements, [requirement.get("text", MISSING), requirement.get("source_text", MISSING), target_text or "未指定", requirement_status], requirement)
            entry.setForeground(3, QColor("#248a3d" if requirement_status == "通过" else "#d70015" if requirement_status == "不符合预期" else "#8b6500"))
        if not requirements:
            self._add(self.requirements, ["未提供原始需求", "需先补充可核对的业务要求", "未指定", "待验证"], {})
        manual = sum(change.get("status") == "unverified" or change.get("in_scope") is False for change in self.report.get("changes") or [])
        unknown_requirements = requirement_statuses.count("待验证") or int(not requirements)
        failed = "不符合预期" in statuses or "不符合预期" in requirement_statuses or self.report.get("status") == "failed"
        complete = (self.report.get("status") == "passed" and statuses and all(item == "通过" for item in statuses)
                    and not unknown_requirements and not manual)
        status = "存在不符合预期的结果" if failed else "所列场景通过" if complete else "业务验收待验证"
        self.status.setText(f"{status} · 场景通过 {statuses.count('通过')} / 失败 {statuses.count('不符合预期')} / 待验证 {statuses.count('待验证')} · 需求待验证 {unknown_requirements} · 额外变化待核对 {manual}")
        context = review_display_context(before, after) if before is not None and after is not None else None
        for change in self.report.get("changes") or []:
            location = change.get("label") or " / ".join(str(value) for value in (change.get("group_key"), change.get("node_key"), change.get("field_path")) if value)
            before_value = change.get("before", MISSING) if change.get("before_present", True) else MISSING
            after_value = change.get("after", MISSING) if change.get("after_present", True) else MISSING
            before_rule = change.get("before_rule", before_value) if before_value is not MISSING else MISSING
            after_rule = change.get("after_rule", after_value) if after_value is not MISSING else MISSING
            impact = change.get("impact") or "影响尚未说明"
            payload = change
            if context is not None:
                display = change_display_context(change, before, after, context=context)
                location, impact = display["location"], display["impact"]
                before_rule = display["before_rule"]
                after_rule = display["after_rule"]
                # Preserve absent versus explicit null even when a native rule
                # description is shown instead of the leaf's full JSON.
                if before_value is MISSING or before_value is None:
                    before_rule = display_value(before_value) + "\n" + compact_value(before_rule)
                if after_value is MISSING or after_value is None:
                    after_rule = display_value(after_value) + "\n" + compact_value(after_rule)
                payload = {**change, "display_context": display}
            if change.get("in_scope") is False:
                impact = display_value(impact) + "\n超出声明的需求范围"
            self._add(self.changes, [location, before_rule, after_rule, impact], payload)
        # Derived display rows do not alter the signed report or imply a parameter edit.
        if context is not None:
            for item in control_flow_impacts(before, after):
                side = item["after"] or item["before"]
                node = side["node"]
                location = f"{side['group']} · 第 {side['line']} 行 · {node.get('title') or node['elementKey']}"
                sources = "；".join(parent["title"] for parent in item["affected_by"])
                payload = {"kind": item["kind"], "targets": [{"group_key": item["group_key"], "node_key": item["node_key"]}],
                           "affected_by": deepcopy(item["affected_by"]), "display_only": True}
                entry = self._add(self.changes, [location, "自身参数见元件配置", "控制流依赖发生变化",
                    f"{item['kind']}：{sources}；是否执行仍待场景验证"], payload)
                entry.setForeground(3, QColor("#8048a5"))
        for case in cases:
            payload = deepcopy(case)
            scenario_spec = scenario_specs.get(case.get("id"), {})
            payload["scenario_spec"] = deepcopy(scenario_spec)
            requirement_ids = case.get("requirement_ids") or scenario_spec.get("requirement_ids") or []
            if not payload.get("targets"):
                payload["targets"] = [deepcopy(target) for identifier in requirement_ids for target in (requirement_map.get(identifier) or {}).get("targets", [])]
            baseline, candidate = case.get("baseline") or {}, case.get("candidate") or {}
            name = case.get("title") or case.get("id") or "未命名场景"
            if "inputs" in case:
                name += "\n" + display_value(case["inputs"])
            elif scenario_spec:
                name += "\n" + scenario_inputs(scenario_spec)
            def actual(evidence):
                value = evidence.get("actual", MISSING)
                expected = case.get("expected")
                if isinstance(value, dict) and isinstance(expected, dict):
                    value = asserted_values(value, expected) if expected else value
                text = evidence_status(evidence) + "\n" + compact_value(value)
                return text + ("\n" + str(evidence["reason"]) if evidence.get("reason") else "")
            entry = self._add(self.scenarios, [name, case.get("expected", MISSING), actual(baseline), actual(candidate), evidence_status(candidate)], payload)
            status = evidence_status(candidate)
            entry.setForeground(4, QColor("#248a3d" if status == "通过" else "#d70015" if status == "不符合预期" else "#8b6500"))
        if not cases:
            self._add(self.scenarios, ["尚无已执行场景", "需补充输入与预期", MISSING, MISSING, "待验证"], {})
        self.details.setPlainText("选择需求、规则或场景查看完整证据；双击或点击“定位元件”跳转到对应画布。")
        self.flow.set_report(self.report, before=before, after=after)

    def select(self, tree):
        entry = tree.currentItem()
        if entry is None:
            return
        payload = entry.data(0, Qt.ItemDataRole.UserRole) or {}
        self.selected_payload = payload
        self.details.setPlainText(display_value(payload))
        self.locate.setEnabled(any(target.get("group_key") for target in self._targets(payload)))
        self.locate_controller.setEnabled(any(target.get("group_key") for target in payload.get("affected_by") or []))

    def navigate(self):
        for target in self._targets(self.selected_payload or {}):
            if target.get("group_key"):
                self.navigate_requested.emit(str(target["group_key"]), str(target.get("node_key") or ""), str(target.get("field_path") or ""))
                return

    def navigate_controller(self):
        for target in (self.selected_payload or {}).get("affected_by") or []:
            if target.get("group_key"):
                self.navigate_requested.emit(str(target["group_key"]), str(target.get("node_key") or ""), str(target.get("field_path") or ""))
                return
