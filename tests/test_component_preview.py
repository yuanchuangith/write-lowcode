from __future__ import annotations

import copy
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from tests import test_approval_display as display_fixtures
from gxp_editor.component_preview import component_changes, parameter_rows, plain_description
from gxp_editor.component_review_ui import ComponentReview


REGISTRY = {"SetVariable": {"input_params": [{"key": "variableValue", "title": "变量值", "required": True}],
                            "output_params": [{"key": "variableName", "title": "保存至变量"}]}}


class ComponentPreviewTests(unittest.TestCase):
    def canvases(self):
        before = display_fixtures.ApprovalDisplayTests().canvas()
        after = copy.deepcopy(before)
        value = after["actionData"][0]["data"][2]["paramsValue"]["inputParams"]["variableValue"]
        value.update(paramTypes="custom", value="isNew ? parameter : current", code="isNew ? parameter : current")
        return before, after

    def test_only_changed_component_and_true_neighbors_are_selected(self):
        before, after = self.canvases()
        original = copy.deepcopy((before, after))
        after["actionData"][0]["data"][0]["description"] = "仅重新生成说明"
        changes = component_changes(before, after)
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["kind"], "修改")
        self.assertEqual(changes[0]["after"]["line"], 3)
        self.assertEqual(changes[0]["after"]["previous"], {"line": 2, "name": "获取页面参数"})
        self.assertIsNone(changes[0]["after"]["next"])
        self.assertEqual(before, original[0])

    def test_insert_delete_removed_group_and_reorder_are_visible_without_shift_noise(self):
        before, _ = self.canvases()
        after = copy.deepcopy(before)
        node = {"key": "inserted", "elementKey": "Breakpoint", "title": "断点", "paramsValue": {}}
        after["actionData"][0]["data"].insert(1, node)
        changes = component_changes(before, after)
        self.assertEqual([item["kind"] for item in changes], ["新增"])
        self.assertEqual(changes[0]["after"]["next"]["name"], "获取页面参数")
        after = copy.deepcopy(before)
        after["actionData"][0]["data"].pop(1)
        self.assertEqual([item["kind"] for item in component_changes(before, after)], ["删除"])
        self.assertEqual(len(component_changes(before, {"actionData": [], "globalVariables": []})), 3)
        after = copy.deepcopy(before)
        after["actionData"][0]["data"] = list(reversed(after["actionData"][0]["data"]))
        self.assertEqual([item["kind"] for item in component_changes(before, after)], ["移动", "移动"])

    def test_depth_change_and_global_variables_are_not_hidden(self):
        before, _ = self.canvases()
        after = copy.deepcopy(before)
        after["actionData"][0]["data"][2]["depth"] = ["if-node"]
        after["globalVariables"] = [{"name": "flag", "value": True}]
        changes = component_changes(before, after)
        self.assertEqual(len(changes), 2)
        self.assertEqual(changes[1]["after"]["node"]["elementKey"], "globalVariables")

    def test_registry_titles_full_expressions_unknown_fields_and_highlights(self):
        before, after = self.canvases()
        expression = "含中文'引号'\\路径\n" + "表达式" * 1500
        fields = after["actionData"][0]["data"][2]["paramsValue"]["inputParams"]
        fields["variableValue"]["code"] = expression
        fields["variableValue"]["value"] = expression
        fields["dynamicExtra"] = {"nested": [1, False, None]}
        rows = parameter_rows(component_changes(before, after)[0], REGISTRY)
        value = next(row for row in rows if row["key"] == "variableValue")
        self.assertEqual(value["after_title"], "变量值")
        self.assertIn(expression, value["after"])
        self.assertTrue(value["changed"])
        self.assertFalse(next(row for row in rows if row["key"] == "variableName")["changed"])
        self.assertEqual(next(row for row in rows if row["key"] == "dynamicExtra")["after_title"], "dynamicExtra")
        self.assertEqual(plain_description({"description": '<span>定义</span><b>变量 &amp; 值</b>'}), "定义变量 & 值")

    def test_click_and_detail_window_are_readonly_and_preserve_data(self):
        before, after = self.canvases()
        original = copy.deepcopy((before, after))
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        root.withdraw()
        notebook = ttk.Notebook(root)
        view = ComponentReview(notebook, REGISTRY, "测试元件注册表")
        view.render(before, after)
        root.update_idletasks()
        self.assertEqual(view.tree.item("0", "values"), ("修改",))
        view.tree.selection_set("0:location")
        view.select()
        self.assertTrue(view.tree.item("0", "open"))
        window = view.open_details()
        self.addCleanup(window.destroy)
        root.update_idletasks()

        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        texts = [widget for widget in descendants(window) if isinstance(widget, tk.Text)]
        self.assertTrue(texts)
        self.assertTrue(all(widget.cget("state") == "disabled" for widget in texts))
        self.assertTrue(any("完整表达式：isNew ? parameter : current" in widget.get("1.0", "end-1c") for widget in texts))
        self.assertEqual((before, after), original)

    def test_refresh_keeps_selection_expansion_scroll_and_uses_stable_node_identity(self):
        before, after = self.canvases()
        after["actionData"][0]["data"][1]["paramsValue"]["inputParams"]["paramName"] = "new_parameter"
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        root.withdraw()
        view = ComponentReview(ttk.Notebook(root), REGISTRY, "")
        view.render(before, after)
        root.update_idletasks()
        view.tree.selection_set("1:location")
        view.select()
        root.update()
        view.tree.item("0", open=False)
        view.tree.item("1", open=True)
        scroll = view.tree.yview()
        detail_scroll = view.detail_body.master.yview()
        with patch.object(view, "show", wraps=view.show) as redraw:
            view.render(copy.deepcopy(before), copy.deepcopy(after))
            redraw.assert_not_called()
        self.assertEqual(view.tree.selection(), ("1:location",))
        self.assertFalse(view.tree.item("0", "open"))
        self.assertTrue(view.tree.item("1", "open"))
        self.assertEqual(view.tree.yview(), scroll)
        self.assertEqual(view.detail_body.master.yview(), detail_scroll)
        # Another change inserted ahead of this item must not change selection.
        after["actionData"][0]["data"][0]["paramsValue"]["inputParams"]["extra"] = "changed"
        view.render(before, after)
        root.update()
        self.assertEqual(view.tree.selection(), ("2:location",))
        self.assertEqual(view.selected()["after"]["node"]["key"], "assign")
        self.assertTrue(view.tree.item("2", "open"))


if __name__ == "__main__":
    unittest.main()
