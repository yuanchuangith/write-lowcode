from __future__ import annotations

import copy
import tkinter as tk
from tkinter import ttk
import unittest
from unittest.mock import patch

from tests.helpers import PLUGIN_ROOT
from gxp_editor.code_review_ui import CodeReview, aligned_code_rows
from gxp_editor.generated_code_review import canvas_context


class CodeReviewTests(unittest.TestCase):
    def generated_context(self):
        nodes = [{"key": "readonly", "elementKey": "SetFormReadOnly", "paramsValue": {}},
                 {"key": "query", "elementKey": "SelectModelData", "paramsValue": {}},
                 {"key": "call", "elementKey": "CallAction", "paramsValue": {"inputParams": {"actionName": {"value": "ChildAction"}}}}]
        data = {"actionData": [{"key": "main", "data": nodes}]}
        return data, canvas_context(data, data)

    def test_known_temporary_names_are_marked_without_changing_displayed_code(self):
        _, context = self.generated_context()
        samples = [
            "var setFormReadOnlyTableCom_AAAAAAAA = inbiz.getFields().filter(t=>t.componentType=='FormTables');\nsetFormReadOnlyTableCom_AAAAAAAA?.forEach(item=>run(item));",
            "var whereAAAAAAAA = {};\nvar resultAAAAAAAA = await relatedAttributes.getSelectModelData('model', 1, 20, whereAAAAAAAA);\nvar data = resultAAAAAAAA.Rows;",
            "var ChildAction_AAAAAAAA_Output = await self.ChildAction();\nvar data = ChildAction_AAAAAAAA_Output['Rows'];",
            "var ChildAction_AAAAAAAA_Output = await this.ChildAction();\nvar data = ChildAction_AAAAAAAA_Output[\"Rows\"];",
            "var ChildAction_AAAAAAAA_Output = await relatedAttributes.CallCsharpAction(inbiz,'ref','ChildAction',self.globalVariables, []);",
        ]
        for before in samples:
            after = before.replace("AAAAAAAA", "BBBBBBBB")
            with self.subTest(before=before):
                rows, changes = aligned_code_rows(before, after, context=context)
                self.assertTrue(changes)
                self.assertTrue(all(row["generated_only"] for row in rows if row["kind"] != "equal"))
                self.assertEqual("\n".join(row["before"] for row in rows if row["before_number"]), before)
                self.assertEqual("\n".join(row["after"] for row in rows if row["after_number"]), after)

    def test_business_edits_literals_unknown_names_and_inconsistent_renames_are_never_marked(self):
        canvas, context = self.generated_context()
        before = "var ChildAction_AAAAAAAA_Output = await self.ChildAction();\nvar data = ChildAction_AAAAAAAA_Output['Rows'];"
        cases = [
            (before, before.replace("AAAAAAAA", "BBBBBBBB").replace("'Rows'", "'Total'"), context),
            (before, before.replace("AAAAAAAA", "BBBBBBBB", 1), context),
            (before + "\nvar s = 'ChildAction_AAAAAAAA_Output';", before.replace("AAAAAAAA", "BBBBBBBB") + "\nvar s = 'ChildAction_BBBBBBBB_Output';", context),
            (before, before.replace("AAAAAAAA", "BBBBBBBB"), None),
            ("var customAAAAAAAA = 1;", "var customBBBBBBBB = 1;", context),
            (before + "\nobj.ChildAction_AAAAAAAA_Output;", before.replace("AAAAAAAA", "BBBBBBBB") + "\nobj.ChildAction_BBBBBBBB_Output;", context),
        ]
        after_canvas = copy.deepcopy(canvas)
        after_canvas["actionData"][0]["data"][2]["paramsValue"]["inputParams"]["actionName"]["value"] = "OtherAction"
        cases.append((before, before.replace("AAAAAAAA", "BBBBBBBB"), canvas_context(canvas, after_canvas)))
        for old, new, enabled in cases:
            with self.subTest(old=old, new=new):
                rows, _ = aligned_code_rows(old, new, context=enabled)
                self.assertFalse(any(row["generated_only"] for row in rows))

    def test_insert_and_delete_align_real_line_numbers_without_losing_code(self):
        before = "a\nb\nremoved\nz"
        after = "a\nadded\nb\nz"
        rows, changes = aligned_code_rows(before, after)
        self.assertEqual(len(changes), 2)
        self.assertEqual([row["before"] for row in rows if row["before_number"] is not None], before.splitlines())
        self.assertEqual([row["after"] for row in rows if row["after_number"] is not None], after.splitlines())
        inserted = next(row for row in rows if row["after"] == "added")
        self.assertIsNone(inserted["before_number"])
        self.assertEqual(inserted["after_number"], 2)
        removed = next(row for row in rows if row["before"] == "removed")
        self.assertIsNone(removed["after_number"])
        self.assertEqual(removed["before_number"], 3)

    def test_expression_replacement_is_visible_and_unchanged_code_has_no_diff(self):
        before = "inbiz('RevisedType').value = revise;"
        after = "inbiz('RevisedType').value = isNew ? revise : current;"
        rows, changes = aligned_code_rows(before, after)
        self.assertEqual(changes, [1])
        self.assertEqual(rows[0]["kind"], "replace")
        self.assertEqual(rows[0]["before"], before)
        self.assertEqual(rows[0]["after"], after)
        self.assertEqual(aligned_code_rows(before, before)[1], [])
        self.assertEqual(aligned_code_rows("", ""), ([], []))
        self.assertTrue(aligned_code_rows(before + "\r\n", before + "\n")[1])

    def test_readonly_switch_jump_and_copy_use_exact_saved_draft_and_candidate(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        root.withdraw()
        view = CodeReview(ttk.Notebook(root))
        before = {"js_code": "//中文\r\nold();\r\nsame();\r\nlast();\r\n", "csharp_code": "return 1;\n"}
        after = {"js_code": "//中文\r\nnew();\r\nsame();\r\nend();\r\n", "csharp_code": "return 2;\n"}
        original = copy.deepcopy((before, after))
        view.update_sources(before, after)
        root.update_idletasks()
        self.assertEqual(len(view.changes), 2)
        self.assertTrue(all(text.cget("state") == "disabled" for text in view.texts.values()))
        self.assertTrue(view.texts["before"].tag_ranges("changed"))
        self.assertTrue(view.texts["after"].tag_ranges("changed"))
        view.navigate(1)
        self.assertEqual(view.index, 1)
        view.copy("before")
        self.assertEqual(root.clipboard_get(), before["js_code"])
        view.copy("after")
        self.assertEqual(root.clipboard_get(), after["js_code"])
        view.language.set("csharp_code")
        view.render()
        self.assertIn("return 1;", view.texts["before"].get("1.0", "end-1c"))
        self.assertIn("return 2;", view.texts["after"].get("1.0", "end-1c"))
        self.assertEqual((before, after), original)
        with patch("gxp_editor.code_review_ui.aligned_code_rows", wraps=aligned_code_rows) as diff:
            view.language.set("js_code")
            view.render()
            view.language.set("csharp_code")
            view.render()
            diff.assert_not_called()
        view.update_sources({"csharp_code": "return 2;\r\n"}, {"csharp_code": "return 2;\n"})
        self.assertIn("换行符", view.status.cget("text"))

    def test_temporary_diff_color_status_and_copy_preserve_the_exact_code(self):
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(str(exc))
        self.addCleanup(root.destroy)
        root.withdraw()
        view = CodeReview(ttk.Notebook(root))
        canvas, _ = self.generated_context()
        view.set_canvas_context(canvas, canvas)
        old = "var ChildAction_AAAAAAAA_Output = await self.ChildAction();\r\n"
        new = old.replace("AAAAAAAA", "BBBBBBBB")
        view.update_sources({"js_code": old}, {"js_code": new})
        self.assertEqual(view.generated_blocks, 1)
        self.assertTrue(view.texts["after"].tag_ranges("generated"))
        self.assertFalse(view.texts["after"].tag_ranges("changed"))
        self.assertIn("1 块仅临时变量名不同", view.status.cget("text"))
        view.copy("after")
        self.assertEqual(root.clipboard_get(), new)


if __name__ == "__main__":
    unittest.main()
