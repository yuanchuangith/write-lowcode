from __future__ import annotations

import copy
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from tests.helpers import editor_config, transaction
from gxp_editor.approval import ApprovalError, approval_is_current, approve_statement, verify_approvals


class ApprovalTests(unittest.TestCase):
    def test_expired_approval_can_be_refreshed(self):
        artifact = transaction()
        config = editor_config()
        statement = artifact['statements'][0]
        with patch('gxp_editor.approval.approval_secret', return_value='test-secret'):
            approve_statement(config, artifact, statement['id'])
            self.assertTrue(approval_is_current(artifact, statement))
            artifact['approvals'][statement['id']]['expires_at'] = (datetime.now() - timedelta(seconds=1)).isoformat()
            self.assertFalse(approval_is_current(artifact, statement))
            approve_statement(config, artifact, statement['id'])
            verify_approvals(config, artifact)
    def setUp(self):
        self.config = editor_config()
        self.secret = patch("gxp_editor.approval.approval_secret", return_value="test-secret")
        self.secret.start()
        self.addCleanup(self.secret.stop)

    def approve_all(self, artifact):
        for statement in artifact["statements"]:
            approve_statement(self.config, artifact, statement["id"])

    def test_every_statement_requires_approval(self):
        artifact = transaction()
        with self.assertRaisesRegex(ApprovalError, "has not been approved"):
            verify_approvals(self.config, artifact)

    def test_parameter_tampering_invalidates_approval(self):
        artifact = transaction()
        self.approve_all(artifact)
        artifact["statements"][0]["params"][0] = "tampered"
        with self.assertRaisesRegex(ApprovalError, "changed after approval"):
            verify_approvals(self.config, artifact)

    def test_signature_is_bound_to_transaction(self):
        artifact = transaction()
        self.approve_all(artifact)
        copied = copy.deepcopy(artifact)
        copied["transaction_id"] = "another-transaction"
        with self.assertRaisesRegex(ApprovalError, "metadata changed|signature is invalid"):
            verify_approvals(self.config, copied)

    def test_expired_approval_is_rejected(self):
        artifact = transaction()
        self.approve_all(artifact)
        approval = next(iter(artifact["approvals"].values()))
        approval["expires_at"] = (datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds")
        with self.assertRaisesRegex(ApprovalError, "Approval expired"):
            verify_approvals(self.config, artifact)


if __name__ == "__main__":
    unittest.main()
