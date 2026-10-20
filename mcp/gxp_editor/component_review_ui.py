"""Native, read-only component cards and configuration details for approval."""
from __future__ import annotations

from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from typing import Any

from .component_preview import component_changes, parameter_rows, plain_description
from .canvas_review_ui import CanvasReview


class ComponentReview:
    def __init__(self, notebook: ttk.Notebook, registry: dict[str, Any], registry_note: str):
        self.registry = registry
        self.items: list[dict[str, Any]] = []
        self._rendered = False
        self._shown_item = None
        self._context_item = None
        self._canvas_sources = None
        self._select_from_canvas = False
        self.frame = ttk.Frame(notebook, padding=8)
        notebook.add(self.frame, text="元件预览")
        ttk.Label(self.frame, text="点击图标或元件联动选中前后画布；双击查看参数差异。分支可折叠，变更处带颜色标记。", wraplength=1080).pack(fill="x", pady=(0, 4))
        ttk.Label(self.frame, text=registry_note, foreground="#64748b", wraplength=1080).pack(fill="x", pady=(0, 6))
        self.views = ttk.Notebook(self.frame)
        self.views.pack(fill="both", expand=True)
        self.canvas_review = CanvasReview(self.views, self.select_canvas_node, self.open_details)
        self.views.add(self.canvas_review.frame, text="画布对比")
        list_frame = ttk.Frame(self.views)
        self.views.add(list_frame, text="变更列表与配置")
        panes = ttk.Panedwindow(list_frame, orient="horizontal")
        panes.pack(fill="both", expand=True)
        listing = ttk.Frame(panes, width=285)
        panes.add(listing, weight=1)
        self.tree = ttk.Treeview(listing, columns=("kind",), selectmode="browse", show="tree headings")
        self.tree.heading("#0", text="变更元件（点击展开）")
        self.tree.heading("kind", text="操作")
        self.tree.column("#0", width=240, minwidth=140)
        self.tree.column("kind", width=72, minwidth=60, stretch=False)
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("新增", foreground="#166534")
        self.tree.tag_configure("删除", foreground="#b91c1c")
        self.tree.tag_configure("修改", foreground="#92400e")
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.tree.bind("<Double-1>", self.open_details)
        details = ttk.Frame(panes)
        panes.add(details, weight=3)
        ttk.Button(details, text="打开元件配置…", command=self.open_details).pack(anchor="e", pady=(0, 4))
        self.detail_body = self.scroll_body(details)

    @staticmethod
    def scroll_body(parent):
        canvas = tk.Canvas(parent, highlightthickness=0, background="#f8fafc")
        scroll = ttk.Scrollbar(parent, command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        body = ttk.Frame(canvas, padding=8)
        window = canvas.create_window(0, 0, anchor="nw", window=body)
        body.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        canvas.bind("<MouseWheel>", lambda event: canvas.yview_scroll(-int(event.delta / 120), "units"))
        return body

    @staticmethod
    def identity(item):
        side = item["after"] or item["before"]
        return side.get("group_key", "globalVariables"), side["node"].get("key", "globalVariables")

    def render(self, before: dict[str, Any], after: dict[str, Any], *, changes=None):
        items = component_changes(before, after) if changes is None else changes
        if self._rendered and self.items == items and self._canvas_sources == (before, after):
            return
        selected = self.selected()
        selected_key = self.identity(selected) if selected else None
        selection = self.tree.selection()
        suffix = selection[0].partition(":")[2] if selection else ""
        open_keys = {self.identity(self.items[int(iid)]) for iid in self.tree.get_children() if self.tree.item(iid, "open")}
        scroll = self.tree.yview()[0]
        self.items = deepcopy(items)
        self._context_item = None
        self._canvas_sources = deepcopy((before, after))
        self.canvas_review.set_data(before, after, self.items)
        self._rendered = True
        self.tree.delete(*self.tree.get_children())
        for index, item in enumerate(self.items):
            side = item["after"] or item["before"]
            node = side["node"]
            title = node.get("title") or node["elementKey"]
            position = f" · 第 {side['line']} 行" if side["line"] else ""
            iid = str(index)
            self.tree.insert("", "end", iid=iid, text=title, values=(item["kind"],), tags=(item["kind"],))
            self.tree.insert(iid, "end", iid=f"{index}:location", text=side["group"] + position)
            self.tree.insert(iid, "end", iid=f"{index}:description", text=plain_description(node) or "点击查看输入、输出参数")
            self.tree.item(iid, open=self.identity(item) in open_keys)
        if self.items:
            index = next((i for i, item in enumerate(self.items) if self.identity(item) == selected_key), 0)
            iid = str(index)
            self.tree.selection_set(iid + (":" + suffix if suffix and selected_key == self.identity(self.items[index]) else ""))
            if selected_key is None or selected_key != self.identity(self.items[index]):
                self.tree.item(iid, open=True)
            self.tree.yview_moveto(scroll)
            self.select()
        else:
            self._shown_item = None
            self.canvas_review.selected_key = None
            self.canvas_review.details_button.configure(state="disabled")
            self.canvas_review.selection_label.configure(text="本次没有元件参数或位置变更；可点击画布查看上下文配置。")
            for child in self.detail_body.winfo_children():
                child.destroy()
            ttk.Label(self.detail_body, text="本次没有元件参数或位置变更。请结合“本次改动”和完整 SQL 核对。", wraplength=600).pack(anchor="w")

    def selected(self):
        if self._context_item is not None:
            return self._context_item
        selection = self.tree.selection()
        return self.items[int(selection[0].split(":")[0])] if selection else None

    def select_canvas_node(self, group_key, node_key):
        self._context_item = None
        index = next((i for i, item in enumerate(self.items) if self.identity(item) == (group_key, node_key)), None)
        if index is not None:
            self.tree.selection_set(str(index))
        else:
            # Unchanged surrounding nodes are inspectable too, using actual sides.
            def side(data):
                for group in data.get("actionData", []):
                    if group["key"] != group_key:
                        continue
                    nodes = group.get("data", [])
                    for row, node in enumerate(nodes):
                        if node["key"] != node_key:
                            continue
                        neighbor = lambda i: {"line": i + 1, "name": nodes[i].get("title") or nodes[i]["elementKey"]} if 0 <= i < len(nodes) else None
                        names = {item["key"]: item.get("title") or item["elementKey"] for item in nodes}
                        return {"group": group.get("title") or group_key, "group_key": group_key, "line": row + 1,
                                "node": node, "parents": [names.get(key, key) for key in node.get("depth") or []],
                                "previous": neighbor(row - 1), "next": neighbor(row + 1)}
                return None
            old, new = (side(data) for data in self._canvas_sources)
            if old is None and new is None:
                return
            self._context_item = self.canvas_review.impacts.get((group_key, node_key)) or {"kind": "未修改", "before": old, "after": new}
        self._select_from_canvas = True
        try:
            self.select()
        finally:
            self._select_from_canvas = False

    def select(self, event=None):
        if event is not None:
            self._context_item = None
        item = self.selected()
        if item:
            group_key, node_key = self.identity(item)
            self.canvas_review.highlight(group_key, node_key, reveal=not self._select_from_canvas)
            side = item["after"] or item["before"]
            node = side["node"]
            line = f" · 第 {side['line']} 行" if side["line"] else ""
            self.canvas_review.selection_label.configure(text=f"{side['group']}{line} · {node.get('title') or node['elementKey']} · {item['kind']}")
            self.canvas_review.details_button.configure(state="normal")
            if event is not None:
                self.tree.item(self.tree.selection()[0].split(":")[0], open=True)
            if self._shown_item == item:
                return
            same_node = self._shown_item is not None and self.identity(self._shown_item) == self.identity(item)
            canvas = self.detail_body.master
            scroll = canvas.yview()[0] if same_node else 0
            self.show(item, self.detail_body)
            self._shown_item = deepcopy(item)
            canvas.update_idletasks()
            canvas.yview_moveto(scroll)

    def open_details(self, event=None):
        item = self.selected()
        if not item:
            return None
        side = item["after"] or item["before"]
        window = tk.Toplevel(self.frame)
        window.title("元件配置 · " + (side["node"].get("title") or side["node"]["elementKey"]))
        window.geometry("1040x760")
        window.minsize(750, 500)
        window.transient(self.frame.winfo_toplevel())
        ttk.Label(window, text="审批配置只读 · 对照修改前后 · 此窗口不会修改或保存元件", padding=10).pack(fill="x")
        bottom = ttk.Frame(window, padding=8)
        bottom.pack(side="bottom", fill="x")
        ttk.Button(bottom, text="关闭详情", command=window.destroy).pack(side="right")
        self.show(item, self.scroll_body(window))
        return window

    @staticmethod
    def value_box(parent, value: str, changed: bool):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(0, 7))
        text = tk.Text(frame, wrap="word", width=1, height=min(7, max(2, len(value.splitlines()) + (len(value) // 65))),
                       font=("Microsoft YaHei UI", 9), padx=7, pady=5, relief="solid", borderwidth=1,
                       background="#fef3c7" if changed else "#ffffff")
        scroll = ttk.Scrollbar(frame, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="x", expand=True)
        text.insert("1.0", value)
        text.configure(state="disabled")
        text.bind("<Control-a>", lambda event: (text.tag_add("sel", "1.0", "end-1c"), "break")[-1])
        return text

    def show(self, item, parent):
        for child in parent.winfo_children():
            child.destroy()
        rows = parameter_rows(item, self.registry)
        parent.columnconfigure(0, weight=1, uniform="side")
        parent.columnconfigure(1, weight=1, uniform="side")
        for column, key, heading, color in ((0, "before", "修改前", "#9a3412"), (1, "after", "修改后", "#166534")):
            panel = ttk.LabelFrame(parent, text=heading, padding=10)
            panel.grid(row=0, column=column, sticky="nsew", padx=4)
            side = item[key]
            if side is None:
                ttk.Label(panel, text="尚未存在（新增元件）" if key == "before" else "将删除此元件", foreground=color).pack(anchor="w")
                continue
            node = side["node"]
            position = f" → 第 {side['line']} 行" if side["line"] else ""
            ttk.Label(panel, text=side["group"] + position, foreground="#64748b").pack(anchor="w")
            ttk.Label(panel, text=node.get("title") or node["elementKey"], font=("Microsoft YaHei UI", 12, "bold"), foreground=color).pack(anchor="w", pady=5)
            ttk.Label(panel, text=f"元件类型：{node['elementKey']}").pack(anchor="w")
            ttk.Label(panel, text="所在层级：" + (" → ".join(side.get("parents") or []) or "分组顶层")).pack(anchor="w")
            self.value_box(panel, plain_description(node) or "暂无元件简述", False)
            for direction, title in (("previous", "上一元件"), ("next", "下一元件")):
                neighbor = side[direction]
                ttk.Label(panel, text=f"{title}：" + (f"第 {neighbor['line']} 行 · {neighbor['name']}" if neighbor else "无（分组边界）")).pack(anchor="w", pady=2)
            for section, title in (("inputParams", "输入参数"), ("outputParams", "输出参数")):
                ttk.Separator(panel).pack(fill="x", pady=(12, 5))
                ttk.Label(panel, text=title, font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(0, 5))
                section_rows = [row for row in rows if row["section"] == section]
                if not section_rows:
                    ttk.Label(panel, text="无配置参数", foreground="#64748b").pack(anchor="w")
                for row in section_rows:
                    field_title = row[f"{key}_title"]
                    ttk.Label(panel, text=("* " if row["required"] else "") + field_title + (" · 已变更" if row["changed"] else ""),
                              foreground="#92400e" if row["changed"] else "#334155").pack(anchor="w", pady=(2, 4))
                    self.value_box(panel, row[key], row["changed"])
