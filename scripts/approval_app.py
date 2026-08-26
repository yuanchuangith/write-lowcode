from __future__ import annotations

import hashlib
import json
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))

from gxp_editor.approval import approve_statement, load_artifact, save_artifact
from gxp_editor.config import load_config
from gxp_editor.state import StateStore
from gxp_editor.transaction import validate_transaction_artifact
from gxp_editor.writer import execute_approved_transaction


def summarize_param(value: Any) -> str:
    if value is None or isinstance(value, (int, float, bool)):
        return repr(value)
    text = str(value)
    if len(text) <= 160:
        return text
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"<长文本 length={len(text)} sha256={digest}>"


class ApprovalWindow:
    def __init__(self, transaction_id: str):
        self.transaction_id = transaction_id
        self.config = load_config()
        self.state = StateStore()
        self.transaction = self.state.get_transaction(transaction_id)
        self.path = Path(self.transaction["artifact_path"])
        self.artifact = load_artifact(self.path)
        validate_transaction_artifact(self.artifact)
        self.index = 0

        self.root = tk.Tk()
        self.root.title("GXP 编排数据库逐条审批")
        self.root.geometry("1040x760")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

        database_safety = self.artifact["database_safety"]
        shared_account = bool(database_safety.get("shared_account"))

        self.header = ttk.Label(self.root, font=("Microsoft YaHei UI", 11, "bold"))
        self.header.pack(fill="x", padx=12, pady=(12, 6))
        self.safety = ttk.Label(
            self.root,
            text=(
                ("共享高权限账户受限模式：" if shared_account else "独立最小权限账户模式：")
                + "本程序只执行下方逐条批准的固定 SQL 数据库事务；"
                "不会调用后端编辑接口、Redis、发布接口，也不会写入 DLL/PDB。"
            ),
            wraplength=990,
            foreground="#9a3412",
        )
        self.safety.pack(fill="x", padx=12, pady=4)
        self.summary = ttk.Label(self.root, wraplength=990)
        self.summary.pack(fill="x", padx=12, pady=6)
        self.text = tk.Text(self.root, wrap="none", font=("Consolas", 10))
        self.text.pack(fill="both", expand=True, padx=12, pady=6)
        buttons = ttk.Frame(self.root)
        buttons.pack(fill="x", padx=12, pady=12)
        self.approve_button = ttk.Button(buttons, text="批准当前语句", command=self.approve_current)
        self.approve_button.pack(side="left")
        ttk.Button(buttons, text="查看完整内容", command=self.show_full_content).pack(side="left", padx=8)
        self.execute_button = ttk.Button(
            buttons,
            text="执行已全部批准的事务",
            command=self.execute,
            state="disabled",
        )
        self.execute_button.pack(side="right")
        ttk.Button(buttons, text="关闭（不执行）", command=self.root.destroy).pack(side="right", padx=8)
        self.render()

    def current(self) -> dict[str, Any]:
        return self.artifact["statements"][self.index]

    def render(self) -> None:
        statements = self.artifact["statements"]
        statement = self.current()
        approved = statement["id"] in (self.artifact.get("approvals") or {})
        self.header.configure(
            text=(
                f"事务 {self.transaction_id} | {self.index + 1}/{len(statements)} | "
                f"{'已批准' if approved else '等待人工批准'}"
            )
        )
        self.summary.configure(text=statement["summary"])
        params = "\n".join(
            f"[{index}] {summarize_param(value)}"
            for index, value in enumerate(statement["params"])
        )
        runtime = self.artifact["runtime_validation"]
        database_safety = self.artifact["database_safety"]
        runtime_reference = runtime.get("reference_manifest_sha256") or "not available (advisory only)"
        runtime_warning = runtime.get("warning") or "none"
        content = (
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
            f"SQL:\n{statement['sql']}\n\nParameters:\n{params}"
        )
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)
        approvals = self.artifact.get("approvals") or {}
        all_approved = all(item["id"] in approvals for item in statements)
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

    def show_full_content(self) -> None:
        statement = self.current()
        window = tk.Toplevel(self.root)
        window.title(f"语句完整内容 - {statement['id']}")
        window.geometry("1100x760")
        text = tk.Text(window, wrap="none", font=("Consolas", 10))
        text.pack(fill="both", expand=True, padx=10, pady=10)
        text.insert(
            "1.0",
            json.dumps(
                {
                    "summary": statement["summary"],
                    "sql": statement["sql"],
                    "params": statement["params"],
                    "statement_hash": statement["statement_hash"],
                    "runtime_validation": self.artifact["runtime_validation"],
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
        )
        text.configure(state="disabled")

    def approve_current(self) -> None:
        self.transaction = self.state.get_transaction(self.transaction_id)
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
            messagebox.showinfo("执行结果", json.dumps(result, ensure_ascii=False, indent=2))
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
    ApprovalWindow(sys.argv[1]).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
