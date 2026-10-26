"""Request a graph comparison after a completed save; never change the graph."""
from __future__ import annotations

from typing import Any


def look_graph_review(session: dict[str, Any], transaction: dict[str, Any],
                      review_state: dict[str, Any] | None = None) -> dict[str, Any] | None:
    if (session.get("state") != "finished" or transaction.get("state") != "saved"
            or not transaction.get("id")
            or session.get("transaction_id") != transaction["id"]
            or transaction.get("session_id") != session.get("id")):
        return None
    review_state = review_state or {"transaction_id": transaction["id"], "outcome": "unreviewed",
                                    "revision": 0, "proposal_sha256": "", "graph_revision": None}
    outcome = review_state["outcome"]
    next_action = {"unreviewed": "compare_graph", "proposed": "await_graph_consent",
                   "pending_publication": "wait_for_publication"}.get(outcome, "none")
    return {
        "transaction_id": transaction["id"],
        "database_scope": session.get("database_scope") or "",
        "environment_binding_known": bool(session.get("database_scope")),
        "action": {key: session.get(key) for key in ("design_id", "ref_id", "action_code", "action_name")},
        "review_state": review_state,
        "review_required": next_action != "none",
        "next_action": next_action,
        "compare_before_asking": True,
        "ask_only_if_changes": True,
        "supported_changes": ["add", "update"],
        "requires_user_confirmation": True,
        "question_if_changes": "是否根据本次排查结果，新增或修改 Look 关系图？",
        "graph_write_executed": False,
        "next_step": (
            "复用本次排查证据，对照同环境的已有关系图；没有的关系可新增，已有的可补充或修正。"
            "仅有已确认且可记录的差异时，先展示差异再询问；已有关系准确且无差异则说明无需更新。"
            "未核对或证据不足不代表图中没有关系；草稿保存不证明已发布或运行通过。"
            "按 review_state 继续；已核对、拒绝或已记录不重复询问，待发布项先等待发布证据。"
            "核对原 database_scope 与 Look 环境，不能将不同格式的环境标识直接互填。"
            "同一事务只处理一次，队列合并处理；用户同意后才调用 Look 图工具写入。"
        ),
    }
