from __future__ import annotations

import copy
import tkinter as tk
from tkinter import ttk
import unittest

from tests.helpers import PLUGIN_ROOT
from gxp_editor.code_review_ui import CodeReview, aligned_code_rows


class CodeReviewTests(unittest.TestCase):
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
        view.update_sources({"csharp_code": "return 2;\r\n"}, {"csharp_code": "return 2;\n"})
        self.assertIn("换行符", view.status.cget("text"))


if __name__ == "__main__":
    unittest.main()
