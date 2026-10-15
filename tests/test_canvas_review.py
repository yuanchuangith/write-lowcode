from __future__ import annotations

from copy import deepcopy
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from tests import helpers  # Sets the local MCP import path; never opens a DB.
from gxp_editor.canvas_review_model import aligned_rows, caption_runs, review_groups
from gxp_editor.component_preview import component_changes
from gxp_editor.component_review_ui import ComponentReview
from scripts.preview_component_canvas import REGISTRY, sample_canvases


class CanvasModelTests(unittest.TestCase):
    def test_alignment_retains_actual_order_and_rows_for_insert_delete_and_move(self):
        before, after = sample_canvases()
        after["actionData"][0]["data"].insert(0, after["actionData"][0]["data"].pop())
        group = review_groups(before, after)[0]
        original = deepcopy(group)
        rows = aligned_rows(group, set())
        for side in ("before", "after"):
            present = [row[side] for row in rows if row[side]]
            self.assertEqual([item["node"] for item in present], group[side])
            self.assertEqual([item["line"] for item in present], list(range(1, len(group[side]) + 1)))
        self.assertTrue(any(row["before"] is None or row["after"] is None for row in rows))
        self.assertEqual(group, original)

    def test_collapsing_keeps_block_boundary_and_other_groups_and_global_changes(self):
        before, after = sample_canvases()
        after["globalVariables"] = [{"name": "flag"}]
        before["actionData"].append({"key": "removed", "title": "删除分组", "data": []})
        groups = review_groups(before, after)
        self.assertEqual([group["key"] for group in groups], ["main", "removed", "globalVariables"])
        rows = aligned_rows(groups[0], {"if-look"})
        for side in ("before", "after"):
            keys = [row[side]["node"]["key"] for row in rows if row[side]]
            self.assertIn("if-look", keys)
            self.assertIn("if-end", keys)
            self.assertNotIn("theme", keys)

    def test_captions_preserve_accent_text_without_executing_markup(self):
        runs = caption_runs({"description": '定义 <span style="color: blue">a &lt; b</span><br/> 完成'})
        self.assertIn(("a < b", True), runs)
        self.assertEqual("".join(text for text, _ in runs), "定义 a < b  完成")
        self.assertIn(("真实变量", True), caption_runs({"description": '<span class="var">真实变量</span>'}))


class CanvasInteractionTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(self.root.destroy)
        self.root.geometry("1120x780")
        self.book = ttk.Notebook(self.root)
        self.book.pack(fill="both", expand=True)
        self.view = ComponentReview(self.book, REGISTRY, "示例数据")
        self.before, self.after = sample_canvases()
        self.original = deepcopy((self.before, self.after))
        self.view.render(self.before, self.after)
        self.root.update()
        self.canvas = self.view.canvas_review

    def test_icon_click_links_both_sides_and_double_click_opens_readonly_details(self):
        canvas = self.canvas.canvases["after"]
        index = next(i for i, row in enumerate(self.canvas.rows) if row["after"] and row["after"]["node"]["key"] == "theme")
        # Click the actual icon, rather than calling the selection helper directly.
        canvas.event_generate("<Motion>", x=100, y=index * self.canvas.ROW_HEIGHT + 32)
        canvas.event_generate("<Button-1>", x=100, y=index * self.canvas.ROW_HEIGHT + 32)
        self.root.update()
        self.assertEqual(self.view.selected()["after"]["node"]["key"], "theme")
        self.assertEqual(self.canvas.selected_key, ("main", "theme"))
        self.assertEqual(str(self.canvas.details_button.cget("state")), "normal")
        for side in ("before", "after"):
            outlines = [self.canvas.canvases[side].itemcget(item, "outline") for item in self.canvas.canvases[side].find_withtag("node")]
            self.assertIn("#4e87ed", outlines)
        window = self.view.open_details()
        self.addCleanup(window.destroy)
        def texts(widget):
            for child in widget.winfo_children():
                if isinstance(child, tk.Text):
                    yield child
                yield from texts(child)
        fields = list(texts(window))
        self.assertTrue(fields)
        self.assertTrue(all(text.cget("state") == "disabled" for text in fields))
        self.assertEqual((self.before, self.after), self.original)

    def test_fold_and_navigation_reveal_real_children_and_inspect_unchanged_nodes(self):
        self.canvas.toggle("if-look")
        self.assertFalse(any(row["after"] and row["after"]["node"]["key"] == "theme" for row in self.canvas.rows))
        self.canvas.next_change(1)
        self.assertTrue(any(row["after"] and row["after"]["node"]["key"] == "readonly" for row in self.canvas.rows))
        self.view.select_canvas_node("main", "mode")
        self.assertEqual(self.view.selected()["kind"], "未修改")
        self.assertEqual(self.canvas.selected_key, ("main", "mode"))
        self.assertEqual((self.before, self.after), self.original)

    def test_real_double_click_and_scroll_dispatch(self):
        canvas = self.canvas.canvases["after"]
        # Two real pointer click sequences exercise the canvas's double binding.
        with patch.object(self.canvas, "on_details") as details:
            for time in (1000, 1100):
                canvas.event_generate("<Motion>", x=74, y=32)
                canvas.event_generate("<ButtonPress-1>", x=74, y=32, time=time)
                canvas.event_generate("<ButtonRelease-1>", x=74, y=32, time=time + 10)
                self.root.update()
            details.assert_called_once()
        self.assertEqual(self.view.selected()["after"]["node"]["key"], "bind")
        canvas.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertEqual(self.canvas.canvases["before"].yview(), canvas.yview())
        self.assertGreater(canvas.yview()[0], 0)

    def test_removed_selection_does_not_leave_a_stale_details_button(self):
        self.view.render({"actionData": []}, {"actionData": []})
        self.root.update()
        self.assertIsNone(self.view.selected())
        self.assertEqual(str(self.canvas.details_button.cget("state")), "disabled")

    def test_context_refresh_is_not_lost_when_only_unchanged_caption_changes(self):
        self.after["actionData"][0]["data"][0]["description"] = "更新后的上下文说明"
        self.view.render(self.before, self.after)
        self.root.update()
        self.assertTrue(any("更新后的上下文说明" in self.canvas.canvases["after"].itemcget(item, "text")
                            for item in self.canvas.canvases["after"].find_all()
                            if self.canvas.canvases["after"].type(item) == "text"))
        with patch.object(self.canvas, "set_data", wraps=self.canvas.set_data) as refresh:
            self.view.render(deepcopy(self.before), deepcopy(self.after))
            refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
