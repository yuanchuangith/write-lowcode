from __future__ import annotations

import sys
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_editor.state import StateError, StateStore
from gxp_editor.service import EditorService
from gxp_editor.ui_launch import managed_window_launch, report_window_startup, register_window_process, report_window_heartbeat
register_window_process()


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
        self._initialize_operations()
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=18)
        keep = self._button(buttons, "保留旧会话", self._close)
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
            self._button(buttons, "取消未执行审批", self.abort).pack(side="right", padx=6)
            if transaction:
                self._button(buttons, "重新打开审批", self.reopen).pack(side="right", padx=6)
        else:
            self._button(buttons, "核对结果并结束会话", self.recover).pack(side="right", padx=6)
        archive_row = ttk.Frame(frame)
        archive_row.pack(side="bottom", fill="x")
        self._button(archive_row, "保留数据并释放本地流程", self.archive).pack(side="right")
        self.operation_status = ttk.Label(frame, text="", wraplength=700, justify="left")
        self.operation_status.pack(side="bottom", anchor="w")
        ttk.Label(frame, text="已有编辑会话，可继续审批或核对结果后结束", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, text=f"动作：{session['action_name']}（{session['action_code']}）\n"
                  f"对话：{self.conversation_id or '旧版未标记'}\n会话：{session_id}\n状态：{session['state']}\n\n"
                  "取消未执行审批会保留备份和记录。取消恢复审批会回到原保存状态。\n"
                  "核对结果只读取数据库，不覆盖或恢复数据；正在执行时会提示等待。\n"
                  "无法核对时，可手动保留数据库现状并释放本地流程；不会标记保存成功。\n"
                  "关闭窗口会保留原会话。", wraplength=700, justify="left").pack(anchor="w")
        keep.focus_set()

    def _initialize_operations(self):
        self._busy = False
        self._closed = False
        self._buttons = []
        self._operation_generation = 0

    def _button(self, parent, text, command):
        button = ttk.Button(parent, text=text, command=command)
        self._buttons.append(button)
        return button

    def _set_operation_status(self, text):
        status = getattr(self, "operation_status", None)
        if status is not None:
            status.configure(text=text)

    def _close(self):
        if self._busy:
            self._set_operation_status("操作仍在进行，请等待结果后再关闭；请勿重复发起操作。")
            return
        if not self._closed:
            self._closed = True
            self.root.destroy()

    def _start_operation(self, text, work, completed, error_title, error_hint=""):
        if self._busy or self._closed:
            return
        self._busy = True
        self._operation_generation += 1
        generation = self._operation_generation
        self._operation_results = queue.Queue()
        self._operation_completed = completed
        self._operation_error = (error_title, error_hint)
        for button in self._buttons:
            button.configure(state="disabled")
        self._set_operation_status(text)
        results = self._operation_results

        def run():
            try:
                results.put((True, work()))
            except Exception as exc:
                results.put((False, exc))

        # Only the UI thread polls results and touches Tk. Closing is refused
        # until the bounded local operation finishes; no service is created.
        self._worker = threading.Thread(target=run, name="gxp-session-handling", daemon=True)
        try:
            self._worker.start()
        except Exception as exc:
            results.put((False, exc))
        self.root.after(25, lambda: self._poll_operation(generation))

    def _poll_operation(self, generation):
        if self._closed or generation != self._operation_generation or not self._busy:
            return
        try:
            succeeded, value = self._operation_results.get_nowait()
        except queue.Empty:
            self.root.after(25, lambda: self._poll_operation(generation))
            return
        completed = self._operation_completed
        error_title, error_hint = self._operation_error
        self._busy = False
        for button in self._buttons:
            button.configure(state="normal")
        self._set_operation_status("")
        try:
            if not succeeded:
                raise value
            completed(value)
        except Exception as exc:
            messagebox.showerror(error_title, f"{exc}{error_hint}", parent=self.root)

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
        def work():
            self._owned_session()
            self.state.abort_session(
                self.session_id, conversation_id=self.conversation_id,
                owner_revision=self.owner_revision,
            )
        def completed(_):
            messagebox.showinfo("审批已取消", "未执行审批已取消。恢复审批取消后可核对原保存结果并结束会话。", parent=self.root)
            self._close()
        self._start_operation("正在取消未执行审批，请等待…", work, completed, "无法安全中止")

    def reopen(self):
        def work():
            session = self._owned_session()
            return EditorService(conversation_id=self.conversation_id).reopen_approval(
                session["transaction_id"], owner_revision=self.owner_revision,
            )
        def completed(result):
            if result.get("approval_window_starting"):
                messagebox.showinfo("审批窗口正在启动", "尚未收到窗口就绪回执，当前处理窗口会保留。稍后可重新查询启动结果。", parent=self.root)
                return
            if not result.get("approval_window_opened"):
                raise StateError(result.get("approval_window_error") or result.get("message") or "审批窗口未能打开，请重试。")
            self._close()
        self._start_operation("正在核对并重新打开审批，请等待…", work, completed, "审批未打开")

    def _recover(self, *, close_changed=False):
        from gxp_editor.recovery import recover_session
        session = self._owned_session()
        if session["state"] == "verified":
            self._finish()
            return {"outcome": "finished", "session_released": True}
        options = {"close_changed": True} if close_changed else {}
        result = recover_session(
            self.session_id, state=self.state, conversation_id=self.conversation_id,
            owner_revision=self.owner_revision, **options,
        )
        if result.get("outcome") == "saved":
            self._finish()
            return {**result, "session_released": True}
        return result

    def _recovery_completed(self, result):
        if result.get("outcome") == "changed":
            if messagebox.askyesno(
                "当前数据已变化",
                "当前数据库内容与审批前后均不一致。\n是否保留当前数据并结束旧会话？\n此操作不会覆盖数据，也不代表原审批保存成功。",
                parent=self.root,
            ):
                self._start_recovery(close_changed=True)
            return
        if (result.get("outcome") not in {"saved", "finished", "not_applied", "closed_changed"}
                or result.get("session_released") is not True):
            raise StateError(result.get("message") or result.get("reason_code") or "核对尚未完成，旧会话仍保留，请核实后重试。")
        messagebox.showinfo("会话已结束", "核对完成，旧会话不再占用。数据库内容未被修改。", parent=self.root)
        self._close()

    def _start_recovery(self, *, close_changed=False):
        self._start_operation(
            "正在核对结果并结束会话，请等待…", lambda: self._recover(close_changed=close_changed),
            self._recovery_completed, "核对未完成",
            "\n\n若无法核对，可关闭原审批程序后使用“保留数据并释放”；此选项只释放本地占用，不会修改数据或确认保存成功。",
        )

    def recover(self):
        self._start_recovery()

    def archive(self):
        def confirm(snapshot):
            if not messagebox.askyesno(
                "保留数据并释放本地流程",
                "请先关闭此流程的所有审批程序（包括旧版窗口）。\n"
                "确认保留数据库当前内容，归档并释放本地占用吗？\n\n"
                "本操作不会写入、恢复或覆盖数据库，也不会核对或标记保存成功。\n"
                "备份和审批记录会保留；原流程不能再次执行。后续编辑需重新读取数据、生成计划和审批。",
                parent=self.root,
            ):
                return
            def work():
                from gxp_editor.recovery import archive_edit_session
                self._owned_session()
                return archive_edit_session(
                    self.session_id, state=self.state, conversation_id=self.conversation_id,
                    owner_revision=self.owner_revision, expected_snapshot=snapshot,
                )
            def completed(result):
                if result.get("session_released") is not True:
                    raise StateError(result.get("message") or "本地流程尚未释放，请核实后重试。")
                messagebox.showinfo("本地流程已释放", "已归档未核对流程。数据库内容未修改，保存结果未确认。", parent=self.root)
                self._close()
            self._start_operation("正在保留数据并释放本地流程，请等待…", work, completed, "流程未释放")
        self._start_operation("正在核对本地流程归属，请等待…", self._owned_session, confirm, "流程未释放")

    def run(self):
        def heartbeat():
            if self.root.winfo_exists():
                report_window_heartbeat(phase="review")
                self.root.after(1000, heartbeat)
        def report_visible():
            if self.root.winfo_viewable():
                report_window_startup("ready", phase="review", review_ready=True, preview_pending=False)
                self.root.after(1000, heartbeat)
            else:
                self.root.after(25, report_visible)
        self.root.after_idle(report_visible)
        try:
            self.root.mainloop()
        except BaseException:
            report_window_startup("failed", error="UI event loop exited with an exception")
            raise
        finally:
            report_window_startup("closed")


def main() -> int:
    if len(sys.argv) not in {2, 4}:
        raise SystemExit("Usage: abort_session_app.py <session_id> [conversation_id owner_revision]")
    try:
        AbortSessionWindow(
            sys.argv[1], sys.argv[2] if len(sys.argv) == 4 else "",
            int(sys.argv[3]) if len(sys.argv) == 4 else None,
        ).run()
    except Exception as exc:
        report_window_startup("failed", error=str(exc))
        if not managed_window_launch():
            messagebox.showerror("会话处理窗口不可用", str(exc))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
