"""Read-only viewer for an already saved local approval; never reopens execution."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tkinter as tk
from tkinter import ttk

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from gxp_editor.approval_display import canvas_change_summary, render_full_sql, statement_scope
from gxp_editor.code_review_ui import CodeReview
from gxp_editor.component_preview import component_changes
from gxp_editor.component_review_ui import ComponentReview
from gxp_editor.transaction import validate_transaction_artifact
from preview_component_canvas import capture_full_review


def load_snapshot(transaction_id, state_path):
    # URI read-only mode neither initializes nor migrates the local state store.
    with sqlite3.connect(state_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT t.id,t.session_id,t.state,t.artifact_path,s.action_name,s.action_code,s.baseline_json,s.state AS session_state "
            "FROM transactions t JOIN sessions s ON s.id=t.session_id WHERE t.id=? AND t.state='saved' AND s.state='finished'",
            (transaction_id,),
        ).fetchone()
    if row is None:
        raise ValueError("仅可展示已保存且会话已完成的历史审批。")
    artifact = json.loads(Path(row["artifact_path"]).read_text(encoding="utf-8"))
    validate_transaction_artifact(artifact)
    if artifact["transaction_id"] != row["id"] or artifact["session_id"] != row["session_id"]:
        raise ValueError("历史审批身份与本地记录不一致。")
    return dict(row), artifact, json.loads(row["baseline_json"])


def add_text_tab(notebook, title, text):
    frame = ttk.Frame(notebook)
    notebook.add(frame, text=title)
    box = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 10), padx=12, pady=10)
    scroll = ttk.Scrollbar(frame, command=box.yview)
    box.configure(yscrollcommand=scroll.set)
    scroll.pack(side="right", fill="y")
    box.pack(fill="both", expand=True)
    box.insert("1.0", text)
    box.configure(state="disabled")


class SavedApprovalPreview:
    def __init__(self, record, artifact, baseline):
        self.root = tk.Tk()
        self.root.title("GXP 真实历史审批 · 只读展示")
        self.root.geometry("1380x760+0+0")
        self.root.minsize(1000, 680)
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TLabel", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", padding=(10, 6))
        style.configure("TNotebook.Tab", padding=(14, 6))
        footer = ttk.Frame(self.root, padding=10)
        footer.pack(side="bottom", fill="x")
        ttk.Button(footer, text="历史已批准并保存", state="disabled").pack(side="left")
        ttk.Button(footer, text="保存已完成（只读）", state="disabled").pack(side="right")
        ttk.Button(footer, text="关闭展示", command=self.root.destroy).pack(side="right", padx=8)
        header = ttk.Frame(self.root, padding=(16, 12))
        header.pack(fill="x")
        ttk.Label(header, text=f"{record['action_name']}（{record['action_code']}）", font=("Microsoft YaHei UI", 16, "bold"), foreground="#253c59").pack(side="left")
        ttk.Label(header, text="真实历史审批 · 已保存 · 只读", foreground="#18714c").pack(side="right")
        scope = ttk.LabelFrame(self.root, text="当时的审批范围", padding=10)
        scope.pack(fill="x", padx=14, pady=(0, 8))
        ttk.Label(scope, text=statement_scope(artifact["statements"][0]), justify="left").pack(anchor="w")
        evidence = artifact.get("compiler_evidence") or {}
        validations = evidence.get("validation") or {}
        valid = validations.get("valid") is True
        complete = evidence.get("code_generation_complete") is True
        ttk.Label(self.root, text=f"画布编译校验：{'通过' if valid else '未通过'}   |   代码生成：{'完整' if complete else '未确认'}   |   历史语句：{len(artifact['statements'])} 条   |   批准记录：{len(artifact.get('approvals') or {})} 条",
                  foreground="#18714c").pack(fill="x", padx=16, pady=(0, 8))
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        self.review = ComponentReview(self.notebook, {}, "来源：真实草稿备份与已保存审批快照。参数配置按原始字段展示。")
        before, after = json.loads(baseline["data"]), json.loads(artifact["desired_design"]["data"])
        changes = component_changes(before, after)
        self.review.render(before, after, changes=changes)
        code = CodeReview(self.notebook)
        code.set_canvas_context(before, after, changes=changes)
        code.update_sources(baseline, artifact["desired_design"])
        add_text_tab(self.notebook, "本次改动", canvas_change_summary(before, after, changes=changes))
        add_text_tab(self.notebook, "当时的完整 SQL", "\n\n".join(render_full_sql(statement) for statement in artifact["statements"]))
        runtime = artifact.get("runtime_validation") or {}
        add_text_tab(self.notebook, "历史校验记录", json.dumps({
            "transaction_id": record["id"], "历史状态": record["state"], "会话状态": record["session_state"],
            "画布编译校验": valid, "代码生成完整": complete,
            "本地运行引用校验成功": runtime.get("success", False),
            "运行引用提示": runtime.get("warning"),
            "说明": "本窗口仅查看历史审批快照；不重新批准、执行或验证当前数据库及运行状态。",
        }, ensure_ascii=False, indent=2))
        self.root.update()
        if changes:
            first = changes[0]
            side = first["after"] or first["before"]
            self.review.select_canvas_node(side.get("group_key", "globalVariables"), side["node"].get("key", "globalVariables"))
        self.root.update()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transaction_id")
    parser.add_argument("--state-path", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "GxpLowcodeEditor/state/editor.sqlite3")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--close-after-capture", action="store_true")
    args = parser.parse_args()
    window = SavedApprovalPreview(*load_snapshot(args.transaction_id, args.state_path))
    if args.screenshot:
        def capture():
            try:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                capture_full_review(window.root, window.review, args.screenshot)
                print(f"Saved: {args.screenshot}", flush=True)
            finally:
                if args.close_after_capture:
                    window.root.destroy()
        window.root.after(500, capture)
    window.root.mainloop()


if __name__ == "__main__":
    main()
