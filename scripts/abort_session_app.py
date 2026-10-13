from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_editor.state import StateStore


class AbortSessionWindow:
    def __init__(self, session_id: str):
        self.state = StateStore()
        self.session_id = session_id
        session = self.state.get_session(session_id)
        self.root = tk.Tk()
        self.root.title("GXP 旧编辑会话处理")
        self.root.geometry("650x310")
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)
        frame = ttk.Frame(self.root, padding=20)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="已有编辑会话，请手动选择是否中止", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w", pady=(0, 12))
        ttk.Label(frame, text=f"动作：{session['action_name']}（{session['action_code']}）\n"
                  f"会话：{session_id}\n状态：{session['state']}\n\n"
                  "中止会取消尚未执行的审批并释放会话占用，保留备份和记录。\n"
                  "写入前拒绝或确认已回滚的失败不会继续占用会话。\n"
                  "正在执行、已保存或提交结果不确定的事务需核对结果。\n"
                  "关闭窗口会保留原会话；中止后请重新发起编辑。", wraplength=600, justify="left").pack(anchor="w")
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=18)
        keep = ttk.Button(buttons, text="保留旧会话", command=self.root.destroy)
        keep.pack(side="right")
        ttk.Button(buttons, text="中止旧会话并释放占用", command=self.abort).pack(side="right", padx=12)
        keep.focus_set()

    def abort(self):
        try:
            self.state.abort_session(self.session_id)
        except Exception as exc:
            messagebox.showerror("无法安全中止", str(exc), parent=self.root)
            return
        messagebox.showinfo("已中止", "旧会话已安全中止，可以重新发起编辑。", parent=self.root)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: abort_session_app.py <session_id>")
    AbortSessionWindow(sys.argv[1]).run()
