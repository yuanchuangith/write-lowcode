from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

from gxp_editor.approval import approve_statement, load_artifact, save_artifact
from gxp_editor.approval_display import canvas_change_summary, execution_result_message, parameter_details, render_full_sql, statement_scope
from gxp_editor.compiler import CompilerBridge
from gxp_editor.code_review_ui import CodeReview
from gxp_editor.component_review_ui import ComponentReview
from gxp_editor.component_preview import component_changes
from gxp_editor.config import load_config
from gxp_editor.hashing import sha256_text
from gxp_editor.state import StateStore
from gxp_editor.transaction import TransactionPlanError, validate_transaction_artifact
from gxp_editor.single_row_checker import SingleRowCheckError
from gxp_editor.writer import execute_approved_transaction


class ApprovalWindow:
    def __init__(self, transaction_id: str):
        self.transaction_id = transaction_id
        self.config = load_config()
        self.state = StateStore()
        self.transaction = self.state.get_transaction(transaction_id)
        self.path = Path(self.transaction["artifact_path"])
        self.artifact = load_artifact(self.path)
        validate_transaction_artifact(self.artifact)
        self.session = self.state.get_session(self.artifact["session_id"])
        self.index = 0
        preview_registry, registry_note = self.load_preview_registry()

        self.root = tk.Tk()
        self.root.title("GXP 草稿保存审批")
        self.root.geometry("1180x860")
        self.root.minsize(850, 620)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        style = ttk.Style(self.root)
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(10, 5))
        style.configure("TNotebook.Tab", padding=(14, 6))
        self.header = ttk.Label(self.root, font=("Microsoft YaHei UI", 14, "bold"))
        self.header.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(
            self.root,
            text="逐条批准后才可执行。此次操作只保存草稿，平台手动发布后才会生效。",
            foreground="#9a3412",
        ).pack(fill="x", padx=12, pady=4)
        scope = ttk.LabelFrame(self.root, text="本条语句的修改范围", padding=10)
        scope.pack(fill="x", padx=12, pady=6)
        self.summary = ttk.Label(scope, justify="left", wraplength=1120)
        self.summary.pack(fill="x")
        toolbar = ttk.Frame(self.root)
        toolbar.pack(fill="x", padx=12, pady=4)
        self.previous_button = ttk.Button(toolbar, text="上一条", command=lambda: self.navigate(-1))
        self.previous_button.pack(side="left")
        self.next_button = ttk.Button(toolbar, text="下一条", command=lambda: self.navigate(1))
        self.next_button.pack(side="left", padx=6)
        self.progress = ttk.Label(toolbar)
        self.progress.pack(side="left", padx=10)
        ttk.Button(toolbar, text="导出完整 SQL…", command=self.export_sql).pack(side="right")
        ttk.Button(toolbar, text="复制完整 SQL", command=self.copy_sql).pack(side="right", padx=6)
        self.wrap_sql = tk.BooleanVar(self.root, value=False)
        ttk.Checkbutton(toolbar, text="SQL 自动换行", variable=self.wrap_sql, command=self.toggle_wrap).pack(side="right", padx=8)
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=6)
        self.component_review = ComponentReview(self.notebook, preview_registry, registry_note)
        self.code_review = CodeReview(self.notebook)
        self.changes_text = self.add_text_tab("本次改动", wrap="word")
        self.text = self.add_text_tab("完整 SQL", wrap="none")
        self.raw_changes_text = self.add_text_tab("原始节点详情", wrap="word")
        self.params_text = self.add_text_tab("模板与完整参数", wrap="none")
        self.validation_text = self.add_text_tab("校验与指纹", wrap="word")
        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", padx=12, pady=12)
        self.approve_button = ttk.Button(buttons, text="批准当前语句", command=self.approve_current)
        self.approve_button.pack(side="left")
        self.execute_button = ttk.Button(
            buttons,
            text="执行并保存草稿",
            command=self.execute,
            state="disabled",
        )
        self.execute_button.pack(side="right")
        ttk.Button(buttons, text="关闭（不执行）", command=self.root.destroy).pack(side="right", padx=8)
        self.render()

    def load_preview_registry(self) -> tuple[dict[str, Any], str]:
        """Read display titles from the exact bridge bound to this approval.

        Missing metadata falls back to actual parameter keys; display metadata
        never changes the signed artifact or controls approval readiness.
        """
        expected = (self.artifact.get("compiler_evidence") or {}).get("compiler_binding") or {}
        if not expected:
            return {}, "未绑定元件标题元数据；按实际参数 Key 展示完整配置。"
        try:
            bridge = CompilerBridge(self.config)
            if sha256_text(bridge.path.read_text(encoding="utf-8")) != expected.get("bridge_sha256"):
                raise ValueError("bridge changed")
            registry = bridge._call({"command": "describe_registry"}, timeout_seconds=8)
            if (
                registry.get("command") != "describe_registry"
                or bridge._stable_registry_hash(registry) != expected.get("registry_hash")
                or registry.get("rule_source_sha256") != expected.get("rule_source_sha256")
                or registry.get("frontend_version") != expected.get("frontend_version")
            ):
                raise ValueError("registry changed")
            return {element["key"]: element for element in registry["elements"]}, "配置来自草稿备份与待保存画布；参数名称来自本次审批绑定的真实元件注册表。"
        except Exception:
            return {}, "未能核对元件标题元数据；按实际参数 Key 展示，配置值保持完整。"

    def add_text_tab(self, title: str, *, wrap: str) -> tk.Text:
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=title)
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        text = tk.Text(frame, wrap=wrap, font=("Consolas", 10), padx=10, pady=10, undo=False)
        vertical = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        horizontal = ttk.Scrollbar(frame, orient="horizontal", command=text.xview)
        text.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        text.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        text.configure(state="disabled")
        text.bind("<Control-a>", lambda event: self.select_all(text))
        return text

    @staticmethod
    def select_all(text: tk.Text) -> str:
        text.tag_add("sel", "1.0", "end-1c")
        return "break"

    @staticmethod
    def set_text(text: tk.Text, content: str) -> None:
        text.configure(state="normal")
        text.delete("1.0", "end")
        text.insert("1.0", content)
        text.configure(state="disabled")

    def navigate(self, direction: int) -> None:
        self.index = max(0, min(len(self.artifact["statements"]) - 1, self.index + direction))
        self.render()

    def toggle_wrap(self) -> None:
        self.text.configure(wrap="word" if self.wrap_sql.get() else "none")

    def copy_sql(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(render_full_sql(self.current()))
        self.progress.configure(text="已复制当前语句的完整 SQL")

    def export_sql(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root, title="导出当前语句的完整 SQL", defaultextension=".sql",
            filetypes=[("SQL 文件", "*.sql"), ("所有文件", "*.*")],
            initialfile=f"gxp-{self.transaction_id[:8]}-{self.index + 1}.sql",
        )
        if path:
            try:
                Path(path).write_text(render_full_sql(self.current()) + "\n", encoding="utf-8")
                self.progress.configure(text="完整 SQL 已导出")
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc), parent=self.root)

    def change_details(self, changes=None) -> str:
        if changes is None:
            before = json.loads(self.session["baseline"]["data"])
            after = json.loads(self.artifact["desired_design"]["data"])
            changes = component_changes(before, after)
        chunks = []
        for item in changes:
            side = item["after"] or item["before"]
            node = side["node"]
            old = item["before"]["node"] if item["before"] else None
            new = item["after"]["node"] if item["after"] else None
            chunks.append(f"{item['kind']} · {side['group']} · 第 {side['line']} 行 · {node.get('title') or node['elementKey']}\n节点：{node.get('key', 'globalVariables')}\n修改前：\n{json.dumps(old, ensure_ascii=False, indent=2)}\n修改后：\n{json.dumps(new, ensure_ascii=False, indent=2)}")
        return "\n\n".join(chunks) or "画布节点未发生变更。"

    def current(self) -> dict[str, Any]:
        return self.artifact["statements"][self.index]

    def render(self) -> None:
        statements = self.artifact["statements"]
        statement = self.current()
        approved = statement["id"] in (self.artifact.get("approvals") or {})
        self.header.configure(
            text=(
                f"{self.session['action_name']}（{self.artifact['action_code']}） · 语句 {self.index + 1}/{len(statements)} · "
                f"{'已批准' if approved else '等待人工批准'}"
            )
        )
        self.summary.configure(text=statement_scope(statement))
        runtime = self.artifact["runtime_validation"]
        database_safety = self.artifact["database_safety"]
        runtime_reference = runtime.get("reference_manifest_sha256") or "not available (advisory only)"
        runtime_warning = runtime.get("warning") or "none"
        content = (
            f"事务 ID：{self.transaction_id}\n"
            f"Statement SHA-256: {statement['statement_hash']}\n"
            f"SQL SHA-256: {statement['sql_sha256']}\n"
            f"Design ID: {statement['design_id']}\n"
            f"Expected affected rows: {statement['expected_rows']}\n"
            f"Old content hash: {statement['old_hash']}\n"
            f"New content hash: {statement['new_hash']}\n"
            f"C# hash: {runtime['csharp_sha256']}\n"
            f"Local runtime validation (advisory): success={runtime.get('success', False)}, "
            f"source_mode={runtime.get('source_mode', 'unknown')}\n"
            f"Runtime reference manifest: {runtime_reference}\n"
            f"Runtime warning: {runtime_warning}\n"
            "Manual publication is required to generate the runtime DLL.\n\n"
            f"Database account mode: {database_safety['account_mode']}\n"
            f"SHOW GRANTS fingerprint: {database_safety['grants_sha256']}\n\n"
            "实际执行仍使用已签名的 SQL 模板和绑定参数。完整 SQL 是审阅副本，"
            "采用 MySQL 默认字符串转义方式展开，不修改审批或执行数据。\n"
            "本程序不会调用后端编辑、发布接口，也不会修改 Redis 或 DLL/PDB。"
        )
        self.set_text(self.text, render_full_sql(statement))
        self.text.tag_configure("scope", background="#edf6ff", foreground="#124077")
        for line, value in enumerate(self.text.get("1.0", "end-1c").splitlines(), 1):
            if value.startswith("WHERE ") or value.startswith("  AND "):
                self.text.tag_add("scope", f"{line}.0", f"{line}.end")
        self.set_text(self.params_text, parameter_details(statement))
        self.set_text(self.validation_text, content)
        canvas_sources = (self.session["baseline"]["data"], self.artifact["desired_design"]["data"])
        if getattr(self, "_canvas_sources", None) != canvas_sources:
            before, after = (json.loads(source) for source in canvas_sources)
            changes = component_changes(before, after)
            self.component_review.render(before, after, changes=changes)
            self.code_review.set_canvas_context(before, after, changes=changes)
            self.set_text(self.changes_text, canvas_change_summary(before, after, changes=changes))
            self.set_text(self.raw_changes_text, self.change_details(changes))
            self._canvas_sources = canvas_sources
        self.code_review.update_sources(self.session["baseline"], self.artifact["desired_design"])
        self.changes_text.tag_configure("before", foreground="#9a3412")
        self.changes_text.tag_configure("after", foreground="#166534")
        for line, value in enumerate(self.changes_text.get("1.0", "end-1c").splitlines(), 1):
            if value.startswith("修改前："):
                self.changes_text.tag_add("before", f"{line}.0", f"{line}.end")
            elif value.startswith("修改后："):
                self.changes_text.tag_add("after", f"{line}.0", f"{line}.end")
        approvals = self.artifact.get("approvals") or {}
        all_approved = all(item["id"] in approvals for item in statements)
        self.progress.configure(text=f"已批准 {sum(item['id'] in approvals for item in statements)}/{len(statements)} 条")
        self.previous_button.configure(state="normal" if self.index > 0 else "disabled")
        self.next_button.configure(state="normal" if self.index + 1 < len(statements) else "disabled")
        transaction_ready = (
            self.transaction["state"] == "awaiting_approval"
            and not self.artifact.get("execution_started")
        )
        self.execute_button.configure(
            state="normal" if all_approved and transaction_ready else "disabled"
        )
        self.approve_button.configure(
            state="disabled" if approved or not transaction_ready else "normal"
        )

    def approve_current(self) -> None:
        self.transaction = self.state.get_transaction(self.transaction_id)
        try:
            validate_transaction_artifact(self.artifact)
            on_disk = load_artifact(self.path)
            validate_transaction_artifact(on_disk)
            if on_disk["transaction_hash"] != self.artifact["transaction_hash"]:
                raise TransactionPlanError("审批内容已改变，请重新生成并检查。")
        except (TransactionPlanError, SingleRowCheckError) as exc:
            self.approve_button.configure(state="disabled")
            self.execute_button.configure(state="disabled")
            messagebox.showerror("已阻止审批", str(exc), parent=self.root)
            return
        if self.transaction["state"] != "awaiting_approval" or self.artifact.get("execution_started"):
            messagebox.showerror(
                "事务不可审批",
                "该事务已经尝试执行，必须重新生成事务并逐条审批。",
            )
            self.render()
            return
        statement = self.current()
        if not messagebox.askyesno(
            "确认审批",
            "确认批准当前显示的这一条数据库语句吗？\n批准不代表立即执行。",
        ):
            return
        approve_statement(self.config, self.artifact, statement["id"])
        save_artifact(self.path, self.artifact)
        if self.index + 1 < len(self.artifact["statements"]):
            self.index += 1
        self.render()

    def execute(self) -> None:
        if self.artifact["database_safety"].get("shared_account") and not messagebox.askyesno(
            "共享账户风险确认",
            "当前使用共享数据库账户。固定 SQL、设计锁和审批只能限制本插件内的操作，"
            "不能缩小该账户在其他客户端中的权限。确认继续吗？",
        ):
            return
        if not messagebox.askyesno(
            "最终执行确认",
            "所有语句已经分别批准。现在才会打开短事务、重新锁定并校验草稿，然后执行。是否继续？",
        ):
            return
        try:
            result = execute_approved_transaction(self.transaction_id)
            messagebox.showinfo("执行结果", execution_result_message(result))
            self.root.destroy()
        except Exception as exc:
            messagebox.showerror("事务未完成", str(exc))
            self.transaction = self.state.get_transaction(self.transaction_id)
            self.artifact = load_artifact(self.path)
            self.render()

    def run(self) -> None:
        self.root.mainloop()


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: approval_app.py <transaction_id>")
    try:
        ApprovalWindow(sys.argv[1]).run()
    except (TransactionPlanError, SingleRowCheckError) as exc:
        messagebox.showerror("修改已被检查器阻止", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
