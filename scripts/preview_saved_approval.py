"""Read-only Qt viewer for an already saved local approval; never executes SQL."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "mcp"))
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))

from PySide6.QtCore import QObject, QTimer
from approval_app import ApprovalShell, ApprovalWindow
from gxp_editor.qt_theme import application
from gxp_editor.transaction import validate_transaction_artifact

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


class SavedApprovalPreview(ApprovalWindow):
    """Reuse the review layout without initializing any editor or executor."""
    def __init__(self, record, artifact, baseline):
        QObject.__init__(self)
        self.transaction_id = record["id"]
        self.transaction = {"state": "saved"}
        self.artifact = artifact
        self.session = {
            "id": record["session_id"], "action_name": record["action_name"], "baseline": baseline,
        }
        self.index = 0
        self._window_lock = None
        self._executing = False
        self._review_blocked = True
        self.qt_app = application()
        self.root = ApprovalShell(self)
        self.timer = QTimer(self.root)
        self.root.setWindowTitle("GXP 历史审批 · 只读预览")
        self._build_ui({}, "来源：真实草稿备份与已保存审批快照。参数名称按原始 Key 展示。")
        self.close_button.setText("关闭预览")
        self.close_button.setToolTip("关闭只读历史预览。")
        self.approve_button.setText("历史已批准")
        self.execute_button.setText("已保存 · 只读")
        self.render()
        self.root.fit_to_screen()

    def render(self):
        super().render()
        self.badge.setText("历史审批 · 只读")
        count = len(self.artifact.get("approvals") or {})
        self.approved_metric.setText(f"{count} / {len(self.artifact['statements'])}")
        self.progress.setText(f"已保存 · 当前第 {self.index + 1} 条 · 仅查看历史快照")
        self.progress_bar.setValue(count)
        self.approve_button.setEnabled(False)
        self.execute_button.setEnabled(False)

    def approve_current(self):
        return  # A saved snapshot can never create another approval.

    def execute(self):
        return  # No executor is initialized or reachable from this viewer.


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("transaction_id")
    parser.add_argument("--state-path", type=Path, default=Path(os.environ["LOCALAPPDATA"]) / "GxpLowcodeEditor/state/editor.sqlite3")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--close-after-capture", action="store_true")
    parser.add_argument("--view", choices=("flow", "evidence", "code"), default="flow")
    args = parser.parse_args()
    window = SavedApprovalPreview(*load_snapshot(args.transaction_id, args.state_path))
    if args.view == "evidence":
        window.logic_review.views.setCurrentIndex(1)
    elif args.view == "code":
        window.notebook.setCurrentWidget(window.code_review)
    if args.screenshot:
        def capture():
            try:
                args.screenshot.parent.mkdir(parents=True, exist_ok=True)
                if not window.root.grab().save(str(args.screenshot)):
                    raise OSError("无法保存界面截图")
                print(f"Saved: {args.screenshot}", flush=True)
            finally:
                if args.close_after_capture:
                    window.close()
        QTimer.singleShot(500, capture)
    window.run()


if __name__ == "__main__":
    main()

