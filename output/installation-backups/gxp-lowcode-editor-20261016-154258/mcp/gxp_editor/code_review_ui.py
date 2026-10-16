"""Read-only, aligned comparison of saved draft code and candidate code."""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from .generated_code_review import canvas_context
from .code_review_model import aligned_code_rows


class CodeReview:
    def __init__(self, notebook):
        self.frame = ttk.Frame(notebook, padding=8)
        notebook.add(self.frame, text="代码对比")
        ttk.Label(self.frame, text="修改前：本次编辑前备份的草稿代码。修改后：待保存代码。这里比较整个动作；单个元件的参数和表达式见“元件预览”。", wraplength=1100).pack(fill="x", pady=(0, 6))
        self.canvas_note = ttk.Label(self.frame, text="先核对元件参数，再核对完整代码。蓝色差异仅表示识别到临时变量一致改名，仍需审阅。", wraplength=1100)
        self.canvas_note.pack(fill="x", pady=(0, 4))
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x", pady=4)
        self.language = tk.StringVar(self.frame, value="js_code")
        for title, value in (("JavaScript", "js_code"), ("C#", "csharp_code")):
            ttk.Radiobutton(toolbar, text=title, variable=self.language, value=value, command=self.render).pack(side="left", padx=4)
        self.previous = ttk.Button(toolbar, text="上一处差异", command=lambda: self.navigate(-1))
        self.previous.pack(side="left", padx=(16, 4))
        self.next = ttk.Button(toolbar, text="下一处差异", command=lambda: self.navigate(1))
        self.next.pack(side="left", padx=4)
        self.status = ttk.Label(toolbar)
        self.status.pack(side="left", padx=8)
        ttk.Button(toolbar, text="复制修改后完整代码", command=lambda: self.copy("after")).pack(side="right")
        ttk.Button(toolbar, text="复制修改前完整代码", command=lambda: self.copy("before")).pack(side="right", padx=4)
        body = ttk.Frame(self.frame)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        for column in (0, 1):
            body.columnconfigure(column, weight=1, uniform="code")
        self.texts = {}
        self.scroll = ttk.Scrollbar(body, command=self.scroll_both)
        self.scroll.grid(row=0, column=2, sticky="ns")
        for column, key, title, color in ((0, "before", "修改前 · 原草稿", "#fee2e2"), (1, "after", "修改后 · 待保存", "#dcfce7")):
            panel = ttk.LabelFrame(body, text=title, padding=4)
            panel.grid(row=0, column=column, sticky="nsew", padx=3)
            text = tk.Text(panel, width=1, wrap="none", font=("Consolas", 10), padx=5, pady=5, undo=False)
            horizontal = ttk.Scrollbar(panel, orient="horizontal", command=text.xview)
            text.configure(xscrollcommand=horizontal.set, yscrollcommand=lambda first, last, side=key: self.sync_scroll(side, first, last))
            horizontal.pack(side="bottom", fill="x")
            text.pack(fill="both", expand=True)
            text.tag_configure("changed", background=color)
            text.tag_configure("generated", background="#e0f2fe", foreground="#075985")
            text.tag_configure("empty", foreground="#94a3b8")
            text.configure(state="disabled")
            text.bind("<Control-a>", lambda event, box=text: (box.tag_add("sel", "1.0", "end-1c"), "break")[-1])
            self.texts[key] = text
        self.sources = {"before": {}, "after": {}}
        self.context = None
        self._context_changed = False
        self._diff_cache = {}
        self.generated_blocks = 0
        self.changes = []
        self.index = 0

    def set_canvas_context(self, before, after, *, changes=None):
        from .component_preview import component_changes
        items = component_changes(before, after) if changes is None else changes
        counts = {kind: sum(item["kind"] == kind for item in items) for kind in ("新增", "修改", "移动", "修改并移动", "删除")}
        summary = "、".join(f"{kind} {count} 项" for kind, count in counts.items() if count) or "无元件参数或位置变化"
        self.canvas_note.configure(text=f"画布：{summary}。蓝色差异仅表示识别到临时变量一致改名，完整代码保留；此标注不代表语义验证。")
        context = canvas_context(before, after)
        if self.context != context:
            self.context = context
            self._context_changed = True
            self._diff_cache.clear()

    def update_sources(self, before, after):
        sources = {"before": {field: before.get(field) or "" for field in ("js_code", "csharp_code")},
                   "after": {field: after.get(field) or "" for field in ("js_code", "csharp_code")}}
        if self.sources != sources or self._context_changed:
            self.sources = sources
            self._context_changed = False
            self._diff_cache.clear()
            if not sources["before"]["js_code"] and not sources["after"]["js_code"]:
                self.language.set("csharp_code")
            self.render()

    def render(self):
        field = self.language.get()
        before, after = self.sources["before"].get(field, ""), self.sources["after"].get(field, "")
        self.format_only = before != after and before.splitlines() == after.splitlines()
        if field not in self._diff_cache:
            self._diff_cache[field] = aligned_code_rows(before, after, context=self.context)
        rows, self.changes = self._diff_cache[field]
        self.generated_blocks = sum(rows[line - 1]["generated_only"] for line in self.changes if line <= len(rows))
        self.index = 0
        for key, text in self.texts.items():
            text.configure(state="normal")
            text.delete("1.0", "end")
            for line, row in enumerate(rows, 1):
                number = row[f"{key}_number"]
                prefix = f"{number:5} │ " if number is not None else "      │ "
                text.insert("end", prefix + row[key] + "\n")
                if number is None:
                    text.tag_add("empty", f"{line}.0", f"{line}.end")
                elif row["kind"] != "equal":
                    text.tag_add("generated" if row["generated_only"] else "changed", f"{line}.0", f"{line}.end")
            text.configure(state="disabled")
        self.navigate(0)

    def navigate(self, direction):
        if self.changes:
            self.index = max(0, min(len(self.changes) - 1, self.index + direction))
            for text in self.texts.values():
                text.see(f"{self.changes[self.index]}.0")
        status = "换行符或末尾换行不同（可复制完整代码核对）" if self.format_only else f"差异 {self.index + 1}/{len(self.changes)} 块" if self.changes else "代码一致"
        if self.generated_blocks:
            status += f" · {self.generated_blocks} 块仅临时变量名不同"
        self.status.configure(text=status)
        self.previous.configure(state="normal" if self.changes and self.index > 0 else "disabled")
        self.next.configure(state="normal" if self.changes and self.index + 1 < len(self.changes) else "disabled")

    def scroll_both(self, *args):
        for text in self.texts.values():
            text.yview(*args)

    def sync_scroll(self, side, first, last):
        self.scroll.set(first, last)
        peer = self.texts.get("after" if side == "before" else "before")
        if peer is not None and abs(peer.yview()[0] - float(first)) > 0.00001:
            peer.yview_moveto(first)

    def copy(self, side):
        self.frame.clipboard_clear()
        self.frame.clipboard_append(self.sources[side].get(self.language.get(), ""))
