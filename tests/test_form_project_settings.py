"""Project connections must reach the form reader without importing old profiles."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gxp_form_editor.common import FormError
from gxp_form_editor.platform import PlatformClient, _credential, load_settings, save_settings
from gxp_project_context.context import ProjectContext, ProjectError, pin_context
from gxp_project_context.platform_auth import credential_key


class FormProjectSettingsTests(unittest.TestCase):
    def context(self, letter="a", form=None):
        scope = letter * 64
        return ProjectContext(scope, scope, "test|database:" + scope, managed=True,
            activation_id=letter * 32, refresh_generation=letter * 32,
            profile_revision=letter * 64,
            platform_binding={"platform_url": "https://" + letter + ".invalid",
                "app_id": "app-" + letter, "site_out_id": "site-" + letter,
                "account": "project-user", "form_config": form or {}})

    def test_active_project_supplies_connection_without_independent_file(self):
        context = self.context(form={"api_prefix": "/gxp2", "contract_path": "verified.json"})
        with pin_context(context), patch("gxp_form_editor.platform.settings_path", side_effect=AssertionError("No legacy read")):
            config = load_settings()
        self.assertEqual(config["base_url"], "https://a.invalid")
        self.assertEqual(config["contract_path"], "verified.json")
        self.assertEqual(config["project_binding"], context.binding())
        self.assertEqual(config["app_id"], "app-a")

    def test_project_credentials_use_the_same_isolated_key_as_project_login(self):
        context = self.context(form={"company_id": "company", "org_identity_id": "org"})
        with pin_context(context):
            config = load_settings()
        profile = {"project_id": context.project_id, "environment": context.environment,
                   "platform_binding": {**dict(context.platform_binding), "form_config": config}}
        with patch("keyring.get_password", return_value="synthetic-cookie") as keyring:
            self.assertEqual(_credential(config), "synthetic-cookie")
        keyring.assert_called_once_with("GxpFormEditor", credential_key(profile))
        with pin_context(self.context("b")):
            other = load_settings()
        with patch("keyring.get_password", return_value="") as keyring:
            _credential(other)
        self.assertNotEqual(keyring.call_args.args[1], credential_key(profile))

    def test_missing_project_contract_does_not_import_old_contract_or_enable_reading(self):
        with pin_context(self.context()), patch("gxp_form_editor.platform.settings_path", side_effect=AssertionError("No legacy fallback")):
            client = PlatformClient()
        result = client.readiness()
        self.assertTrue(result["connection_configured"])
        self.assertEqual(result["connection_source"], "active_project")
        self.assertNotIn("connection_not_configured", result["read_reasons"])
        self.assertIn("native_contract_v1_required", result["read_reasons"])
        self.assertFalse(result["read_ready"])
        self.assertFalse(result["write_ready"])

    def test_connection_probe_sends_the_configured_application_and_site_headers(self):
        calls = []
        def transport(method, url, headers, body, timeout):
            calls.append((method, url, headers))
            return {"status": 200, "body": {"data": {}}}
        client = PlatformClient({"base_url": "https://a.invalid", "api_prefix": "/gxp2",
                                 "app_id": "app-a", "site_out_id": "site-a"},
                                credentials=lambda: "synthetic-cookie", transport=transport)
        client._perform({"method": "GET", "path": "/api/app_info"}, write=False)
        method, url, headers = calls[0]
        self.assertEqual((method, url), ("GET", "https://a.invalid/gxp2/api/app_info"))
        self.assertEqual((headers["appid"], headers["siteoutid"]), ("app-a", "site-a"))

    def test_project_switch_is_reported_before_any_http(self):
        with pin_context(self.context()):
            client = PlatformClient(transport=lambda *args: self.fail("No HTTP on stale project"))
        with patch("gxp_project_context.context.assert_binding", side_effect=ProjectError("PROJECT_CHANGED", "Changed")):
            with self.assertRaises(ProjectError):
                client.read_draft({})

    def test_other_app_or_site_is_rejected(self):
        with pin_context(self.context()):
            client = PlatformClient()
        target = {"environment": "development", "base_url": "https://a.invalid", "platform": "web",
                  "app_id": "app-a", "site_out_id": "site-a", "page_id": "page", "route": "route", "data_version_id": "draft"}
        for field in ("app_id", "site_out_id"):
            with self.subTest(field=field), self.assertRaises(FormError) as caught:
                client._target({**target, field: "other"})
            self.assertEqual(caught.exception.code, "form_target_mismatch")

    def test_explicit_legacy_path_remains_readable_but_default_save_cannot_shadow_project(self):
        with tempfile.TemporaryDirectory() as directory, pin_context(self.context()):
            path = Path(directory) / "legacy.json"
            save_settings({"base_url": "https://legacy.invalid"}, path)
            self.assertEqual(load_settings(path), {"base_url": "https://legacy.invalid"})
            with self.assertRaises(FormError) as caught:
                save_settings({"base_url": "https://legacy.invalid"})
            self.assertEqual(caught.exception.code, "form_project_settings_managed")


if __name__ == "__main__":
    unittest.main()
