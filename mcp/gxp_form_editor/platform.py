"""Independent, evidence-bound HTTP adapter for unpublished page configuration.

No protocol is inferred from the runtime/front endpoint.  A local administrator
must register the native designer contract and captured evidence first.  This
module never accepts a URL, credential, or request template through an MCP call.
"""
from __future__ import annotations

import copy
import base64
import hashlib
from http.cookies import SimpleCookie
import json
import os
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .common import FormError, canonical, digest


SAVE_PATH = "/inbiz/api/services/engines/v3/page"
DETAIL_PATH = SAVE_PATH + "/loadDetail"
ALLOWED_READ_PATHS = {SAVE_PATH, DETAIL_PATH, SAVE_PATH + "/loadTreeList", "/api/users/current", "/api/users/getcurrentuserinfo", "/api/dictionary"}
ALLOWED_WRITES = {
    ("PUT", SAVE_PATH),
    ("POST", "/api/functions/batch"),
    ("POST", "/api/pagemodels"),
    ("DELETE", "/api/pagemodels"),
    ("PUT", "/api/inbizdatas/page/lastmodifytime"),
}
EVIDENCE_KINDS = {"draft_read", "draft_isolation", "conditional_write", "association_sync", "actor"}
TARGET_FIELDS = ("environment", "base_url", "app_id", "site_out_id", "page_id", "route", "data_version_id", "platform")
SECRET_FIELDS = {"password", "token", "cookie", "authorization", "secret", "access_token", "refresh_token"}
SAVE_IDENTITIES = {"appId": "app_id", "siteOutId": "site_out_id", "outId": "page_id", "dataVersionId": "data_version_id"}
ASSOCIATION_PROTOCOLS = {
    ("POST", "/api/functions/batch"): "native-functions-batch-v1",
    ("POST", "/api/pagemodels"): "native-page-model-upsert-v1",
    ("DELETE", "/api/pagemodels"): "native-page-model-delete-v1",
    ("PUT", "/api/inbizdatas/page/lastmodifytime"): "native-page-lastmodifytime-v1",
}
PRESERVED_NATIVE_FIELDS = {
    "OnLoadBefore": "onLoadBefore", "OnLoaded": "onLoaded", "CssContent": "cssContent",
    "ComponentBiz": "componentBiz", "Name": "name", "LayoutInfo": "layoutInfo",
    "PageGroupOutId": "pageGroupOutId", "Sort": "sort", "ExtendType": "extendType",
}


def settings_path() -> Path:
    configured = os.environ.get("GXP_FORM_EDITOR_CONFIG")
    if configured:
        return Path(configured).expanduser().resolve()
    root = Path(os.environ.get("APPDATA") or Path.home() / ".config")
    return root / "GxpFormEditor" / "form-config.json"


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    location = Path(path) if path else settings_path()
    if not location.exists():
        return {}
    try:
        value = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FormError("form_settings_invalid", "表单编辑器本机连接设置无法读取。") from exc
    if not isinstance(value, dict):
        raise FormError("form_settings_invalid", "连接设置必须是 JSON 对象。")
    _reject_secrets(value)
    return value


def save_settings(value: dict[str, Any], path: str | Path | None = None) -> Path:
    _reject_secrets(value)
    if value.get("base_url"):
        _origin(str(value["base_url"]))
    location = Path(path) if path else settings_path()
    location.parent.mkdir(parents=True, exist_ok=True)
    temporary = location.with_suffix(location.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(location)
    return location


def _reject_secrets(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in SECRET_FIELDS:
                raise FormError("form_secret_in_settings", "密码和登录凭据只能保存在本机凭据库。")
            _reject_secrets(child)
    elif isinstance(value, list):
        for child in value:
            _reject_secrets(child)


def set_credential(config: dict[str, Any], value: str) -> None:
    import keyring
    target = _credential_key(config)
    keyring.set_password("GxpFormEditor", target, value)


def _credential(config: dict[str, Any]) -> str:
    import keyring
    target = _credential_key(config)
    return keyring.get_password("GxpFormEditor", target) or ""


def _credential_key(config: dict[str, Any]) -> str:
    reference = str(config.get("credential_target") or "Codex.GxpFormEditor.Platform")
    identity = {"environment": config.get("environment"), "base_url": config.get("base_url"), "auth_header": config.get("auth_header") or "Cookie"}
    if config.get("login_url"):
        identity["login_url"] = config["login_url"]
    return reference + ":" + digest(identity)


def _api_prefix(config: dict[str, Any]) -> str:
    prefix = str(config.get("api_prefix") or "").rstrip("/")
    if prefix and (not prefix.startswith("/") or any(value in prefix for value in ("..", "?", "#", "\\", ":"))):
        raise FormError("form_api_prefix_invalid", "GXP API 前缀不合法。")
    return prefix


def _login_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> dict[str, Any]:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            return {"status": response.status, "set_cookie": response.headers.get_all("Set-Cookie") or []}
    except HTTPError as exc:
        # Error responses and redirects never grant authentication.
        return {"status": exc.code, "set_cookie": []}
    except (URLError, OSError, TimeoutError) as exc:
        raise FormError("form_login_failed", "平台登录未取得成功响应，请检查地址和网络后重试。") from exc


def login(config: dict[str, Any], account: str, password: str, company_id: str = "", org_identity_id: str = "", *, transport: Callable[..., dict[str, Any]] | None = None) -> dict[str, Any]:
    """Local Qt-only login. Plain credentials never enter settings or MCP."""
    origin = _origin(str(config.get("base_url") or ""))
    prefix = _api_prefix(config)
    endpoint = str(config.get("login_url") or origin + prefix + "/api/auth/login")
    if config.get("login_url"):
        from gxp_project_context.platform_auth import validate_login_url
        try:
            validate_login_url(endpoint)
        except Exception as exc:
            raise FormError("form_login_url_invalid", "登录链接必须是完整的 HTTP(S) 接口地址，且不能包含凭据或查询参数。") from exc
    if not account or not password:
        raise FormError("form_login_fields_required", "请在本机登录窗口填写账号和密码。")
    for context in (company_id, org_identity_id):
        if any(char in context for char in (";", "\r", "\n")):
            raise FormError("form_login_context_invalid", "租户上下文格式不合法。")
    data = urlencode({"account": "o" + base64.b64encode(account.encode("utf-8")).decode("ascii"), "password": "b" + base64.b64encode(password.encode("utf-8")).decode("ascii")}).encode("ascii")
    try:
        result = (transport or _login_transport)(endpoint, {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "X-Requested-With": "XMLHttpRequest"}, data, 15)
    except FormError:
        raise
    except Exception as exc:
        raise FormError("form_login_failed", "平台登录失败，请检查连接及账号密码。") from exc
    if type(result.get("status")) is not int or not 200 <= result["status"] < 300:
        raise FormError("form_login_failed", "平台未确认登录成功，请检查账号密码。")
    token = ""
    cookies = result.get("set_cookie") or []
    if isinstance(cookies, str):
        cookies = [cookies]
    try:
        for header in cookies:
            parsed = SimpleCookie()
            parsed.load(header)
            if "token" in parsed:
                token = parsed["token"].value
    except (TypeError, ValueError):
        token = ""
    if not token or any(char in token for char in (";", "\r", "\n")):
        raise FormError("form_login_token_missing", "平台没有返回登录凭据，请核对账号密码。")
    cookie = "token=" + token
    if company_id:
        cookie += "; companyId=" + company_id
    if org_identity_id:
        cookie += "; orgIdentityId=" + org_identity_id
    set_credential(config, cookie)
    return {"authenticated": True, "credential_stored": True}


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise FormError("form_origin_invalid", "平台地址必须是无路径、无凭据的 HTTP(S) Origin。")
    try:
        parsed.port
    except ValueError as exc:
        raise FormError("form_origin_invalid", "平台端口不合法。") from exc
    return url.rstrip("/")


def _pointer(value: Any, pointer: str) -> Any:
    current = value
    if pointer == "":
        return copy.deepcopy(current)
    if not isinstance(pointer, str) or not pointer.startswith("/"):
        raise FormError("form_contract_invalid", "响应字段必须使用 JSON Pointer。")
    try:
        for part in pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            current = current[int(part)] if isinstance(current, list) else current[part]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise FormError("form_response_invalid", "平台响应不符合已登记的设计器协议。", {"pointer": pointer}) from exc
    return copy.deepcopy(current)


def _resolve(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, dict):
        if set(value) in ({"$"}, {"$json"}):
            field = value.get("$", value.get("$json"))
            current: Any = context
            try:
                for part in str(field).split("."):
                    current = current[part]
            except (KeyError, TypeError) as exc:
                raise FormError("form_contract_binding_missing", "请求模板引用的字段不存在。", {"field": field}) from exc
            return canonical(current) if "$json" in value else copy.deepcopy(current)
        return {key: _resolve(child, context) for key, child in value.items()}
    if isinstance(value, list):
        return [_resolve(child, context) for child in value]
    return value


def _resource_id(value: Any) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not all(char.isalnum() or char in "_-" for char in value):
        raise FormError("form_resource_id_invalid", "资源标识格式不合法。")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http_transport(method: str, url: str, headers: dict[str, str], body: Any, timeout: float) -> dict[str, Any]:
    data = None if body is None else canonical(body).encode("utf-8")
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            status = response.status
    except HTTPError as exc:
        raw = exc.read(8 * 1024 * 1024 + 1)
        status = exc.code
    except (URLError, TimeoutError, OSError) as exc:
        raise FormError("form_transport_uncertain", "平台请求未取得确定结果；请通过恢复核对，不得自动重发。") from exc
    if len(raw) > 8 * 1024 * 1024:
        raise FormError("form_response_too_large", "平台响应超过允许大小。")
    try:
        payload = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError) as exc:
        raise FormError("form_response_invalid", "平台没有返回预期的 JSON 响应。", {"status": status}) from exc
    return {"status": status, "body": payload}


class PlatformClient:
    """One registered environment; all public requests are credential-free plans."""

    def __init__(self, config: dict[str, Any] | None = None, credentials: Callable[[], str] | None = None, transport: Callable[..., dict[str, Any]] | None = None):
        self.config = copy.deepcopy(config if config is not None else load_settings())
        _reject_secrets(self.config)
        self.credentials = credentials or (lambda: _credential(self.config))
        self.transport = transport or _http_transport
        self.contract: dict[str, Any] = {}
        self.contract_hash = ""
        self._contract_root = Path.cwd()
        self._contract_path: Path | None = None
        self._load_error = ""
        path = self.config.get("contract_path")
        if path:
            try:
                location = Path(str(path)).expanduser().resolve()
                self._contract_path = location
                self._contract_root = location.parent
                self.contract = json.loads(location.read_text(encoding="utf-8"))
                if not isinstance(self.contract, dict):
                    raise ValueError("object required")
                _reject_secrets(self.contract)
                self.contract_hash = digest(self.contract)
            except (OSError, ValueError, FormError):
                self.contract = {}
                self._load_error = "native_contract_invalid"

    def _evidence_errors(self, required: set[str] | None = None) -> list[str]:
        errors: list[str] = []
        kinds: set[str] = set()
        for entry in self.contract.get("evidence", []):
            if not isinstance(entry, dict):
                errors.append("evidence_invalid")
                continue
            kind = entry.get("kind")
            try:
                path = (self._contract_root / str(entry["path"])).resolve()
                if not path.is_relative_to(self._contract_root) or not path.is_file():
                    raise ValueError()
                if path.stat().st_size > 8 * 1024 * 1024:
                    raise ValueError()
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != entry["sha256"] or not entry.get("description"):
                    raise ValueError()
                kinds.add(str(kind))
            except (OSError, KeyError, ValueError):
                errors.append("evidence_missing_or_changed:" + str(kind))
        errors.extend("evidence_required:" + kind for kind in sorted((EVIDENCE_KINDS if required is None else required) - kinds))
        return errors

    def readiness(self) -> dict[str, Any]:
        errors: list[str] = []
        try:
            _origin(str(self.config.get("base_url") or ""))
        except FormError:
            errors.append("connection_not_configured")
        contract = self.contract
        if self._load_error:
            errors.append(self._load_error)
        if self._contract_path:
            try:
                if digest(json.loads(self._contract_path.read_text(encoding="utf-8"))) != self.contract_hash:
                    errors.append("native_contract_changed_reload_required")
            except (OSError, ValueError):
                errors.append("native_contract_changed_reload_required")
        if contract.get("version") != 1:
            errors.append("native_contract_v1_required")
        for key in ("environment", "base_url", "platform"):
            if not self.config.get(key) or contract.get(key) != self.config.get(key):
                errors.append("contract_target_mismatch:" + key)
        errors.extend(self._evidence_errors({"draft_read"}))
        draft = contract.get("draft_read") or {}
        if draft.get("method") != "GET" or draft.get("path") not in {SAVE_PATH, DETAIL_PATH} or draft.get("authoritative_unpublished") is not True:
            errors.append("authoritative_draft_protocol_required")
        response = draft.get("response") or {}
        for key in ("page",):
            if key not in response:
                errors.append("draft_response_mapping_required:" + key)
        read_errors = list(errors)
        errors.extend(self._evidence_errors())
        for key in ("revision", "associated", "identity"):
            if key not in response:
                errors.append("draft_response_mapping_required:" + key)
        identity_fields = {"app_id", "site_out_id", "page_id", "route", "data_version_id"}
        if not identity_fields.issubset(response.get("identity") or {}):
            errors.append("write_identity_response_incomplete")
        if "actor" not in response and not contract.get("actor_read"):
            errors.append("actor_protocol_required")
        conditional = contract.get("conditional_write") or {}
        if conditional.get("server_enforced") is not True or conditional.get("location") not in {"header", "body"} or not conditional.get("name"):
            errors.append("server_conditional_write_required")
        if contract.get("draft_isolation_confirmed") is not True:
            errors.append("draft_isolation_unconfirmed")
        association = contract.get("association_sync") or {}
        if association.get("mode") not in {"server_atomic", "explicit_requests"}:
            errors.append("association_sync_unconfirmed")
        if "associated" not in (contract.get("verification") or {}):
            errors.append("associated_readback_contract_required")
        try:
            catalog = self.catalog()
            if not catalog.get("components") or catalog.get("available") is False:
                errors.append("native_catalog_required")
        except FormError:
            errors.append("native_catalog_invalid")
        requests = contract.get("write_requests")
        if not isinstance(requests, list) or not requests:
            errors.append("save_request_template_required")
        else:
            saves = 0
            for request in requests:
                pair = (request.get("method"), request.get("path")) if isinstance(request, dict) else (None, None)
                if pair not in ALLOWED_WRITES:
                    errors.append("write_path_not_allowed")
                saves += pair == ("PUT", SAVE_PATH)
                if pair == ("PUT", SAVE_PATH):
                    body = request.get("body")
                    if not isinstance(body, dict) or any(body.get(field) != {"$": "target." + key} for field, key in SAVE_IDENTITIES.items()):
                        errors.append("save_target_binding_required")
                    if not isinstance(body, dict) or body.get("component") not in ({"$": "candidate"}, {"$json": "candidate"}):
                        errors.append("save_candidate_binding_required")
                elif pair in ASSOCIATION_PROTOCOLS:
                    expected_scope = {"kind": "current_page", "app_id": {"$": "target.app_id"}, "page_id": {"$": "target.route"}}
                    if request.get("native_protocol") != ASSOCIATION_PROTOCOLS[pair] or request.get("scope") != expected_scope:
                        errors.append("association_target_scope_required")
            if saves != 1:
                errors.append("exactly_one_save_required")
            first = requests[0] if isinstance(requests[0], dict) else {}
            if (first.get("method"), first.get("path")) != ("PUT", SAVE_PATH):
                errors.append("conditional_page_save_must_be_first")
            if association.get("mode") == "explicit_requests" and len(requests) < 2:
                errors.append("association_requests_required")
        return {"available": not read_errors, "write_ready": not errors, "read_ready": not read_errors, "reasons": sorted(set(errors)), "read_reasons": sorted(set(read_errors)), "adapter_hash": self.contract_hash, "protocol_source": "local_evidence_bound_contract", "verified_by_plugin": False}

    def _require_ready(self, *, write: bool = True) -> None:
        readiness = self.readiness()
        if not readiness["write_ready" if write else "read_ready"]:
            raise FormError("form_adapter_not_ready", "原生草稿、并发或关联保存协议尚未完整核实，写入保持关闭。", readiness)

    def _target(self, target: dict[str, Any]) -> dict[str, Any]:
        missing = [key for key in TARGET_FIELDS if not target.get(key)]
        if missing:
            raise FormError("form_target_incomplete", "必须固定页面、环境及数据版本。", {"missing": missing})
        for key in ("environment", "base_url", "platform"):
            if target[key] != self.config.get(key):
                raise FormError("form_target_mismatch", "页面目标与本机已登记连接不一致。", {"field": key})
        _origin(target["base_url"])
        return copy.deepcopy(target)

    def _request(self, request: dict[str, Any], *, write: bool) -> dict[str, Any]:
        method = str(request.get("method") or "GET").upper()
        path = str(request.get("path") or "")
        prefix = _api_prefix(self.config)
        logical_path = path[len(prefix):] if prefix and path.startswith(prefix + "/api/") else path
        dynamic_resource = False
        for resource_prefix in ("/api/datasets/", "/api/dictionary/items/"):
            if logical_path.startswith(resource_prefix):
                try:
                    _resource_id(unquote(logical_path[len(resource_prefix):]))
                    dynamic_resource = True
                except FormError:
                    pass
        if (write and (method, logical_path) not in ALLOWED_WRITES) or (not write and (method != "GET" or not (logical_path in ALLOWED_READ_PATHS or dynamic_resource))):
            raise FormError("form_request_not_allowed", "该请求不属于登记的表单配置协议。")
        if path.startswith("/api/"):
            path = prefix + path
        if any(char in path for char in ("?", "#", "\\")) or ".." in path:
            raise FormError("form_request_not_allowed", "请求路径不合法。")
        headers = {str(key): str(value) for key, value in (request.get("headers") or {}).items()}
        if any(key.lower() in SECRET_FIELDS or key.lower() in {"host", "proxy-authorization"} for key in headers):
            raise FormError("form_secret_in_request", "请求计划不得包含凭据。")
        if any("\r" in key + value or "\n" in key + value for key, value in headers.items()):
            raise FormError("form_request_invalid", "请求头含非法换行。")
        statuses = request.get("expected_statuses") or [200, 204]
        if not isinstance(statuses, list) or not all(type(status) is int and 200 <= status < 300 for status in statuses):
            raise FormError("form_contract_invalid", "成功响应状态必须明确限定为 2xx。")
        return {"id": str(request.get("id") or "request"), "method": method, "path": path, "query": copy.deepcopy(request.get("query") or {}), "headers": headers, "body": copy.deepcopy(request.get("body")), "expected_statuses": statuses, "success": copy.deepcopy(request.get("success")), "native_protocol": request.get("native_protocol"), "scope": copy.deepcopy(request.get("scope")), "target_scope": copy.deepcopy(request.get("target_scope")), "expected_revision": copy.deepcopy(request.get("expected_revision"))}

    def _perform(self, request: dict[str, Any], *, write: bool) -> dict[str, Any]:
        validated = self._request(request, write=write)
        origin = _origin(str(self.config.get("base_url") or ""))
        url = origin + validated["path"]
        if validated["query"]:
            url += "?" + urlencode(validated["query"], doseq=True)
        headers = {"Accept": "application/json", "Content-Type": "application/json", **validated["headers"]}
        credential = self.credentials()
        if not credential:
            raise FormError("form_credential_missing", "请在独立连接窗口填写平台登录凭据。")
        auth_header = str(self.config.get("auth_header") or "Cookie")
        if auth_header not in {"Cookie", "Authorization"} or "\n" in credential or "\r" in credential:
            raise FormError("form_credential_invalid", "本机登录凭据格式不合法。")
        headers[auth_header] = credential
        try:
            result = self.transport(validated["method"], url, headers, validated["body"], max(1, min(float(self.config.get("timeout_seconds", 15)), 60)))
        except FormError:
            raise
        except Exception as exc:
            raise FormError("form_transport_uncertain", "平台请求未取得确定结果，禁止自动重发。") from exc
        status = result.get("status")
        if status not in validated["expected_statuses"]:
            if status in {409, 412}:
                raise FormError("form_revision_conflict", "页面已被其他操作修改，请重新读取并准备审批。", {"status": status})
            code = "form_transport_uncertain" if write and (not isinstance(status, int) or status >= 500 or 300 <= status < 400) else "form_platform_rejected"
            raise FormError(code, "平台请求没有取得预期的成功响应。", {"status": status})
        # Never return raw headers or errors that can contain credentials.
        body = copy.deepcopy(result.get("body"))
        success = validated.get("success")
        if success and _pointer(body, success["pointer"]) != success["equals"]:
            raise FormError("form_platform_rejected", "平台未确认请求处理成功。", {"status": status})
        return {"status": status, "body": body}

    def read_draft(self, target: dict[str, Any]) -> dict[str, Any]:
        self._require_ready(write=False)
        target = self._target(target)
        spec = self.contract["draft_read"]
        request = _resolve({key: value for key, value in spec.items() if key not in {"response", "authoritative_unpublished"}}, {"target": target})
        response = self._perform(request, write=False)["body"]
        mapping = spec["response"]
        page = _pointer(response, mapping["page"])
        if mapping.get("page_json_string"):
            try:
                page = json.loads(page)
            except (TypeError, ValueError) as exc:
                raise FormError("form_response_invalid", "草稿 Component 不是合法 JSON。") from exc
        identity = {key: _pointer(response, pointer) for key, pointer in (mapping.get("identity") or {}).items()}
        for key, actual in identity.items():
            if actual != target.get(key):
                raise FormError("form_draft_identity_mismatch", "草稿响应不是指定页面及版本。", {"field": key})
        server_revision = _pointer(response, mapping["revision"]) if mapping.get("revision") else None
        revision = server_revision if server_revision not in (None, "") else digest(page)
        actor = _pointer(response, mapping["actor"]) if mapping.get("actor") else None
        actor_read = self.contract.get("actor_read")
        if actor is None and actor_read:
            actor_request = _resolve({key: value for key, value in actor_read.items() if key != "response"}, {"target": target})
            actor = _pointer(self._perform(actor_request, write=False)["body"], actor_read["response"])
        associated = _pointer(response, mapping["associated"]) if mapping.get("associated") else None
        if not isinstance(page, dict):
            raise FormError("form_draft_incomplete", "草稿响应缺少有效页面配置。")
        return {"page": page, "identity": {**target, **identity}, "revision": revision, "revision_kind": "server" if server_revision not in (None, "") else "local_content_hash", "associated": associated, "actor": actor, "native": copy.deepcopy(response), "source_binding": {"adapter_hash": self.contract_hash, "request_hash": digest(request), "source": "native_designer_draft", "server_concurrency": server_revision not in (None, ""), "contract_evidence_hash": digest(self.contract.get("evidence", []))}}

    def build_requests(self, snapshot: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
        self._require_ready()
        target = self._target(snapshot["identity"])
        if snapshot.get("source_binding", {}).get("adapter_hash") != self.contract_hash:
            raise FormError("form_adapter_changed", "草稿读取后原生协议已改变。")
        if snapshot.get("revision") in (None, ""):
            raise FormError("form_revision_missing", "缺少服务端并发标识。")
        if snapshot.get("revision_kind") == "local_content_hash":
            raise FormError("form_server_revision_missing", "本地内容哈希只能检查预览变化，不能替代服务端条件写入。")
        context = {"snapshot": snapshot, "target": target, "candidate": candidate}
        requests = [self._request(_resolve(template, context), write=True) for template in self.contract["write_requests"]]
        conditional = self.contract["conditional_write"]
        save = next(item for item in requests if item["method"] == "PUT" and item["path"] == SAVE_PATH)
        save["expected_revision"] = copy.deepcopy(snapshot["revision"])
        if conditional["location"] == "header":
            save["headers"][conditional["name"]] = str(snapshot["revision"])
        else:
            if not isinstance(save["body"], dict):
                raise FormError("form_contract_invalid", "并发字段要求保存请求体为对象。")
            save["body"][conditional["name"]] = copy.deepcopy(snapshot["revision"])
        for request in requests:
            request["target_scope"] = copy.deepcopy(target)
            self._validate_write_scope(request, target)
        body = save["body"]
        component = body["component"]
        if isinstance(component, str):
            try:
                component = json.loads(component)
            except (ValueError, TypeError) as exc:
                raise FormError("form_save_candidate_mismatch", "保存请求中的 Component 无法解析。") from exc
        if digest(component) != digest(candidate):
            raise FormError("form_save_candidate_mismatch", "保存请求没有使用完整已审批候选配置。")
        native = snapshot.get("native") or {}
        for source_field, body_field in PRESERVED_NATIVE_FIELDS.items():
            if source_field in native and (body_field not in body or digest(body[body_field]) != digest(native[source_field])):
                raise FormError("form_native_field_changed", "表单属性修改不得改变原生脚本、样式或页面元数据。", {"field": body_field})
        return requests

    def _validate_write_scope(self, request: dict[str, Any], target: dict[str, Any]) -> None:
        self._target(target)
        prefix = _api_prefix(self.config)
        path = request["path"]
        logical_path = path[len(prefix):] if prefix and path.startswith(prefix + "/api/") else path
        pair = (request["method"], logical_path)
        if pair == ("PUT", SAVE_PATH):
            body = request.get("body")
            if not isinstance(body, dict) or any(body.get(field) != target[key] for field, key in SAVE_IDENTITIES.items()):
                raise FormError("form_save_target_mismatch", "保存请求不是当前固定页面及草稿版本。")
            if "component" not in body:
                raise FormError("form_save_candidate_mismatch", "保存请求缺少候选 Component。")
            if body.get("wfKey") not in (None, ""):
                raise FormError("form_save_scope_unverified", "当前表单草稿保存不允许附带流程操作。")
            conditional = self.contract["conditional_write"]
            expected = request.get("expected_revision")
            actual = request.get("headers", {}).get(conditional["name"]) if conditional["location"] == "header" else body.get(conditional["name"])
            if expected in (None, "") or actual != (str(expected) if conditional["location"] == "header" else expected):
                raise FormError("form_save_concurrency_missing", "保存请求必须携带已审批的服务端并发条件。")
            return
        if pair not in ASSOCIATION_PROTOCOLS or request.get("native_protocol") != ASSOCIATION_PROTOCOLS[pair]:
            raise FormError("form_association_scope_unverified", "关联请求缺少已核实的原生目标协议。")
        if request.get("scope") != {"kind": "current_page", "app_id": target["app_id"], "page_id": target["route"]}:
            raise FormError("form_association_target_mismatch", "关联请求作用域不是当前页面。")
        if pair == ("POST", "/api/functions/batch"):
            body = request.get("body")
            rows = body.get("function") if isinstance(body, dict) else None
            if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) or row.get("appId") != target["app_id"] or row.get("pageId") != target["route"] for row in rows):
                raise FormError("form_association_target_mismatch", "功能权限同步的每一项都必须固定到当前页面。")
        elif pair == ("POST", "/api/pagemodels"):
            body = request.get("body")
            if not isinstance(body, dict) or body.get("AppId") != target["app_id"] or body.get("PageId") != target["route"]:
                raise FormError("form_association_target_mismatch", "页面模型关联不是当前页面。")
        else:
            query = request.get("query") or {}
            if query.get("appId") != target["app_id"] or query.get("pageId") != target["route"] or request.get("body") is not None:
                raise FormError("form_association_target_mismatch", "关联清理或修改时间请求不是当前页面。")

    def execute_request(self, request: dict[str, Any]) -> dict[str, Any]:
        self._require_ready()
        if not isinstance(request.get("target_scope"), dict):
            raise FormError("form_write_scope_required", "执行请求必须绑定已审批页面的完整作用域。")
        self._validate_write_scope(self._request(request, write=True), request["target_scope"])
        return self._perform(request, write=True)

    def verify_saved(self, baseline: dict[str, Any], candidate: dict[str, Any], fresh: dict[str, Any], requests: list[dict[str, Any]]) -> bool:
        """Readback must include the expected associated state, not merely 2xx."""
        self._require_ready()
        if baseline.get("identity") != fresh.get("identity") or baseline.get("actor") != fresh.get("actor"):
            return False
        if fresh.get("source_binding", {}).get("adapter_hash") != self.contract_hash or digest(fresh.get("page")) != digest(candidate):
            return False
        if digest(requests) != digest(self.build_requests(baseline, candidate)):
            return False
        verification = self.contract.get("verification") or {}
        if "associated" not in verification or fresh.get("associated") is None:
            return False
        expected = _resolve(verification["associated"], {"snapshot": baseline, "candidate": candidate, "target": baseline["identity"]})
        return digest(expected) == digest(fresh["associated"])

    def catalog(self) -> dict[str, Any]:
        spec = self.contract.get("catalog") or {}
        if not spec:
            return {"components": {}, "available": False, "reason": "native_catalog_not_registered"}
        try:
            path = (self._contract_root / str(spec["path"])).resolve()
            if not path.is_relative_to(self._contract_root):
                raise ValueError()
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != spec["sha256"]:
                raise ValueError()
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError()
            if value.get("platform") != self.config.get("platform") or not value.get("component_version") or not value.get("source_fingerprint"):
                raise ValueError()
            if spec.get("component_version") != value["component_version"] or spec.get("source_fingerprint") != value["source_fingerprint"]:
                raise ValueError()
            exports = spec.get("native_exports") or []
            for export in exports:
                native = (self._contract_root / str(export["path"])).resolve()
                if not native.is_relative_to(self._contract_root) or hashlib.sha256(native.read_bytes()).hexdigest() != export["sha256"]:
                    raise ValueError()
            return value
        except (OSError, KeyError, ValueError) as exc:
            raise FormError("form_catalog_invalid", "本机原生组件目录缺失或与登记哈希不一致。") from exc

    def read_resource(self, snapshot: dict[str, Any], kind: str, resource_id: str) -> dict[str, Any]:
        """Read metadata, never resource business rows or editable entities."""
        self._require_ready(write=False)
        target = self._target(snapshot["identity"])
        resource_id = _resource_id(resource_id)
        if kind not in {"datasets", "dictionaries"}:
            raise FormError("form_resource_kind_invalid", "仅支持数据集和字典元数据。")
        spec = (self.contract.get("resource_resolvers") or {}).get(kind) or {}
        expected_protocol = "native-dataset-v1" if kind == "datasets" else "native-dictionary-v1"
        evidence_kind = "dataset_read" if kind == "datasets" else "dictionary_read"
        if spec.get("protocol") != expected_protocol or self._evidence_errors({evidence_kind}):
            raise FormError("form_resource_protocol_required", "该资源的原生读取协议尚未登记及核实。", {"kind": kind})
        headers = {"appId": target["app_id"], "siteOutId": target["site_out_id"]}
        if kind == "datasets":
            response = self._perform({"method": "GET", "path": "/api/datasets/" + quote(resource_id, safe=""), "headers": headers}, write=False)["body"]
            row = _pointer(response, spec.get("response", "/data"))
            if not isinstance(row, dict) or row.get("id") != resource_id or row.get("lastModificationTime") is None:
                raise FormError("form_resource_response_invalid", "数据集响应缺少正确标识或原生版本。")
            row = copy.deepcopy(row)
            if "configModel" not in row:
                try:
                    row["configModel"] = json.loads(row["config"]) if isinstance(row.get("config"), str) else copy.deepcopy(row["config"])
                except (ValueError, KeyError, TypeError) as exc:
                    raise FormError("form_resource_response_invalid", "数据集原生配置无法解析。") from exc
            row["version"] = row["lastModificationTime"]
            row["version_kind"] = "native_last_modification_time"
            return row
        metadata_response = self._perform({"method": "GET", "path": "/api/dictionary", "query": {"keys": resource_id, "name": ""}, "headers": headers}, write=False)["body"]
        metadata = _pointer(metadata_response, spec.get("metadata_response", "/data"))
        if not isinstance(metadata, list):
            raise FormError("form_resource_response_invalid", "字典目录响应无效。")
        matches = [entry for entry in metadata if isinstance(entry, dict) and entry.get("key") == resource_id]
        if len(matches) != 1:
            raise FormError("form_resource_response_invalid", "字典标识不存在或不唯一。")
        item_response = self._perform({"method": "GET", "path": "/api/dictionary/items/" + quote(resource_id, safe=""), "headers": headers}, write=False)["body"]
        raw_items = _pointer(item_response, spec.get("items_response", "/data"))
        if not isinstance(raw_items, list) or any(not isinstance(item, dict) or not {"id", "text", "value"}.issubset(item) for item in raw_items):
            raise FormError("form_resource_response_invalid", "字典项响应不完整。")
        items = [{"Id": item["id"], "Text": item["text"], "Value": item["value"], **({"ColumnSort": item["columnsort"]} if "columnsort" in item else {})} for item in raw_items]
        return {"id": resource_id, "key": resource_id, "native_id": matches[0].get("id"), "name": matches[0].get("name"), "version": digest({"metadata": matches[0], "items": raw_items}), "version_kind": "content_hash", "items": items}

    def resources(self, snapshot: dict[str, Any], operations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        from .schema import inspect_component
        result: dict[str, Any] = {"available": True, "datasets": {}, "dictionaries": {}}
        operations = operations or []
        relevant = [op for op in operations if op.get("operation") in {"set_binding", "set_default"}]
        if not relevant:
            return result
        catalog = self.catalog()
        for op in relevant:
            component = inspect_component(snapshot["page"], op.get("component_id", ""))
            entry = catalog.get("components", {}).get(component["component_type"], {})
            descriptor = entry.get("properties", {}).get(op.get("path"), {})
            adapter = entry.get("adapters", {}).get(descriptor.get("adapter") or op["operation"], {})
            contract = adapter.get("contract")
            if op["operation"] == "set_binding":
                if contract in {"native-dataset-v1", "dynamic-list-dataset-v1"}:
                    kind = "datasets"
                elif contract == "native-dictionary-v1":
                    kind = "dictionaries"
                else:
                    continue  # The pure schema validator reports unknown setter.
                resource_id = op.get("value", {}).get("id") if isinstance(op.get("value"), dict) else None
            elif contract == "native-dictionary-default-v1":
                kind = "dictionaries"
                resource_id = _pointer(component["schema"], adapter.get("dictionary_id_path", ""))
            else:
                continue
            resource_id = _resource_id(resource_id)
            if resource_id not in result[kind]:
                result[kind][resource_id] = self.read_resource(snapshot, kind, resource_id)
        return result
