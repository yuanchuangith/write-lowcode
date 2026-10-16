from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_editor.state import StateError, StateStore
from gxp_editor.service import EditorService


class AbortSessionWindow:
    def __init__(self, session_id: str, conversation_id: str | None = None, owner_revision: int | None = None):
        self.state = StateStore()
        self.session_id = session_id
        session = self.state.get_session(session_id, include_baseline=False)
        self.conversation_id = (session.get("conversation_id") or "") if conversation_id is None else conversation_id
        self.owner_revision = session.get("owner_revision", 0) if owner_revision is None else owner_revision
        self._owned_session()
        self.root = tk.Tk()
        self.root.title(f"GXP 旧编辑会话处理 · 对话 {self.conversation_id or '旧版'} · 会话 {session_id[:8]}")
        self.root.geometry("780x400")
        self.root.minsize(650, 350)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=18)
        keep = ttk.Button(buttons, text="保留旧会话", command=self.root.destroy)
        keep.pack(side="right")
        pending = session["state"] in {"locked", "planned", "awaiting_approval", "restore_pending"}
        transaction = None
        if session.get("transaction_id"):
            try:
                transaction = self.state.get_transaction(session["transaction_id"])
            except StateError:
                pass
        unexecuted = not transaction or transaction["state"] == "awaiting_approval"
        if pending and unexecuted:
            ttk.Button(buttons, text="取消未执行审批", command=self.abort).pack(side="right", padx=6)
            if transaction:
                ttk.Button(buttons, text="重新打开审批", command=self.reopen).pack(side="right", padx=6)
        else:
            ttk.Button(buttons, text="核对结果并结束会话", command=self.recover).pack(side="right", padx=6)
        archive_row = ttk.Frame(frame)
        archive_row.pack(side="bottom", fill="x")
        ttk.Button(archive_row, text="保留数据并释放本地流程", command=self.archive).pack(side="right")
        ttk.Label(frame, text="已有编辑会话，可继续审批或核对结果后结束", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, text=f"动作：{session['action_name']}（{session['action_code']}）\n"
                  f"对话：{self.conversation_id or '旧版未标记'}\n会话：{session_id}\n状态：{session['state']}\n\n"
                  "取消未执行审批会保留备份和记录。取消恢复审批会回到原保存状态。\n"
                  "核对结果只读取数据库，不覆盖或恢复数据；正在执行时会提示等待。\n"
                  "无法核对时，可手动保留数据库现状并释放本地流程；不会标记保存成功。\n"
                  "关闭窗口会保留原会话。", wraplength=700, justify="left").pack(anchor="w")
        keep.focus_set()

    def _owned_session(self):
        session = self.state.get_session(self.session_id, include_baseline=False)
        if (session.get("conversation_id") or "") != self.conversation_id or session.get("owner_revision", 0) != self.owner_revision:
            raise StateError("该会话已转交，当前处理窗口已失效。请关闭窗口并从当前归属对话重新打开。")
        return session

    def _finish(self):
        session = self._owned_session()
        if session["state"] != "verified":
            raise StateError("必须先核对保存结果才能结束会话。")
        self.state.update_session(
            self.session_id, "finished", conversation_id=self.conversation_id,
            owner_revision=self.owner_revision, expected_states=("verified",),
            expected_transaction_id=session.get("transaction_id"),
        )

    def abort(self):
        try:
            self._owned_session()
            self.state.abort_session(
                self.session_id, conversation_id=self.conversation_id,
                owner_revision=self.owner_revision,
            )
        except Exception as exc:
            messagebox.showerror("无法安全中止", str(exc), parent=self.root)
            return
        messagebox.showinfo("审批已取消", "未执行审批已取消。恢复审批取消后可核对原保存结果并结束会话。", parent=self.root)
        self.root.destroy()

    def reopen(self):
        try:
            session = self._owned_session()
            result = EditorService(conversation_id=self.conversation_id).reopen_approval(
                session["transaction_id"], owner_revision=self.owner_revision,
            )
            if not result.get("approval_window_opened"):
                raise StateError(result.get("approval_window_error") or result.get("message") or "审批窗口未能打开，请重试。")
            self.root.destroy()
        except Exception as exc:
            messagebox.showerror("审批未打开", str(exc), parent=self.root)

    def recover(self):
        from gxp_editor.recovery import recover_session
        try:
            session = self._owned_session()
            if session["state"] == "verified":
                self._finish()
            else:
                result = recover_session(
                    self.session_id, state=self.state, conversation_id=self.conversation_id,
                    owner_revision=self.owner_revision,
                )
                if result["outcome"] == "changed":
                    if not messagebox.askyesno(
                        "当前数据已变化",
                        "当前数据库内容与审批前后均不一致。\n是否保留当前数据并结束旧会话？\n此操作不会覆盖数据，也不代表原审批保存成功。",
                        parent=self.root,
                    ):
                        return
                    self._owned_session()
                    result = recover_session(
                        self.session_id, state=self.state, close_changed=True,
                        conversation_id=self.conversation_id, owner_revision=self.owner_revision,
                    )
                if result["outcome"] == "saved":
                    self._finish()
            messagebox.showinfo("会话已结束", "核对完成，旧会话不再占用。数据库内容未被修改。", parent=self.root)
            self.root.destroy()
        except Exception as exc:
            messagebox.showerror("核对未完成", f"{exc}\n\n若无法核对，可关闭原审批程序后使用“保留数据并释放”；此选项只释放本地占用，不会修改数据或确认保存成功。", parent=self.root)

    def archive(self):
        from gxp_editor.recovery import archive_edit_session
        try:
            snapshot = self._owned_session()
            if not messagebox.askyesno(
                "保留数据并释放本地流程",
                "请先关闭此流程的所有审批程序（包括旧版窗口）。\n"
                "确认保留数据库当前内容，归档并释放本地占用吗？\n\n"
                "本操作不会写入、恢复或覆盖数据库，也不会核对或标记保存成功。\n"
                "备份和审批记录会保留；原流程不能再次执行。后续编辑需重新读取数据、生成计划和审批。",
                parent=self.root,
            ):
                return
            result = archive_edit_session(
                self.session_id, state=self.state, conversation_id=self.conversation_id,
                owner_revision=self.owner_revision, expected_snapshot=snapshot,
            )
            if result["session_released"]:
                messagebox.showinfo("本地流程已释放", "已归档未核对流程。数据库内容未修改，保存结果未确认。", parent=self.root)
                self.root.destroy()
        except Exception as exc:
            messagebox.showerror("流程未释放", str(exc), parent=self.root)

    def run(self):
        self.root.mainloop()


def main() -> int:
    if len(sys.argv) not in {2, 4}:
        raise SystemExit("Usage: abort_session_app.py <session_id> [conversation_id owner_revision]")
    try:
        AbortSessionWindow(
            sys.argv[1], sys.argv[2] if len(sys.argv) == 4 else "",
            int(sys.argv[3]) if len(sys.argv) == 4 else None,
        ).run()
    except (StateError, ValueError) as exc:
        messagebox.showerror("会话处理窗口不可用", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
