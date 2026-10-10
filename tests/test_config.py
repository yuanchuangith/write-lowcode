from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from gxp_editor.config import ConfigurationError, DatabaseConfig, EditorConfig, load_config, save_config


class DatabaseConfigTests(unittest.TestCase):
    def test_shared_account_mode_accepts_optional_valid_fingerprint(self):
        config = DatabaseConfig.from_dict(
            {
                "host": "127.0.0.1",
                "port": 3306,
                "database": "edoc2v5",
                "user": "shared-user",
                "account_mode": "shared_restricted",
                "expected_grants_sha256": "a" * 64,
            }
        )
        self.assertEqual(config.account_mode, "shared_restricted")
        self.assertEqual(config.expected_grants_sha256, "a" * 64)

    def test_invalid_account_mode_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "account_mode"):
            DatabaseConfig.from_dict(
                {
                    "host": "127.0.0.1",
                    "port": 3306,
                    "database": "edoc2v5",
                    "user": "shared-user",
                    "account_mode": "administrator",
                }
            )

    def test_privileged_shared_account_mode_is_explicitly_supported(self):
        config = DatabaseConfig.from_dict(
            {
                "host": "127.0.0.1",
                "port": 3306,
                "database": "edoc2v5",
                "user": "shared-user",
                "account_mode": "shared_unrestricted",
                "expected_grants_sha256": "b" * 64,
            }
        )
        self.assertEqual(config.account_mode, "shared_unrestricted")

    def test_invalid_grant_fingerprint_is_rejected(self):
        with self.assertRaisesRegex(ConfigurationError, "expected_grants_sha256"):
            DatabaseConfig.from_dict(
                {
                    "host": "127.0.0.1",
                    "port": 3306,
                    "database": "edoc2v5",
                    "user": "shared-user",
                    "account_mode": "shared_restricted",
                    "expected_grants_sha256": "not-a-sha256",
                }
            )


class SourceRepositoryConfigTests(unittest.TestCase):
    def config_data(self):
        return {
            "database": {"host": "127.0.0.1", "port": 3306, "database": "test", "user": "editor"},
            "frontend_repo": r"G:\hoyi\updateComponents\gxp2.components",
            "runtime_references": {"directory": "test-references"},
        }

    def test_old_configuration_gains_backend_source_without_changing_compiler_or_references(self):
        data = self.config_data()
        config = EditorConfig.from_dict(data)
        self.assertEqual(config.backend_repo, r"G:\hoyi\updateWeb\gxp2.web")
        self.assertEqual(config.frontend_repo, data["frontend_repo"])
        self.assertEqual(config.runtime_references.directory, "test-references")

    def test_custom_repository_paths_survive_configuration_save(self):
        data = self.config_data()
        data.update(frontend_repo=r"F:\cpm\gxp2.components", backend_repo=r"F:\cpm\gxp2.web")
        config = EditorConfig.from_dict(data)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            save_config(config, path)
            loaded = load_config(path)
        self.assertEqual(loaded.frontend_repo, data["frontend_repo"])
        self.assertEqual(loaded.backend_repo, data["backend_repo"])
        self.assertEqual(loaded.runtime_references.directory, "test-references")


if __name__ == "__main__":
    unittest.main()
