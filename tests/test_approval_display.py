from __future__ import annotations

import copy
import importlib.util
import unittest
import tempfile
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pymysql
from pymysql.constants import SERVER_STATUS

from tests.helpers import PLUGIN_ROOT, baseline, editor_config, transaction
from gxp_editor.approval_display import canvas_change_summary, execution_result_message, parameter_details, render_full_sql, statement_scope


class ApprovalDisplayTests(unittest.TestCase):
    def canvas(self):
        nodes = [
            {"key": "new", "title": "定义变量", "elementKey": "SetVariable", "paramsValue": {"inputParams": {"variableValue": {"code": '(inbiz.getPageMode()=="add")'}}, "outputParams": {"variableName": {"code": "IsNewRecord"}}}},
            {"key": "query", "title": "获取页面参数", "elementKey": "QueryData", "paramsValue": {"inputParams": {"paramName": "revise"}, "outputParams": {"parameterResults": {"code": "revise"}}}},
            {"key": "assign", "title": "定义变量", "elementKey": "SetVariable", "paramsValue": {"inputParams": {"variableValue": {"code": "revise", "label": "局部变量-revise"}}, "outputParams": {"variableName": {"code": "inbiz('RevisedType').value", "label": "页面组件-变更类型-当前值"}}}},
        ]
        return {"actionData": [{"key": "main", "title": "主动作", "data": nodes}], "globalVariables": []}

    def test_real_initialization_rule_is_explained_without_json_or_node_keys(self):
        before = self.canvas()
        after = copy.deepcopy(before)
        expression = "(IsNewRecord && !relatedAttributes.isEmpty(revise)) ? revise : inbiz('RevisedType').value"
        after["actionData"][0]["data"][2]["paramsValue"]["inputParams"]["variableValue"] = {"paramTypes": "custom", "code": expression, "value": expression}
        summary = canvas_change_summary(before, after)
        for phrase in ("共 1 项", "主动作 → 第 3 行", "修改字段：变更类型", "直接使用页面参数", "重新打开已有申请时保留"):
            self.assertIn(phrase, summary)
        self.assertNotIn("paramsValue", summary)
        self.assertIn("元件配置 → 输入参数 → 变量值", summary)
        self.assertIn("上一行“获取页面参数”", summary)
        self.assertIn("未新增 IF 元件", summary)
        self.assertIn("变量值（完整）：" + expression, summary)
        self.assertNotIn("警告：", summary)

    def test_display_warns_when_custom_code_would_be_invisible_in_canvas_editor(self):
        before = self.canvas()
        after = copy.deepcopy(before)
        after["actionData"][0]["data"][2]["paramsValue"]["inputParams"]["variableValue"] = {
            "paramTypes": "custom", "value": "oldRule", "code": "newRule",
        }
        summary = canvas_change_summary(before, after)
        self.assertIn("变量值（完整）：oldRule", summary)
        self.assertIn("警告：显示值与生成表达式不一致", summary)

    def test_generated_metadata_and_shifted_line_numbers_are_not_reported_as_rule_edits(self):
        before = self.canvas()
        after = copy.deepcopy(before)
        after["actionData"][0]["data"][0]["description"] = "重新生成的说明"
        self.assertEqual(canvas_change_summary(before, after), "画布参数未变更。")
        after["actionData"][0]["data"].insert(0, {"key": "added", "title": "断点", "elementKey": "breakpoint", "paramsValue": {}})
        summary = canvas_change_summary(before, after)
        self.assertIn("共 1 项", summary)
        self.assertNotIn("移动节点", summary)

    def test_nested_conditions_preserve_and_or_meaning(self):
        before = self.canvas()
        after = copy.deepcopy(before)
        after["actionData"][0]["data"][2]["elementKey"] = "IfCondition"
        after["actionData"][0]["data"][2]["paramsValue"] = {"inputParams": {"condition": {"Logic": "Or", "Filters": [{"target": {"label": "变更类型"}, "equalTo": "NotEqualNull"}, {"target": {"label": "是否新增"}, "equalTo": "Equal", "value": True}]}}}
        summary = canvas_change_summary(before, after)
        self.assertIn("变更类型 不为空，或者 是否新增 等于 是", summary)

    def test_nesting_and_type_changes_share_preview_counts_and_show_both_layers(self):
        from gxp_editor.component_preview import component_changes
        before = self.canvas()
        after = copy.deepcopy(before)
        after["actionData"][0]["data"][2]["depth"] = ["query"]
        items = component_changes(before, after)
        summary = canvas_change_summary(before, after, changes=items)
        self.assertIn(f"共 {len(items)} 项", summary)
        self.assertIn("修改前：分组顶层", summary)
        self.assertIn("修改后：获取页面参数", summary)
        after["actionData"][0]["data"][2]["elementKey"] = "SetVariableValue"
        summary = canvas_change_summary(before, after)
        self.assertIn("元件类型：SetVariable → SetVariableValue", summary)
        self.assertIn("所在层级", summary)

    def test_removed_groups_summary_counts_match_component_preview(self):
        from gxp_editor.component_preview import component_changes
        before = self.canvas()
        after = {"actionData": [], "globalVariables": []}
        items = component_changes(before, after)
        summary = canvas_change_summary(before, after)
        self.assertIn(f"共 {len(items)} 项", summary)
        self.assertEqual(summary.count("删除节点"), 3)

    def test_complete_sql_matches_driver_escaping_without_truncation(self):
        statement = transaction()["statements"][0]
        payload = "中文 'quote' \\ path\n WHERE AND SET %s " + "长内容" * 1000
        statement["params"][0] = payload
        original = copy.deepcopy(statement)
        sql = render_full_sql(statement)
        connection = pymysql.connect(defer_connect=True, charset="utf8mb4")
        connection.server_status = SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT
        literal = connection.literal(payload)
        self.assertIn(literal, sql)
        self.assertIn("WHERE id='design-1'\n  AND is_publish=0\n  AND isDeleted=0", sql)
        self.assertIn("lastModificationTime='2026-07-26 08:00:00.123'", sql)
        self.assertEqual(statement, original)
        self.assertIn(payload, parameter_details(statement))
        self.assertTrue(sql.endswith(";"))

    def test_null_numbers_and_dates_are_sql_literals(self):
        statement = {"sql": "INSERT INTO t VALUES (%s,%s,%s,%s)", "params": [None, True, 7, datetime(2026, 10, 10, 14, 0)]}
        self.assertEqual(render_full_sql(statement), "INSERT INTO t\nVALUES (NULL,1,7,'2026-10-10 14:00:00');")

    def test_mismatched_parameters_fail_instead_of_showing_partial_sql(self):
        with self.assertRaisesRegex(ValueError, "数量"):
            render_full_sql({"sql": "UPDATE t SET a=%s WHERE id=%s", "params": [1]})

    def test_scope_makes_single_draft_restriction_visible(self):
        scope = statement_scope(transaction()["statements"][0])
        for value in ("design-1", "1 行", "is_publish = 0", "isDeleted = 0", "2026-07-26 08:00:00.123"):
            self.assertIn(value, scope)

    def test_insert_and_delete_relation_scope_show_ownership(self):
        from tests.helpers import relation
        insert = transaction(desired_relations={"node-2": "{}"})["statements"][1]
        delete = transaction(current_relations=[relation()])["statements"][1]
        self.assertIn("所属草稿 ID：design-1", statement_scope(insert))
        self.assertIn("关联记录 ID：relation-1", statement_scope(delete))

    def test_saved_result_explains_publication_in_plain_language(self):
        message = execution_result_message({"state": "saved", "runtime_finalized": False})
        self.assertIn("草稿已保存成功", message)
        self.assertIn("手动发布", message)
        self.assertIn("尚未生效", message)


class ApprovalWindowTests(unittest.TestCase):
    def test_raw_details_include_structural_changes_and_removed_group_nodes(self):
        spec = importlib.util.spec_from_file_location("approval_raw_test", PLUGIN_ROOT / "scripts" / "approval_app.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        window = app.ApprovalWindow.__new__(app.ApprovalWindow)
        before = ApprovalDisplayTests().canvas()
        after = copy.deepcopy(before)
        after["actionData"][0]["data"].reverse()
        import json
        window.session = {"baseline": {"data": json.dumps(before)}}
        window.artifact = {"desired_design": {"data": json.dumps(after)}}
        self.assertIn("移动", window.change_details())
        window.artifact["desired_design"]["data"] = json.dumps({"actionData": [], "globalVariables": []})
        details = window.change_details()
        self.assertEqual(details.count("删除 ·"), 4)
        self.assertIn("节点：assign", details)

    def test_preview_parameter_titles_are_accepted_only_from_bound_registry(self):
        from gxp_editor.compiler import CompilerBridge
        from gxp_editor.hashing import sha256_text
        spec = importlib.util.spec_from_file_location("approval_registry_test", PLUGIN_ROOT / "scripts" / "approval_app.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        registry = {"command": "describe_registry", "elements": [{"key": "SetVariable", "input_params": [{"key": "variableValue", "title": "变量值"}]}],
                    "rule_source_sha256": "c" * 64, "frontend_version": "test"}
        with tempfile.TemporaryDirectory() as directory:
            bridge_path = Path(directory) / "bridge.js"
            bridge_path.write_text("// bound registry", encoding="utf-8")
            window = app.ApprovalWindow.__new__(app.ApprovalWindow)
            window.config = replace(editor_config(), frontend_repo=directory)
            binding = {"bridge_sha256": sha256_text(bridge_path.read_text(encoding="utf-8")),
                       "registry_hash": CompilerBridge._stable_registry_hash(registry),
                       "rule_source_sha256": "c" * 64, "frontend_version": "test"}
            window.artifact = {"compiler_evidence": {"compiler_binding": binding}}
            with patch.object(CompilerBridge, "_call", return_value=registry) as call:
                metadata, _ = window.load_preview_registry()
                self.assertEqual(metadata["SetVariable"]["input_params"][0]["title"], "变量值")
                self.assertEqual(call.call_args.kwargs["timeout_seconds"], 8)
                binding["registry_hash"] = "d" * 64
                self.assertEqual(window.load_preview_registry()[0], {})
                binding["bridge_sha256"] = "e" * 64
                call.reset_mock()
                self.assertEqual(window.load_preview_registry()[0], {})
                call.assert_not_called()

    def test_unsafe_artifact_shows_blocking_alert_without_opening_approval(self):
        from gxp_editor.approval import statement_hash, transaction_binding_hash
        spec = importlib.util.spec_from_file_location("approval_app_block_test", PLUGIN_ROOT / "scripts" / "approval_app.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        artifact = transaction()
        artifact["statements"][0]["expected_rows"] = 2
        artifact["statements"][0]["statement_hash"] = statement_hash(artifact["statements"][0])
        artifact["transaction_hash"] = transaction_binding_hash(artifact)
        state = Mock()
        state.get_transaction.return_value = {"artifact_path": "unused.json", "state": "awaiting_approval"}
        with patch.object(app, "load_config"), patch.object(app, "StateStore", return_value=state), \
             patch.object(app, "load_artifact", return_value=artifact), patch.object(app.sys, "argv", ["approval_app.py", "transaction-1"]), \
             patch.object(app.messagebox, "showerror") as alert, patch.object(app, "ApprovalShell") as root:
            self.assertEqual(app.main(), 1)
            self.assertIn("只能影响 1 行", alert.call_args.args[1])
            root.assert_not_called()

    def test_readonly_tabs_buttons_and_copy_without_database_or_approval(self):
        spec = importlib.util.spec_from_file_location("approval_app_test", PLUGIN_ROOT / "scripts" / "approval_app.py")
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        artifact = transaction()
        original = copy.deepcopy(artifact)
        state = Mock()
        state.get_session.return_value = {"action_name": "测试动作", "baseline": baseline(), "transaction_id": "transaction-1", "state": "awaiting_approval"}
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(app, "load_config", return_value=editor_config()), patch.object(app, "StateStore", return_value=state), \
             patch.object(app, "load_artifact", return_value=artifact), patch.object(app, "approve_statement") as approve, \
             patch.object(app, "execute_approved_transaction") as execute:
            state.get_transaction.return_value = {"artifact_path": str(Path(folder) / "approval.json"), "state": "awaiting_approval"}
            window = app.ApprovalWindow("transaction-1")
            try:
                self.assertEqual(window.notebook.count(), 5)
                self.assertEqual(window.notebook.tabText(0), "业务逻辑验收")
                self.assertEqual(window.notebook.tabText(window.notebook.currentIndex()), "业务逻辑验收")
                self.assertTrue(window.text.isReadOnly())
                self.assertEqual(window.text.toPlainText(), render_full_sql(artifact["statements"][0]))
                self.assertFalse(window.execute_button.isEnabled())
                for width, height in ((850, 620), (1240, 880)):
                    window.root.resize(width, height)
                    window.root.show()
                    window.qt_app.processEvents()
                    for button in (window.approve_button, window.execute_button):
                        point = button.mapTo(window.root, button.rect().topLeft())
                        self.assertTrue(button.isVisible())
                        self.assertGreaterEqual(point.y(), 0)
                        self.assertLessEqual(point.y() + button.height(), window.root.height())
                window.root.hide()
                self.assertIn("design-1", window.summary.toPlainText())
                window.copy_sql()
                self.assertEqual(app.QApplication.clipboard().text(), render_full_sql(artifact["statements"][0]))
                window.wrap_sql.setChecked(True)
                self.assertEqual(window.text.lineWrapMode(), app.QPlainTextEdit.LineWrapMode.WidgetWidth)
                self.assertEqual(artifact, original)
                with patch.object(app, "component_changes", wraps=app.component_changes) as model, patch.object(window.component_review, "render") as preview:
                    window.render()
                    model.assert_not_called()
                    preview.assert_not_called()
                approve.assert_not_called()
                execute.assert_not_called()
                from gxp_editor.approval import approve_statement
                statement = artifact["statements"][0]
                with patch("gxp_editor.approval.approval_secret", return_value="test-secret"):
                    approve_statement(window.config, artifact, statement["id"])
                    window.render()
                    self.assertFalse(window.approve_button.isEnabled())
                    self.assertTrue(window.execute_button.isEnabled())
                    artifact["approvals"][statement["id"]]["expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat()
                    window.refresh_approval_state()
                    self.assertTrue(window.approve_button.isEnabled())
                    self.assertFalse(window.execute_button.isEnabled())
            finally:
                window.close()



if __name__ == "__main__":
    unittest.main()
