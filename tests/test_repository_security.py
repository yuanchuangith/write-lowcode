from __future__ import annotations

import contextlib
import unittest
from unittest.mock import Mock

from gxp_editor.repository import EditorRepository


class GrantSession:
    def __init__(self, grants):
        self.grants = grants

    def query(self, sql, max_rows=50):
        self.asserted_sql = sql
        if sql == "SELECT CURRENT_USER() AS authenticated_principal":
            return [{"authenticated_principal": "synthetic-editor@localhost"}]
        return [{"grant": item} for item in self.grants]


class GrantDatabase:
    def __init__(self, grants):
        self.session = GrantSession(grants)

    @contextlib.contextmanager
    def read_session(self):
        yield self.session


class RepositorySecurityTests(unittest.TestCase):
    def test_relation_snapshot_can_be_locked_in_the_write_transaction(self):
        session = Mock()
        session.query.return_value = []
        repository = EditorRepository(None)
        repository._relations("design-1", session, for_update=True)
        sql, params = session.query.call_args.args
        self.assertTrue(sql.rstrip().endswith("FOR UPDATE"))
        self.assertIn("WHERE design_Id=", sql)
        self.assertEqual(params, {"design_id": "design-1"})
        repository._relations("design-1", session)
        self.assertNotIn("FOR UPDATE", session.query.call_args.args[0])

    def test_grant_status_never_returns_raw_grant_identity(self):
        repository = EditorRepository(
            GrantDatabase(
                [
                    "GRANT SELECT, INSERT, UPDATE, DELETE, SUPER ON *.* TO 'secret-user'@'secret-host'"
                ]
            )
        )
        status = repository.grant_status()
        self.assertFalse(status["safe"])
        self.assertTrue(status["broad_write"])
        self.assertNotIn("grants", status)
        serialized = str(status)
        self.assertNotIn("secret-user", serialized)
        self.assertNotIn("secret-host", serialized)

    def test_shared_mode_requires_pinned_grants_fingerprint(self):
        repository = EditorRepository(
            GrantDatabase(
                [
                    "GRANT USAGE ON *.* TO 'shared-user'@'%'",
                    "GRANT ALL PRIVILEGES ON `edoc2v5`.* TO 'shared-user'@'%'",
                ]
            )
        )
        unpinned = repository.grant_status(account_mode="shared_restricted")
        self.assertTrue(unpinned["shared_policy_eligible"])
        self.assertFalse(unpinned["safe"])
        pinned = repository.grant_status(
            account_mode="shared_restricted",
            expected_grants_sha256=unpinned["grants_sha256"],
        )
        self.assertTrue(pinned["safe"])
        self.assertFalse(pinned["dedicated_safe"])
        self.assertTrue(pinned["fingerprint_matches"])

    def test_shared_mode_rejects_global_non_usage_privileges(self):
        repository = EditorRepository(
            GrantDatabase(
                [
                    "GRANT SELECT ON *.* TO 'shared-user'@'%'",
                    "GRANT ALL PRIVILEGES ON `edoc2v5`.* TO 'shared-user'@'%'",
                ]
            )
        )
        current = repository.grant_status(account_mode="shared_restricted")
        status = repository.grant_status(
            account_mode="shared_restricted",
            expected_grants_sha256=current["grants_sha256"],
        )
        self.assertFalse(status["safe"])
        self.assertEqual(status["global_non_usage_grant_count"], 1)

    def test_shared_mode_rejects_grant_fingerprint_drift(self):
        repository = EditorRepository(
            GrantDatabase(
                [
                    "GRANT USAGE ON *.* TO 'shared-user'@'%'",
                    "GRANT ALL PRIVILEGES ON `edoc2v5`.* TO 'shared-user'@'%'",
                ]
            )
        )
        status = repository.grant_status(
            account_mode="shared_restricted",
            expected_grants_sha256="0" * 64,
        )
        self.assertFalse(status["safe"])
        self.assertFalse(status["fingerprint_matches"])

    def test_privileged_shared_mode_accepts_pinned_global_privileges(self):
        repository = EditorRepository(
            GrantDatabase(
                [
                    "GRANT SELECT, INSERT, UPDATE, DELETE, SUPER, FILE, PROCESS ON *.* TO 'shared-user'@'%'",
                ]
            )
        )
        unpinned = repository.grant_status(account_mode="shared_unrestricted")
        self.assertFalse(unpinned["safe"])
        self.assertFalse(unpinned["shared_policy_eligible"])
        pinned = repository.grant_status(
            account_mode="shared_unrestricted",
            expected_grants_sha256=unpinned["grants_sha256"],
        )
        self.assertTrue(pinned["safe"])
        self.assertTrue(pinned["privileged_shared_override"])
        self.assertTrue(pinned["shared_account_warning"])


if __name__ == "__main__":
    unittest.main()
