"""Read-only business acceptance evidence; compilation never implies a pass."""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QHBoxLayout, QPushButton, QSizePolicy, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from .qt_theme import label, text_view
from .logic_review_model import change_display_context, review_display_context
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
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(4)
        self.status = label("尚无业务验收报告，业务行为待验证。", "muted")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        layout.addWidget(self.status)
        self.views = QTabWidget()
        self.flow = LogicFlowReview()
        self.flow.navigate_requested.connect(self.navigate_requested.emit)
        self.views.addTab(self.flow, "数据流与字段状态")
        evidence = QWidget()
        evidence_layout = QVBoxLayout(evidence)
        evidence_layout.setContentsMargins(0, 0, 0, 0)
        evidence_layout.setSpacing(4)
        self.views.addTab(evidence, "详细证据")
        layout.addWidget(self.views, 1)
        self.evidence_tabs = QTabWidget()
        self.requirements = self._section("需求与作用范围", ["需求", "原始要求", "目标", "场景 / 执行核对", "配置核对"])
        self.changes = self._section("前后规则与影响", ["位置 / 改动", "修改前", "修改后", "影响 / 范围"])
        self.scenarios = self._section("场景核对", ["场景 / 输入", "期望", "修改前实测", "修改后实测", "候选结果"])
        self.configurations = self._section("配置核对", ["规则 / 位置", "预期配置", "修改前", "修改后", "配置结果"])
        for index, width in enumerate((220, 180, 160, 160, 200)):
            self.configurations.setColumnWidth(index, width)
        self.evidence_tabs.setCurrentWidget(self.changes)
        self.evidence_tabs.currentChanged.connect(self.select_evidence_tab)
        evidence_layout.addWidget(self.evidence_tabs, 1)
        toolbar = QHBoxLayout()
        toolbar.addWidget(label("选择记录可定位元件或查看完整证据", "muted"), 1)
        self.locate = QPushButton("定位元件")
        self.locate.setEnabled(False)
        self.locate.clicked.connect(self.navigate)
        toolbar.addWidget(self.locate)
        self.locate_controller = QPushButton("定位影响来源")
        self.locate_controller.setEnabled(False)
        self.locate_controller.clicked.connect(self.navigate_controller)
        toolbar.addWidget(self.locate_controller)
        self.details_toggle = QPushButton("展开完整证据")
        self.details_toggle.setCheckable(True)
        self.details_toggle.toggled.connect(self.toggle_details)
        toolbar.addWidget(self.details_toggle)
        evidence_layout.addLayout(toolbar)
        self.details = text_view()
        self.details.setMinimumHeight(100)
        self.details.setMaximumHeight(180)
        self.details.hide()
        evidence_layout.addWidget(self.details)
        self.update_navigation()
        self.status.setToolTip("场景结果仅证明报告列出的输入和断言；页面时机、异步行为与发布后的运行效果需另行验证。")

    def _section(self, title, columns):
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
        self.evidence_tabs.addTab(tree, title)
        return tree

    def toggle_details(self, expanded):
        self.details.setVisible(expanded)
        self.details_toggle.setText("收起完整证据" if expanded else "展开完整证据")

    def select_evidence_tab(self, index):
        tree = self.evidence_tabs.widget(index)
        if tree.currentItem() is not None:
            self.select(tree)
            return
        self.selected_payload = None
        self.details.setPlainText("选择需求、规则或场景查看完整证据；双击或点击“定位元件”跳转到对应画布。")
        self.update_navigation()

    def update_navigation(self):
        payload = self.selected_payload or {}
        has_target = any(target.get("group_key") for target in self._targets(payload))
        has_controller = any(target.get("group_key") for target in payload.get("affected_by") or [])
        self.locate.setEnabled(has_target)
        self.locate_controller.setEnabled(has_controller)
        self.locate.setToolTip("跳转到所选记录对应的画布元件" if has_target else
                               "请先选择证据记录" if self.selected_payload is None else "此记录没有可定位的元件")
        self.locate_controller.setToolTip("跳转到影响所选元件的上游条件" if has_controller else
                                          "请先选择证据记录" if self.selected_payload is None else "此记录没有上游影响来源")

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

    def set_report(self, report, *, before=None, after=None, action_context=None):
        self.report = report if isinstance(report, dict) else {}
        self.selected_payload = None
        self.update_navigation()
        self.details_toggle.setChecked(False)
        self.details.clear()
        for tree in (self.requirements, self.changes, self.scenarios, self.configurations):
            tree.clear()
        canvas_checks = self.report.get("canvas_checks") or []
        configuration_statuses = [evidence_status(check) for check in canvas_checks]
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
            if "status" in requirement:
                reported_status = evidence_status(requirement)
                if reported_status == "不符合预期" or (reported_status == "待验证" and requirement_status == "通过"):
                    requirement_status = reported_status
            requirement_statuses.append(requirement_status)
            configuration_status = {"passed": "配置已核对", "failed": "配置不符合预期"}.get(requirement.get("configuration_status"), "配置待验证")
            entry = self._add(self.requirements, [requirement.get("text", MISSING), requirement.get("source_text", MISSING), target_text or "未指定", requirement_status, configuration_status], requirement)
            if requirement.get("reason"):
                entry.setToolTip(3, requirement["reason"])
            entry.setToolTip(4, str(requirement.get("configuration_reason") or "仅核对所列配置断言，未声明的配置仍需核对。")
                             + "\n配置核对不代表真实业务运行已验证。")
            entry.setForeground(3, QColor("#248a3d" if requirement_status == "通过" else "#d70015" if requirement_status == "不符合预期" else "#8b6500"))
            entry.setForeground(4, QColor("#248a3d" if configuration_status == "配置已核对" else "#d70015" if configuration_status == "配置不符合预期" else "#8b6500"))
        if not requirements:
            self._add(self.requirements, ["未提供原始需求", "需先补充可核对的业务要求", "未指定", "待验证", "配置待验证"], {})
        manual = sum(change.get("status") == "unverified" or change.get("in_scope") is False for change in self.report.get("changes") or [])
        unknown_requirements = requirement_statuses.count("待验证") or int(not requirements)
        failed = "不符合预期" in statuses or "不符合预期" in requirement_statuses or "不符合预期" in configuration_statuses or self.report.get("status") == "failed"
        complete = (self.report.get("status") == "passed" and statuses and all(item == "通过" for item in statuses)
                    and not unknown_requirements and not manual)
        status = "存在不符合预期的结果" if failed else "所列场景通过" if complete else "业务验收待验证"
        self.status.setText(f"{status} · 场景通过 {statuses.count('通过')} / 失败 {statuses.count('不符合预期')} / 待验证 {statuses.count('待验证')} · 需求待验证 {unknown_requirements} · 额外变化待核对 {manual}")
        if canvas_checks:
            self.status.setText(self.status.text() + f" · 配置通过 {configuration_statuses.count('通过')} / 失败 {configuration_statuses.count('不符合预期')} / 待验证 {configuration_statuses.count('待验证')}")
        self.status.setToolTip(self.status.text() + "\n配置核对仅覆盖所列断言，不代表真实业务运行已验证。"
                               + "\n场景结果仅证明报告列出的输入和断言；页面时机、异步行为与发布后的运行效果需另行验证。")
        context = review_display_context(before, after) if before is not None and after is not None else None
        for check in canvas_checks:
            target = check.get("target") or {}
            group_key, node_key = target.get("group_key"), target.get("node_key")
            side = ((context["after_locations"].get((group_key, node_key)) or context["before_locations"].get((group_key, node_key)))
                    if context is not None else None)
            location = (f"{side['group']} · 第 {side['line']} 行 · {side['node'].get('title') or side['node'].get('elementKey')}"
                        if side else f"{group_key or '未指定分组'} / {node_key or '未指定元件'}")
            operation = {"equals": "等于", "contains": "包含完整项", "unchanged": "保持原值"}.get(check.get("operator"), "待验证")
            expected = operation if check.get("operator") == "unchanged" else operation + "\n" + compact_value(check.get("expected", MISSING))
            before_value = check.get("before", MISSING) if check.get("before_present") else MISSING
            after_value = check.get("after", MISSING) if check.get("after_present") else MISSING
            verdict = {"passed": "配置已核对", "failed": "配置不符合预期"}.get(check.get("status"), "配置待验证")
            payload = {**deepcopy(check), "targets": [{"group_key": group_key, "node_key": node_key,
                        "field_path": "paramsValue" + str(check.get("path") or "")}]}
            entry = self._add(self.configurations, [f"{check.get('id', '配置检查')} · {location}\n{check.get('path', '')}", expected,
                before_value, after_value, verdict + "\n" + str(check.get("reason") or "")], payload)
            entry.setForeground(4, QColor("#248a3d" if check.get("status") == "passed" else "#d70015" if check.get("status") == "failed" else "#8b6500"))
        if not canvas_checks:
            self._add(self.configurations, ["尚未声明配置断言", "需要指定元件、参数路径和预期配置", MISSING, MISSING, "配置待验证"], {})
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
            for item in context["impacts"]:
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
        self.flow.set_report(self.report, before=before, after=after, action_context=action_context, display_context=context)
        self.selected_payload = None
        self.update_navigation()

    def select(self, tree):
        entry = tree.currentItem()
        if entry is None:
            return
        payload = entry.data(0, Qt.ItemDataRole.UserRole) or {}
        self.selected_payload = payload
        self.details.setPlainText(display_value(payload))
        self.update_navigation()

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
