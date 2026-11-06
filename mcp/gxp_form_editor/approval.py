"""Form-only approval signatures.  Approval and execution are local UI actions."""
from __future__ import annotations

import copy
import hashlib
import hmac
import secrets
import time
from typing import Any

from .common import FormError, canonical, digest, file_lock, runtime_root


DOMAIN = "form_config"
REQUIRED_BINDINGS = {"conversation_id", "session_id", "target", "baseline_hash", "candidate_hash", "adapter_hash", "requests", "actor"}


def signing_secret() -> bytes:
    """Use a separate credential entry; never share the action editor secret."""
    import keyring
    value = keyring.get_password("GxpFormEditor", "approval-signing-v1")
    if not value:
        with file_lock(runtime_root() / "locks" / "approval-secret.lock", timeout=5):
            value = keyring.get_password("GxpFormEditor", "approval-signing-v1")
            if not value:
                value = secrets.token_hex(32)
                keyring.set_password("GxpFormEditor", "approval-signing-v1", value)
    return value.encode("utf-8")


def _validate_binding(binding: dict[str, Any]) -> None:
    if not isinstance(binding, dict):
        raise FormError("form_approval_binding_invalid", "审批绑定必须是对象。")
    missing = sorted(key for key in REQUIRED_BINDINGS if not binding.get(key))
    if missing:
        raise FormError("form_approval_binding_incomplete", "审批缺少必须绑定的内容。", {"missing": missing})
    if not isinstance(binding.get("requests"), list) or not all(isinstance(item, dict) for item in binding["requests"]):
        raise FormError("form_approval_requests_invalid", "审批必须包含完整请求清单。")
    target = binding["target"]
    if not isinstance(target, dict) or not target.get("page_id") or not target.get("data_version_id"):
        raise FormError("form_approval_target_invalid", "审批必须固定页面和草稿版本。")


def prepare_approval(binding: dict[str, Any], ttl_seconds: int = 900, now: float | None = None) -> dict[str, Any]:
    _validate_binding(binding)
    clock = time.time() if now is None else float(now)
    if not 1 <= ttl_seconds <= 3600:
        raise FormError("form_approval_ttl_invalid", "审批有效期必须为 1 至 3600 秒。")
    return {
        "domain": DOMAIN,
        "version": 1,
        "nonce": secrets.token_hex(16),
        "binding": copy.deepcopy(binding),
        "binding_hash": digest(binding),
        "created_at": clock,
        "expires_at": clock + ttl_seconds,
        "approval": None,
    }


def _payload(artifact: dict[str, Any], reviewer: str, approved_at: float) -> dict[str, Any]:
    return {key: copy.deepcopy(artifact.get(key)) for key in ("domain", "version", "nonce", "binding", "binding_hash", "created_at", "expires_at")} | {"reviewer": reviewer, "approved_at": approved_at}


def _validate_artifact(artifact: dict[str, Any], clock: float) -> None:
    if artifact.get("domain") != DOMAIN or artifact.get("version") != 1 or not artifact.get("nonce"):
        raise FormError("form_approval_domain_invalid", "此审批不属于表单配置编辑器。")
    _validate_binding(artifact.get("binding"))
    if digest(artifact["binding"]) != artifact.get("binding_hash"):
        raise FormError("form_approval_changed", "审批目标、页面配置或请求清单已经改变。")
    try:
        created = float(artifact["created_at"])
        expires = float(artifact["expires_at"])
        valid = created <= clock < expires and 0 < expires - created <= 3600
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise FormError("form_approval_expired", "审批已过期或时间范围不合法，请重新准备。")


def _secret(value: bytes | str | None) -> bytes:
    actual = signing_secret() if value is None else value
    actual = actual.encode("utf-8") if isinstance(actual, str) else actual
    if not isinstance(actual, bytes) or len(actual) < 32:
        raise FormError("form_approval_secret_invalid", "本机审批签名密钥不合法。")
    return actual


def approve(artifact: dict[str, Any], secret: bytes | str | None = None, reviewer: str = "local-user", now: float | None = None) -> dict[str, Any]:
    """Called by a local click handler, never registered as an MCP tool."""
    clock = time.time() if now is None else float(now)
    _validate_artifact(artifact, clock)
    if not isinstance(reviewer, str) or not reviewer.strip():
        raise FormError("form_approval_reviewer_missing", "请记录本机人工审批者。")
    reviewer = reviewer.strip()
    signature = hmac.new(_secret(secret), canonical(_payload(artifact, reviewer, clock)).encode("utf-8"), hashlib.sha256).hexdigest()
    artifact["approval"] = {"reviewer": reviewer, "approved_at": clock, "signature": signature}
    return artifact


def verify(artifact: dict[str, Any], secret: bytes | str | None = None, expected_binding: dict[str, Any] | None = None, now: float | None = None) -> bool:
    clock = time.time() if now is None else float(now)
    _validate_artifact(artifact, clock)
    if expected_binding is not None and digest(expected_binding) != artifact["binding_hash"]:
        raise FormError("form_approval_binding_mismatch", "当前会话、草稿或请求与审批内容不一致。")
    approval = artifact.get("approval")
    if not isinstance(approval, dict) or not approval.get("reviewer"):
        raise FormError("form_approval_required", "需要在本机窗口点击人工批准。")
    try:
        approved_at = float(approval["approved_at"])
        if not float(artifact["created_at"]) <= approved_at <= clock:
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise FormError("form_approval_invalid", "人工审批时间不合法。") from exc
    expected = hmac.new(_secret(secret), canonical(_payload(artifact, approval["reviewer"], approved_at)).encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, str(approval.get("signature") or "")):
        raise FormError("form_approval_invalid", "审批签名不合法或内容已经改变。")
    return True
