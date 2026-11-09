"""List local project blockers and open the existing human handling window."""
from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp"))

from gxp_project_context.local_sessions import active_sessions

STATES = {"saved": "已保存，本地未结束", "locked": "编辑未执行", "planned": "已计划，未执行",
          "awaiting_approval": "审批未执行", "executing": "正在执行或待核实", "saved_state_pending": "保存结果待核实",
          "verified": "已核对，待结束", "restore_pending": "恢复审批待处理"}
STATES.update({"active": "表单编辑未结束", "reviewing": "表单审批待处理", "pending": "表单保存待审批",
               "approved": "表单保存已审批", "uncertain": "执行结果待核实", "failed": "执行结果待核实"})


class SessionManagerWindow:
    def __init__(self, root, loader=active_sessions, opener=None):
        self.root, self.loader, self.opener = root, loader, opener or self._open_handler
        self.rows = {}
        self.busy = False
        self.results = queue.Queue()
        self.message = tk.StringVar(root)
        root.title("GXP 未结束流程 · 仅处理本地占用")
        root.geometry("920x460")
        frame = ttk.Frame(root, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="选择一个旧流程，打开处理窗口", font=("Microsoft YaHei UI", 14, "bold")).pack(anchor="w")
        ttk.Label(frame, text="未执行的流程可取消；已保存或无法核对的流程可选择“保留数据并释放本地流程”。\n处理窗口会再次确认；数据库内容、备份和历史记录均保留。", wraplength=880).pack(anchor="w", pady=10)
        self.table = ttk.Treeview(frame, columns=("name", "code", "state", "updated"), show="headings", selectmode="browse")
        for key, label, width in (("name", "动作/流程", 290), ("code", "编号", 100), ("state", "状态", 190), ("updated", "更新时间", 190)):
            self.table.heading(key, text=label)
            self.table.column(key, width=width)
        self.table.pack(fill="both", expand=True)
        toolbar = ttk.Frame(frame)
        toolbar.pack(fill="x", pady=10)
        ttk.Button(toolbar, text="刷新列表", command=self.reload).pack(side="left")
        ttk.Button(toolbar, text="打开选中流程处理窗口", command=self.handle).pack(side="left", padx=10)
        ttk.Button(toolbar, text="关闭", command=root.destroy).pack(side="right")
        ttk.Label(frame, textvariable=self.message, wraplength=880).pack(anchor="w")
        self.reload()
        root.after(100, self.poll)

    def reload(self):
        if self.busy:
            return
        try:
            self.rows = {}
            for row in self.table.get_children():
                self.table.delete(row)
            for index, row in enumerate(self.loader()):
                key = str(index)
                self.rows[key] = row
                self.table.insert("", "end", iid=key, values=(row.get("action_name") or row.get("kind") or row.get("id"),
                    row.get("action_code", ""), STATES.get(row.get("state"), row.get("state", "待核对")), row.get("updated_at", "")))
            self.message.set(f"还有 {len(self.rows)} 个独立流程需要处理。处理完成后刷新本列表，再回项目窗口切换。" if self.rows else "未结束流程已处理完，可以回到项目窗口切换数据库。")
        except Exception:
            self.message.set("无法读取本地流程状态；未修改任何记录。")

    @staticmethod
    def _open_handler(row):
        if row.get("domain") != "action" or row.get("kind") == "transaction":
            raise RuntimeError("该记录需要在原表单或审批窗口处理，不能自动删除。")
        from gxp_editor.service import EditorService
        return EditorService(conversation_id=row.get("conversation_id") or "").abort_edit_session(row["id"])

    def handle(self):
        if self.busy:
            return
        selected = self.table.selection()
        if not selected:
            self.message.set("请先选择一个流程。")
            return
        row = self.rows[selected[0]]
        self.busy = True
        self.message.set("正在打开处理窗口，尚未取消或释放任何流程…")
        def work():
            try:
                self.results.put((True, self.opener(row)))
            except Exception as error:
                self.results.put((False, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            success, result = self.results.get_nowait()
            self.busy = False
            if success:
                if result.get("confirmation_window_opened") or result.get("confirmation_window_starting"):
                    self.message.set("请在打开的窗口选择处理方式；关闭该窗口会保留原流程。处理完成后点击刷新列表。")
                else:
                    startup = result.get("window_startup") or {}
                    self.message.set("处理窗口未就绪：" + str(startup.get("window_error") or result.get("message") or "请检查 Write 运行环境。")[:600])
            else:
                self.message.set("处理窗口未打开：" + str(result)[:600])
        except queue.Empty:
            pass
        self.root.after(100, self.poll)


def main():
    from gxp_editor.runtime_package import RuntimeLease
    from gxp_project_context.session_launcher import register_launcher
    register_launcher(ROOT)
    with RuntimeLease(ROOT, role="ui"):
        root = tk.Tk()
        window = SessionManagerWindow(root)
        root.update_idletasks()
        root.deiconify()
        root.update()
        import os, tempfile
        receipt = os.environ.pop("GXP_PROJECT_SESSION_MANAGER_RECEIPT", "")
        if receipt:
            from gxp_form_editor.common import atomic_json
            target = Path(receipt)
            if target.is_absolute() and target.parent.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
                atomic_json(target, {"ready": bool(root.winfo_viewable()), "flows": len(window.rows)})
        root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
