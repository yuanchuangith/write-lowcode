from __future__ import annotations

import unittest

from gxp_editor.config import ConfigurationError, DatabaseConfig


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


if __name__ == "__main__":
    unittest.main()
