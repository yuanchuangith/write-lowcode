"""Clickable, read-only before/after action canvases using native Tk drawing."""
from __future__ import annotations

import tkinter as tk
from tkinter import font, ttk
from typing import Any, Callable

from .canvas_review_model import aligned_rows, caption_runs, review_groups
from .component_preview import control_flow_impacts
from .action_icons import native_icon


COLORS = {"新增": ("#ecfdf3", "#16834a"), "删除": ("#fff1f2", "#c43e4e"),
          "修改": ("#fff8e8", "#b77916"), "移动": ("#eff4ff", "#5965bd"),
          "修改并移动": ("#fff8e8", "#b77916"), "受条件影响": ("#f4effc", "#7656ab"), "可能受控制流影响": ("#f4effc", "#7656ab")}


def draw_icon(canvas: tk.Canvas, x: float, y: float, kind: str, tag: str) -> None:
    """Use the exact element's platform image; unknown types get a neutral cube."""
    icon = native_icon(canvas, kind)
    if icon is not None:
        canvas.create_image(x, y, image=icon, anchor="nw", tags=(tag, "icon"))
        return
    options = {"tags": (tag, "icon"), "width": 1.8}
    canvas.create_polygon(x + 12, y + 1, x + 23, y + 7, x + 23, y + 19, x + 12, y + 25,
                          x + 1, y + 19, x + 1, y + 7, fill="#f5f7fb", outline="#7b8da5", **options)
    canvas.create_line(x + 1, y + 7, x + 12, y + 13, x + 23, y + 7, fill="#7b8da5", **options)
    canvas.create_line(x + 12, y + 13, x + 12, y + 25, fill="#7b8da5", **options)


class CanvasReview:
    ROW_HEIGHT = 78

    def __init__(self, parent, on_select: Callable, on_details: Callable):
        self.frame = ttk.Frame(parent)
        self.on_select, self.on_details = on_select, on_details
        self.groups: list[dict[str, Any]] = []
        self.changes: dict[tuple[str, str], dict] = {}
        self.impacts: dict[tuple[str, str], dict] = {}
        self.collapsed: dict[str, set[str]] = {}
        self.active_group = ""
        self.selected_key = None
        self.rows: list[dict] = []
        self._pending_redraw = None
        self.body_font = font.Font(family="Microsoft YaHei UI", size=9)
        self.title_font = font.Font(family="Microsoft YaHei UI", size=10, weight="bold")
        toolbar = ttk.Frame(self.frame, padding=(4, 8))
        toolbar.pack(fill="x")
        ttk.Label(toolbar, text="动作分组").pack(side="left", padx=(0, 8))
        self.group_picker = ttk.Combobox(toolbar, state="readonly", width=25)
        self.group_picker.pack(side="left")
        self.group_picker.bind("<<ComboboxSelected>>", self.change_group)
        self.count = ttk.Label(toolbar, foreground="#64748b")
        self.count.pack(side="left", padx=12)
        ttk.Button(toolbar, text="展开全部", command=self.expand_all).pack(side="right", padx=(6, 0))
        ttk.Button(toolbar, text="下一处改动 ↓", command=lambda: self.next_change(1)).pack(side="right", padx=4)
        ttk.Button(toolbar, text="上一处改动 ↑", command=lambda: self.next_change(-1)).pack(side="right")
        headers = tk.Frame(self.frame, background="#e2e8f0")
        headers.pack(fill="x")
        for column, text, color in ((0, "修改前", "#9a4b36"), (1, "修改后", "#18714c")):
            headers.columnconfigure(column, weight=1, uniform="side")
            tk.Label(headers, text=text, anchor="w", background="#f5f7fb", foreground=color,
                     font=("Microsoft YaHei UI", 10, "bold"), padx=20, pady=10).grid(row=0, column=column, sticky="ew", padx=1)
        body = ttk.Frame(self.frame)
        body.pack(fill="both", expand=True)
        body.rowconfigure(0, weight=1)
        self.canvases = {}
        for column, side in enumerate(("before", "after")):
            body.columnconfigure(column, weight=1, uniform="side")
            panel = ttk.Frame(body)
            panel.grid(row=0, column=column, sticky="nsew", padx=(0, 1))
            panel.rowconfigure(0, weight=1)
            panel.columnconfigure(0, weight=1)
            canvas = tk.Canvas(panel, background="#ffffff", highlightthickness=0, cursor="hand2", yscrollincrement=24)
            canvas.grid(row=0, column=0, sticky="nsew")
            horizontal = ttk.Scrollbar(panel, orient="horizontal", command=canvas.xview)
            horizontal.grid(row=1, column=0, sticky="ew")
            canvas.configure(xscrollcommand=horizontal.set)
            canvas.bind("<Configure>", self.schedule_draw)
            canvas.bind("<MouseWheel>", self.wheel)
            canvas.bind("<Button-4>", lambda event: self.scroll("scroll", -3, "units"))
            canvas.bind("<Button-5>", lambda event: self.scroll("scroll", 3, "units"))
            self.canvases[side] = canvas
        self.scrollbar = ttk.Scrollbar(body, orient="vertical", command=self.scroll)
        self.scrollbar.grid(row=0, column=2, sticky="ns")
        self.canvases["before"].configure(yscrollcommand=self.scrollbar.set)
        footer = ttk.Frame(self.frame, padding=(6, 10))
        footer.pack(fill="x")
        self.selection_label = ttk.Label(footer, text="点击元件查看对应位置；双击打开参数差异。", foreground="#526279")
        self.selection_label.pack(side="left", fill="x", expand=True)
        self.details_button = ttk.Button(footer, text="查看参数差异…", command=on_details, state="disabled")
        self.details_button.pack(side="right")
        self.condition_button = ttk.Button(footer, text="定位上级条件", command=self.select_controller, state="disabled")
        self.condition_button.pack(side="right", padx=6)
        legend = ttk.Frame(self.frame, padding=(6, 0, 6, 4))
        legend.pack(fill="x")
        for label in ("新增", "删除", "修改", "移动", "受条件影响"):
            ttk.Label(legend, text="● " + label, foreground=COLORS[label][1]).pack(side="left", padx=(0, 18))
        ttk.Label(legend, text="点击三角折叠分支 · 两侧同步滚动 · 灰色空位表示此侧没有对应节点", foreground="#748196").pack(side="left")

    def set_data(self, before, after, changes):
        self.groups = review_groups(before, after)
        self.changes = {}
        self.impacts = {(item["group_key"], item["node_key"]): item
                        for item in control_flow_impacts(before, after, changes=changes)}
        for change in changes:
            side = change["after"] or change["before"]
            self.changes[(side.get("group_key", "globalVariables"), side["node"].get("key", "globalVariables"))] = change
        self.group_picker.configure(values=[f"{group['title']}  ·  {index + 1}" for index, group in enumerate(self.groups)])
        if not self.groups:
            self.active_group = ""
            self.group_picker.set("")
            self.draw()
            return
        initial = next((i for i, group in enumerate(self.groups) if group["key"] == self.active_group), None)
        if initial is None:
            initial = next((i for i, group in enumerate(self.groups) if any(key[0] == group["key"] for key in self.changes)), 0)
        self.group_picker.current(initial)
        self.active_group = self.groups[initial]["key"]
        self.draw()

    def current_group(self):
        return next((group for group in self.groups if group["key"] == self.active_group), None)

    def change_group(self, event=None):
        self.active_group = self.groups[self.group_picker.current()]["key"]
        self.draw()
        self.scroll("moveto", 0)

    def schedule_draw(self, event=None):
        if self._pending_redraw:
            self.frame.after_cancel(self._pending_redraw)
        self._pending_redraw = self.frame.after_idle(self.draw)

    def scroll(self, *args):
        for canvas in self.canvases.values():
            canvas.yview(*args)

    def wheel(self, event):
        self.scroll("scroll", -int(event.delta / 120), "units")
        return "break"

    def expand_all(self):
        self.collapsed[self.active_group] = set()
        self.draw()

    def toggle(self, key):
        collapsed = self.collapsed.setdefault(self.active_group, set())
        collapsed.symmetric_difference_update({key})
        self.draw()

    def next_change(self, direction):
        keys = list(self.changes)
        if not keys:
            return
        index = keys.index(self.selected_key) if self.selected_key in keys else (-1 if direction > 0 else 0)
        group_key, node_key = keys[(index + direction) % len(keys)]
        self.on_select(group_key, node_key)
        self.highlight(group_key, node_key)

    def highlight(self, group_key, node_key, *, reveal=True):
        key = (group_key, node_key)
        self.condition_button.configure(state="normal" if key in self.impacts else "disabled")
        if self.selected_key == key and not reveal:
            return
        self.selected_key = key
        if reveal and self.active_group != group_key:
            index = next((i for i, group in enumerate(self.groups) if group["key"] == group_key), None)
            if index is not None:
                self.group_picker.current(index)
                self.active_group = group_key
        group = self.current_group()
        if group and reveal:
            for side in ("before", "after"):
                for node in group[side]:
                    if node["key"] == node_key:
                        self.collapsed.setdefault(group_key, set()).difference_update(node.get("depth") or [])
        self.draw()
        if reveal:
            row_index = next((i for i, row in enumerate(self.rows) if any(item and item["node"]["key"] == node_key for item in row.values())), None)
            if row_index is not None:
                canvas = self.canvases["before"]
                top = row_index * self.ROW_HEIGHT + 8
                height = canvas.winfo_height()
                if height > 1:
                    if top < canvas.canvasy(0):
                        target = max(0, top - 16)
                    elif top + self.ROW_HEIGHT > canvas.canvasy(height):
                        target = max(0, top + self.ROW_HEIGHT - height + 16)
                    else:
                        return
                    self.scroll("moveto", target / max(1, len(self.rows) * self.ROW_HEIGHT + 24))

    def select_controller(self):
        impact = self.impacts.get(self.selected_key)
        if impact:
            target = impact["affected_by"][-1]
            self.on_select(target["group_key"], target["node_key"])
            self.highlight(target["group_key"], target["node_key"])

    def draw(self):
        self._pending_redraw = None
        group = self.current_group()
        self.rows = aligned_rows(group, self.collapsed.get(self.active_group, set())) if group else []
        count = sum(key[0] == self.active_group for key in self.changes)
        self.count.configure(text=f"{count} 处改动 · {len(self.rows)} 行对比")
        for side, canvas in self.canvases.items():
            canvas.delete("all")
            width = max(460, canvas.winfo_width())
            max_depth = max((len(item[side]["node"].get("depth") or []) for item in self.rows if item[side]), default=0)
            width = max(width, 350 + max_depth * 28)
            canvas.configure(scrollregion=(0, 0, width, len(self.rows) * self.ROW_HEIGHT + 24))
            if not self.rows:
                canvas.create_text(30, 40, anchor="nw", text="没有可显示的画布节点", fill="#7b8797", font=self.body_font)
            for index, row in enumerate(self.rows):
                self.draw_row(canvas, side, index, row[side], width, group)

    def draw_row(self, canvas, side, index, item, width, group):
        y = index * self.ROW_HEIGHT + 12
        if item is None:
            canvas.create_rectangle(12, y, width - 12, y + 68, fill="#f7f9fc", outline="#edf0f5", dash=(3, 3))
            canvas.create_text(width / 2, y + 34, text="—  此侧无对应节点  —", fill="#9aa6b6", font=self.body_font)
            return
        node = item["node"]
        node_key = node["key"]
        tag = f"node:{index}"
        change = self.changes.get((group["key"], node_key))
        impact = self.impacts.get((group["key"], node_key))
        kind = change["kind"] if change else impact["kind"] if impact else ""
        bg, color = COLORS.get(kind, ("#ffffff", "#8a96a6"))
        selected = self.selected_key == (group["key"], node_key)
        canvas.create_rectangle(12, y, width - 12, y + 68, fill=bg,
                                outline="#4e87ed" if selected else bg, width=1.5, tags=(tag, "node"))
        if kind:
            canvas.create_rectangle(12, y + 1, 15, y + 67, fill=color, outline=color, tags=tag)
        canvas.create_text(26, y + 19, text=f"{item['line']:02d}", anchor="w", fill="#9ba5b3", font=("Consolas", 9), tags=tag)
        depth = node.get("depth") or []
        for level, parent_key in enumerate(depth):
            gx = 72 + level * 28
            canvas.create_line(gx, y - 10, gx, y + self.ROW_HEIGHT - 10, fill="#dce1e8", tags=tag)
        x = 62 + len(depth) * 28
        if depth:
            canvas.create_line(x - 18, y + 25, x - 5, y + 25, fill="#dce1e8", tags=tag)
        draw_icon(canvas, x, y + 12, node.get("elementKey", ""), tag)
        title = node.get("title") or node.get("elementKey") or "元件"
        canvas.create_text(x + 36, y + 20, text=title, anchor="w", fill="#243246", font=self.title_font, tags=tag)
        if kind:
            badge_width = self.body_font.measure(kind) + 16
            canvas.create_rectangle(width - badge_width - 25, y + 9, width - 22, y + 31, fill=bg, outline=color, tags=tag)
            canvas.create_text(width - 23 - badge_width / 2, y + 20, text=kind, fill=color, font=self.body_font, tags=tag)
        self.draw_caption(canvas, node, x + 36, y + 43, width - x - 64, tag)
        if any(node_key in (child.get("depth") or []) for nodes in (group["before"], group["after"]) for child in nodes):
            fold_tag = f"fold:{index}"
            collapsed = node_key in self.collapsed.get(self.active_group, set())
            canvas.create_rectangle(x - 18, y + 9, x - 2, y + 34, fill=bg, outline=bg, tags=fold_tag)
            canvas.create_text(x - 10, y + 21, text="▸" if collapsed else "▾", fill="#708098", font=("Segoe UI", 12), tags=fold_tag)
            canvas.tag_bind(fold_tag, "<Button-1>", lambda event, key=node_key: self.toggle(key))
        canvas.tag_bind(tag, "<Button-1>", lambda event, key=node_key: self.on_select(group["key"], key))
        canvas.tag_bind(tag, "<Double-1>", lambda event, key=node_key: self.open_node(group["key"], key))

    def open_node(self, group_key, node_key):
        self.on_select(group_key, node_key)
        self.on_details()

    def draw_caption(self, canvas, node, x, y, available, tag):
        used = 0
        for text, accent in caption_runs(node):
            visible = ""
            for char in text:
                if self.body_font.measure(visible + char) + used > max(20, available - 12):
                    canvas.create_text(x + used, y, text=visible + "…", anchor="w", fill="#3485ff" if accent else "#8a94a3", font=self.body_font, tags=tag)
                    return
                visible += char
            canvas.create_text(x + used, y, text=visible, anchor="w", fill="#3485ff" if accent else "#8a94a3", font=self.body_font, tags=tag)
            used += self.body_font.measure(visible)
