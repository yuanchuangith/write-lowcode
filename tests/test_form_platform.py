"""Offline protocol contract tests; fixtures never assert production CAS support."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs
import base64

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
from gxp_form_editor.common import FormError, digest
from gxp_form_editor.platform import DETAIL_PATH, EVIDENCE_KINDS, PlatformClient, SAVE_PATH, load_settings, login, save_settings


class PlatformContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.target = {"environment": "offline-test", "base_url": "http://localhost:61234", "app_id": "app-test", "site_out_id": "site-test", "page_id": "page-test", "route": "route-test", "data_version_id": "draft-test", "platform": "web"}
        self.config = {key: self.target[key] for key in ("environment", "base_url", "platform")}
        self.config.update({"contract_path": str(self.root / "contract.json"), "api_prefix": "/gxp2"})
        evidence = b"Synthetic offline fixture; does not verify any deployed server."
        (self.root / "evidence.txt").write_bytes(evidence)
        self.catalog = {"schema_version": 1, "platform": "web", "component_version": "offline-version", "source_fingerprint": "offline-source", "components": {"EformInput": {"properties": {}}}}
        catalog_bytes = json.dumps(self.catalog).encode("utf-8")
        (self.root / "catalog.json").write_bytes(catalog_bytes)
        self.contract = {
            "version": 1, **{key: self.config[key] for key in ("environment", "base_url", "platform")},
            "evidence": [{"kind": kind, "path": "evidence.txt", "sha256": hashlib.sha256(evidence).hexdigest(), "description": "Synthetic fixture only"} for kind in EVIDENCE_KINDS],
            "draft_read": {"method": "GET", "path": DETAIL_PATH, "authoritative_unpublished": True,
                "query": {"outId": {"$": "target.page_id"}, "dataVersionId": {"$": "target.data_version_id"}},
                "response": {"page": "/Component", "page_json_string": True, "revision": "/Revision", "actor": "/Actor", "associated": "/Associated", "identity": {key: "/Identity/" + key for key in ("app_id", "site_out_id", "page_id", "route", "data_version_id")}}},
            "draft_isolation_confirmed": True,
            "conditional_write": {"location": "header", "name": "If-Match", "server_enforced": True},
            "association_sync": {"mode": "server_atomic"},
            "verification": {"associated": {"$": "snapshot.associated"}},
            "catalog": {"path": "catalog.json", "sha256": hashlib.sha256(catalog_bytes).hexdigest(), "component_version": "offline-version", "source_fingerprint": "offline-source"},
            "write_requests": [{"id": "save", "method": "PUT", "path": SAVE_PATH, "body": {"appId": {"$": "target.app_id"}, "siteOutId": {"$": "target.site_out_id"}, "outId": {"$": "target.page_id"}, "dataVersionId": {"$": "target.data_version_id"}, "component": {"$json": "candidate"}}}],
        }
        self.response = {"Component": json.dumps({"content": {"schema": {"type": "object", "properties": {}}}}), "Revision": '"test-revision-1"', "Actor": {"id": "offline-user"}, "Associated": {"functions": [], "models": []}, "Identity": self.target}
        self.calls = []

    def client(self):
        (self.root / "contract.json").write_text(json.dumps(self.contract), encoding="utf-8")
        def transport(method, url, headers, body, timeout):
            self.calls.append({"method": method, "url": url, "headers": headers, "body": body})
            return {"status": 200, "body": self.response if method == "GET" else {"ok": True}}
        return PlatformClient(self.config, credentials=lambda: "token=offline-only", transport=transport)

    def assert_code(self, code, function):
        with self.assertRaises(FormError) as caught:
            function()
        self.assertEqual(code, caught.exception.code)

    def save_request(self):
        return {"method": "PUT", "path": SAVE_PATH, "target_scope": self.target, "expected_revision": '"test-revision-1"', "headers": {"If-Match": '"test-revision-1"'}, "body": {"appId": self.target["app_id"], "siteOutId": self.target["site_out_id"], "outId": self.target["page_id"], "dataVersionId": self.target["data_version_id"], "component": "{}"}}

    def test_default_connection_never_enables_write(self):
        result = PlatformClient({}).readiness()
        self.assertFalse(result["write_ready"])
        self.assertFalse(result["read_ready"])

    def test_native_read_is_target_bound_and_request_plan_has_no_credentials(self):
        client = self.client()
        snapshot = client.read_draft(self.target)
        self.assertEqual("server", snapshot["revision_kind"])
        requests = client.build_requests(snapshot, {"content": {"schema": {"properties": {}}}})
        self.assertEqual('"test-revision-1"', requests[0]["headers"]["If-Match"])
        self.assertNotIn("offline-only", json.dumps(requests))
        self.assertIn("dataVersionId=draft-test", self.calls[0]["url"])

    def test_captured_style_response_without_cas_is_read_only(self):
        mapping = self.contract["draft_read"]["response"]
        for key in ("revision", "actor", "associated"):
            mapping.pop(key)
        self.contract.pop("conditional_write")
        client = self.client()
        self.assertTrue(client.readiness()["read_ready"])
        self.assertFalse(client.readiness()["write_ready"])
        snapshot = client.read_draft(self.target)
        self.assertEqual("local_content_hash", snapshot["revision_kind"])
        self.assertFalse(snapshot["source_binding"]["server_concurrency"])
        self.assert_code("form_adapter_not_ready", lambda: client.build_requests(snapshot, snapshot["page"]))

    def test_front_runtime_endpoint_cannot_be_registered_as_draft(self):
        self.contract["draft_read"]["path"] = "/inbiz/api/services/front/engines/v3/page"
        client = self.client()
        self.assertFalse(client.readiness()["read_ready"])
        self.assert_code("form_adapter_not_ready", lambda: client.read_draft(self.target))
        self.assertEqual([], self.calls)

    def test_origin_or_version_mismatch_is_rejected(self):
        client = self.client()
        wrong = {**self.target, "base_url": "http://other.invalid"}
        self.assert_code("form_target_mismatch", lambda: client.read_draft(wrong))
        self.response["Identity"]["data_version_id"] = "another-draft"
        self.assert_code("form_draft_identity_mismatch", lambda: client.read_draft({**self.target, "data_version_id": "draft-test"}))

    def test_evidence_or_contract_change_blocks_further_io(self):
        client = self.client()
        self.assertTrue(client.readiness()["write_ready"])
        (self.root / "evidence.txt").write_text("changed", encoding="utf-8")
        self.assertFalse(client.readiness()["write_ready"])
        self.assert_code("form_adapter_not_ready", lambda: client.read_draft(self.target))
        client = self.client()
        (self.root / "contract.json").write_text("{}", encoding="utf-8")
        self.assertIn("native_contract_changed_reload_required", client.readiness()["reasons"])

    def test_publish_arbitrary_urls_and_request_credentials_are_rejected(self):
        client = self.client()
        for request in (
            {"method": "POST", "path": "/inbiz/api/services/engines/v3/appSite/publish"},
            {"method": "PUT", "path": "http://other.invalid/"},
            {"method": "PUT", "path": SAVE_PATH, "headers": {"Cookie": "leak"}},
        ):
            with self.assertRaises(FormError):
                client.execute_request(request)
        self.assertEqual([], self.calls)

    def test_prefix_maps_only_known_gxp_routes(self):
        client = self.client()
        client.execute_request({"method": "POST", "path": "/api/functions/batch", "body": {"function": [{"appId": self.target["app_id"], "pageId": self.target["route"]}]}, "native_protocol": "native-functions-batch-v1", "scope": {"kind": "current_page", "app_id": self.target["app_id"], "page_id": self.target["route"]}, "target_scope": self.target})
        self.assertEqual("http://localhost:61234/gxp2/api/functions/batch", self.calls[-1]["url"])
        self.assert_code("form_request_not_allowed", lambda: client.execute_request({"method": "PUT", "path": "/inbiz/api/services/engines/v3/appSite/extends", "target_scope": self.target}))

    def test_server_conflict_and_timeout_do_not_retry(self):
        client = self.client()
        def conflict(*args):
            self.calls.append(1)
            return {"status": 412, "body": {"error": "secret response is not exposed"}}
        client.transport = conflict
        self.assert_code("form_revision_conflict", lambda: client.execute_request(self.save_request()))
        self.assertEqual(1, len(self.calls))
        def timeout(*args):
            self.calls.append(1)
            raise TimeoutError("token=should-not-appear")
        client.transport = timeout
        self.assert_code("form_transport_uncertain", lambda: client.execute_request(self.save_request()))
        self.assertEqual(2, len(self.calls))

    def test_saved_check_requires_complete_associated_readback(self):
        client = self.client()
        baseline = client.read_draft(self.target)
        candidate = copy.deepcopy(baseline["page"])
        requests = client.build_requests(baseline, candidate)
        fresh = copy.deepcopy(baseline)
        self.assertTrue(client.verify_saved(baseline, candidate, fresh, requests))
        fresh["associated"]["functions"] = [{"unexpected": True}]
        self.assertFalse(client.verify_saved(baseline, candidate, fresh, requests))

    def test_settings_reject_secrets_and_roundtrip_independent_profile(self):
        path = self.root / "form-settings.json"
        self.assert_code("form_secret_in_settings", lambda: save_settings({"token": "never-store"}, path))
        save_settings(self.config, path)
        self.assertEqual(self.config, load_settings(path))

    def test_local_login_uses_native_encoding_and_only_returns_nonsecret_status(self):
        calls = []
        def transport(url, headers, body, timeout):
            calls.append((url, headers, parse_qs(body.decode("ascii"))))
            return {"status": 200, "set_cookie": ["token=offline-test-token; Path=/; HttpOnly"]}
        with patch("gxp_form_editor.platform.set_credential") as credential:
            result = login(self.config, "本机测试", "test-password", "test-company", "test-org", transport=transport)
        self.assertEqual({"authenticated": True, "credential_stored": True}, result)
        self.assertEqual("http://localhost:61234/gxp2/api/auth/login", calls[0][0])
        self.assertEqual("o" + base64.b64encode("本机测试".encode()).decode(), calls[0][2]["account"][0])
        self.assertEqual("b" + base64.b64encode(b"test-password").decode(), calls[0][2]["password"][0])
        credential.assert_called_once_with(self.config, "token=offline-test-token; companyId=test-company; orgIdentityId=test-org")
        self.assertNotIn("token", json.dumps(result))
        self.assertNotIn("test-password", json.dumps(result))

    def test_login_refuses_missing_cookie_redirect_or_cookie_injection(self):
        with patch("gxp_form_editor.platform.set_credential") as credential:
            for response in ({"status": 200, "set_cookie": []}, {"status": 302, "set_cookie": ["token=should-not-be-used"]}):
                with self.assertRaises(FormError):
                    login(self.config, "account", "password", transport=lambda *args: response)
            with self.assertRaises(FormError):
                login(self.config, "account", "password", "tenant; token=injected", transport=lambda *args: {})
            credential.assert_not_called()

    def enable_resource_protocols(self):
        for kind in ("dataset_read", "dictionary_read"):
            self.contract["evidence"].append({"kind": kind, "path": "evidence.txt", "sha256": hashlib.sha256((self.root / "evidence.txt").read_bytes()).hexdigest(), "description": "Synthetic metadata resolver fixture"})
        self.contract["resource_resolvers"] = {"datasets": {"protocol": "native-dataset-v1"}, "dictionaries": {"protocol": "native-dictionary-v1"}}

    def test_dataset_resolver_reads_current_native_version_without_cache(self):
        self.enable_resource_protocols()
        client = self.client()
        version = ["2026-01-01T00:00:00"]
        def transport(method, url, headers, body, timeout):
            self.calls.append(url)
            self.assertEqual("GET", method)
            self.assertTrue(url.endswith("/gxp2/api/datasets/dataset-test"))
            return {"status": 200, "body": {"data": {"id": "dataset-test", "lastModificationTime": version[0], "config": '{"queryFields":[]}', "primaryKey": "id", "model": "test_model", "name": "Test"}}}
        client.transport = transport
        snapshot = {"identity": self.target}
        first = client.read_resource(snapshot, "datasets", "dataset-test")
        self.assertEqual([], first["configModel"]["queryFields"])
        self.assertEqual(version[0], first["version"])
        version[0] = "2026-01-02T00:00:00"
        second = client.read_resource(snapshot, "datasets", "dataset-test")
        self.assertNotEqual(first["version"], second["version"])
        self.assertEqual(2, len(self.calls))

    def test_dictionary_key_and_content_version_include_current_item_metadata(self):
        self.enable_resource_protocols()
        client = self.client()
        text = ["Before"]
        def transport(method, url, headers, body, timeout):
            self.assertEqual("GET", method)
            if "/dictionary/items/" in url:
                payload = [{"id": "item-test", "text": text[0], "value": "value-test", "columnsort": 1, "lastModificationTime": "test"}]
            else:
                self.assertIn("keys=dictionary-key", url)
                payload = [{"id": "different-database-id", "key": "dictionary-key", "name": "Offline dictionary"}]
            return {"status": 200, "body": {"data": payload}}
        client.transport = transport
        first = client.read_resource({"identity": self.target}, "dictionaries", "dictionary-key")
        self.assertEqual("dictionary-key", first["id"])
        self.assertEqual("different-database-id", first["native_id"])
        self.assertEqual("content_hash", first["version_kind"])
        self.assertEqual("Before", first["items"][0]["Text"])
        text[0] = "After"
        second = client.read_resource({"identity": self.target}, "dictionaries", "dictionary-key")
        self.assertNotEqual(first["version"], second["version"])

    def test_resource_unregistered_protocol_and_path_injection_are_blocked(self):
        client = self.client()
        self.assert_code("form_resource_protocol_required", lambda: client.read_resource({"identity": self.target}, "datasets", "dataset-test"))
        self.assert_code("form_resource_id_invalid", lambda: client.read_resource({"identity": self.target}, "datasets", "../other"))
        self.assertEqual([], self.calls)

    def test_catalog_platform_version_source_and_original_export_are_bound(self):
        client = self.client()
        self.assertTrue(client.readiness()["write_ready"])
        self.contract["catalog"]["component_version"] = "other-version"
        client = self.client()
        self.assertFalse(client.readiness()["write_ready"])
        self.assert_code("form_catalog_invalid", client.catalog)
        self.contract["catalog"]["component_version"] = "offline-version"
        self.contract["catalog"]["native_exports"] = [{"path": "native.json", "sha256": hashlib.sha256(b"native").hexdigest()}]
        (self.root / "native.json").write_bytes(b"native")
        client = self.client()
        self.assertTrue(client.readiness()["write_ready"])
        (self.root / "native.json").write_bytes(b"changed")
        self.assertFalse(client.readiness()["write_ready"])

    def test_credentials_are_scoped_to_registered_origin_and_environment(self):
        from gxp_form_editor.platform import _credential, set_credential
        from types import SimpleNamespace
        values = {}
        fake = SimpleNamespace(set_password=lambda service, key, value: values.__setitem__((service, key), value), get_password=lambda service, key: values.get((service, key)))
        with patch.dict("sys.modules", {"keyring": fake}):
            set_credential(self.config, "token=offline")
            self.assertEqual("token=offline", _credential(self.config))
            self.assertEqual("", _credential({**self.config, "base_url": "http://other.invalid"}))
            self.assertEqual("", _credential({**self.config, "environment": "another-environment"}))

    def test_save_templates_must_bind_every_native_identity_and_candidate(self):
        for field in ("appId", "siteOutId", "outId", "dataVersionId"):
            original = copy.deepcopy(self.contract["write_requests"][0]["body"])
            self.contract["write_requests"][0]["body"][field] = "another-page-or-version"
            client = self.client()
            self.assertFalse(client.readiness()["write_ready"])
            self.assertIn("save_target_binding_required", client.readiness()["reasons"])
            self.contract["write_requests"][0]["body"] = original
        self.contract["write_requests"][0]["body"]["component"] = {"$json": "snapshot.page"}
        client = self.client()
        self.assertFalse(client.readiness()["write_ready"])
        self.assertIn("save_candidate_binding_required", client.readiness()["reasons"])

    def test_cross_page_save_and_missing_conditional_are_rejected_before_http(self):
        client = self.client()
        request = self.save_request()
        request["body"]["outId"] = "another-page"
        self.assert_code("form_save_target_mismatch", lambda: client.execute_request(request))
        request = self.save_request()
        request["headers"].clear()
        self.assert_code("form_save_concurrency_missing", lambda: client.execute_request(request))
        self.assertEqual([], self.calls)

    def test_original_scripts_styles_and_name_must_be_preserved(self):
        self.response.update({"OnLoadBefore": "original-before", "OnLoaded": "original-after", "CssContent": ".original{}", "Name": "Original page"})
        client = self.client()
        snapshot = client.read_draft(self.target)
        self.assert_code("form_native_field_changed", lambda: client.build_requests(snapshot, snapshot["page"]))
        body = self.contract["write_requests"][0]["body"]
        for native, field in (("OnLoadBefore", "onLoadBefore"), ("OnLoaded", "onLoaded"), ("CssContent", "cssContent"), ("Name", "name")):
            body[field] = {"$": "snapshot.native." + native}
        client = self.client()
        snapshot = client.read_draft(self.target)
        request = client.build_requests(snapshot, snapshot["page"])[0]
        self.assertEqual("original-before", request["body"]["onLoadBefore"])
        body["onLoaded"] = "changed-script"
        client = self.client()
        snapshot = client.read_draft(self.target)
        self.assert_code("form_native_field_changed", lambda: client.build_requests(snapshot, snapshot["page"]))

    def test_association_templates_require_registered_scope_and_native_targets(self):
        helper = {"method": "DELETE", "path": "/api/pagemodels", "query": {"appId": {"$": "target.app_id"}, "pageId": "another-page"}}
        self.contract["association_sync"]["mode"] = "explicit_requests"
        self.contract["write_requests"].append(helper)
        client = self.client()
        self.assertFalse(client.readiness()["write_ready"])
        self.assertIn("association_target_scope_required", client.readiness()["reasons"])
        helper["native_protocol"] = "native-page-model-delete-v1"
        helper["scope"] = {"kind": "current_page", "app_id": {"$": "target.app_id"}, "page_id": {"$": "target.route"}}
        client = self.client()
        snapshot = client.read_draft(self.target)
        self.assert_code("form_association_target_mismatch", lambda: client.build_requests(snapshot, snapshot["page"]))
        helper["query"]["pageId"] = {"$": "target.route"}
        client = self.client()
        snapshot = client.read_draft(self.target)
        requests = client.build_requests(snapshot, snapshot["page"])
        self.assertEqual(self.target["route"], requests[-1]["query"]["pageId"])


if __name__ == "__main__":
    unittest.main()
