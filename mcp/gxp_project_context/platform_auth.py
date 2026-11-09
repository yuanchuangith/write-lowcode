"""Existing GXP native login, with project-separated OS credentials."""
from __future__ import annotations

import base64
import json
from http.cookies import SimpleCookie
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import keyring

from .context import ProjectError, digest


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def credential_key(profile):
    binding = profile["platform_binding"]
    form = binding.get("form_config") or {}
    identity = {"environment": profile["environment"], "base_url": binding["platform_url"],
                "auth_header": form.get("auth_header") or "Cookie",
                "project_id": profile["project_id"], "database_scope": profile["project_id"],
                "platform_account": binding.get("account", ""), "app_id": binding.get("app_id", ""),
                "site_out_id": binding.get("site_out_id", ""), "company_id": form.get("company_id", ""),
                "org_identity_id": form.get("org_identity_id", "")}
    return str(form.get("credential_target") or "Codex.GxpFormEditor.Platform") + ":" + digest(identity)


def credential(profile):
    return keyring.get_password("GxpFormEditor", credential_key(profile)) or ""


def prefix(profile):
    value = str(profile["platform_binding"].get("form_config", {}).get("api_prefix") or "").rstrip("/")
    if value and (not value.startswith("/") or any(char in value for char in ("..", "?", "#", "\\", ":"))):
        raise ProjectError("PROJECT_PLATFORM_INVALID", "平台 API 前缀无效。")
    return value


def login_platform(profile, password: str, *, opener=None):
    from .settings import _secure_keyring
    _secure_keyring()
    binding = profile["platform_binding"]
    account = binding.get("account")
    if not account or not password:
        raise ProjectError("PROJECT_PLATFORM_LOGIN_REQUIRED", "请在本机窗口填写平台账号和密码。")
    body = urlencode({"account": "o" + base64.b64encode(account.encode()).decode(),
                      "password": "b" + base64.b64encode(password.encode()).decode()}).encode("ascii")
    request = Request(binding["platform_url"] + prefix(profile) + "/api/auth/login", data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"})
    try:
        with (opener or build_opener(NoRedirect)).open(request, timeout=15) as response:
            headers = response.headers.get_all("Set-Cookie") or []
            if not 200 <= response.status < 300:
                raise ValueError("login status")
            token = ""
            for header in headers:
                cookies = SimpleCookie()
                cookies.load(header)
                if "token" in cookies:
                    token = cookies["token"].value
        if not token or any(char in token for char in (";", "\r", "\n")):
            raise ValueError("missing token")
        cookie = "token=" + token
        form = binding.get("form_config") or {}
        for source, target in (("company_id", "companyId"), ("org_identity_id", "orgIdentityId")):
            value = str(form.get(source) or "")
            if any(char in value for char in (";", "\r", "\n")):
                raise ValueError("tenant context")
            if value:
                cookie += "; " + target + "=" + value
        keyring.set_password("GxpFormEditor", credential_key(profile), cookie)
        return {"ok": True, "authenticated": True}
    except Exception as exc:
        raise ProjectError("PROJECT_PLATFORM_LOGIN_FAILED", "平台登录未成功；项目配置保留，请核对本机账号及平台地址。") from exc
