"""Existing GXP native login, with project-separated OS credentials."""
from __future__ import annotations

import base64
import json
from http.cookies import SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
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
    if binding.get("login_url") or form.get("login_url"):
        identity["login_url"] = login_url(profile)
    return str(form.get("credential_target") or "Codex.GxpFormEditor.Platform") + ":" + digest(identity)


def credential(profile):
    return keyring.get_password("GxpFormEditor", credential_key(profile)) or ""


def prefix(profile):
    value = str(profile["platform_binding"].get("form_config", {}).get("api_prefix") or "").rstrip("/")
    if value and (not value.startswith("/") or any(char in value for char in ("..", "?", "#", "\\", ":"))):
        raise ProjectError("PROJECT_PLATFORM_INVALID", "平台 API 前缀无效。")
    return value


def validate_login_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 33 for char in value):
        raise ProjectError("PROJECT_PLATFORM_LOGIN_URL_INVALID", "登录链接必须是完整的 HTTP(S) 接口地址，且不能包含空白或凭据。")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ProjectError("PROJECT_PLATFORM_LOGIN_URL_INVALID", "登录链接必须是完整的 HTTP(S) 接口地址，且不能包含凭据、查询参数或片段。")
    return value


def login_url(profile):
    binding = profile["platform_binding"]
    explicit = binding.get("login_url") or (binding.get("form_config") or {}).get("login_url")
    return validate_login_url(explicit or binding["platform_url"].rstrip("/") + prefix(profile) + "/api/auth/login")


def _http_login_error(status):
    details = {"http_status": status}
    if status == 404:
        return ProjectError("PROJECT_PLATFORM_ENDPOINT_NOT_FOUND",
                            "平台登录接口返回 HTTP 404（接口不存在）。请核对“登录链接”、“项目网页地址”和“API 前缀”；项目配置已保留。", details)
    if status in (401, 403):
        return ProjectError("PROJECT_PLATFORM_AUTH_REJECTED",
                            f"平台拒绝登录（HTTP {status}）。请核对平台账号及访问权限；项目配置已保留。", details)
    if 300 <= status < 400:
        return ProjectError("PROJECT_PLATFORM_REDIRECT",
                            f"平台登录接口返回重定向（HTTP {status}）。请核对项目的直接访问地址；项目配置已保留。", details)
    return ProjectError("PROJECT_PLATFORM_HTTP_ERROR",
                        f"平台登录接口返回 HTTP {status}；项目配置已保留，请检查平台服务。", details)


def request_login(endpoint, account, password, *, opener=None, timeout=15):
    """Internal native login for project checks and CPM; token stays in-process."""
    endpoint = validate_login_url(endpoint)
    if not account or not password:
        raise ProjectError("PROJECT_PLATFORM_LOGIN_REQUIRED", "请在本机窗口填写平台账号和密码。")
    body = urlencode({"account": "o" + base64.b64encode(account.encode()).decode(),
                      "password": "b" + base64.b64encode(password.encode()).decode()}).encode("ascii")
    request = Request(endpoint, data=body,
                      headers={"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"})
    try:
        with (opener or build_opener(NoRedirect)).open(request, timeout=timeout) as response:
            headers = response.headers.get_all("Set-Cookie") or []
            if not 200 <= response.status < 300:
                raise _http_login_error(response.status)
            status = response.status
            token = ""
            for header in headers:
                cookies = SimpleCookie()
                cookies.load(header)
                if "token" in cookies:
                    token = cookies["token"].value
    except HTTPError as exc:
        raise _http_login_error(exc.code) from exc
    except TimeoutError as exc:
        raise ProjectError("PROJECT_PLATFORM_TIMEOUT",
                           "平台登录请求超时；项目配置已保留，请检查网络和平台服务。") from exc
    except URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ProjectError("PROJECT_PLATFORM_TIMEOUT",
                               "平台登录请求超时；项目配置已保留，请检查网络和平台服务。") from exc
        raise ProjectError("PROJECT_PLATFORM_UNREACHABLE",
                           "无法连接平台登录地址；项目配置已保留，请检查地址、网络和证书。") from exc
    except ProjectError:
        raise
    except Exception as exc:
        raise ProjectError("PROJECT_PLATFORM_LOGIN_FAILED",
                           "平台登录请求未完成；项目配置已保留，请检查平台地址和本机登录配置。") from exc
    if not token:
        raise ProjectError("PROJECT_PLATFORM_TOKEN_MISSING",
                           f"平台响应未返回登录凭据（HTTP {status}）。请核对平台账号、密码及登录方式；项目配置已保留。",
                           {"http_status": status})
    if any(char in token for char in (";", "\r", "\n")):
        raise ProjectError("PROJECT_PLATFORM_AUTH_CONTEXT_INVALID", "平台登录凭据格式无效；项目配置已保留。")
    return token


def login_platform(profile, password: str, *, opener=None):
    from .settings import _secure_keyring
    _secure_keyring()
    binding = profile["platform_binding"]
    token = request_login(login_url(profile), binding.get("account"), password, opener=opener)
    try:
        cookie = "token=" + token
        form = binding.get("form_config") or {}
        for source, target in (("company_id", "companyId"), ("org_identity_id", "orgIdentityId")):
            value = str(form.get(source) or "")
            if any(char in value for char in (";", "\r", "\n")):
                raise ValueError("tenant context")
            if value:
                cookie += "; " + target + "=" + value
    except Exception as exc:
        raise ProjectError("PROJECT_PLATFORM_AUTH_CONTEXT_INVALID",
                           "平台登录凭据或租户参数格式无效；项目配置已保留，请核对本机登录配置。") from exc
    try:
        keyring.set_password("GxpFormEditor", credential_key(profile), cookie)
    except Exception as exc:
        raise ProjectError("PROJECT_PLATFORM_CREDENTIAL_SAVE_FAILED",
                           "平台已返回登录凭据，但未能保存到本机凭据库；项目配置已保留，请检查本机凭据服务。") from exc
    return {"ok": True, "authenticated": True}
