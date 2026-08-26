from __future__ import annotations

import unittest
from unittest.mock import patch

from tests.helpers import editor_config
from gxp_editor.service import EditorService


class FakeState:
    def active_session(self):
        return None


class FakeDatabase:
    def status(self):
        return {"connected": True}


class FakeRepository:
    def index_status(self):
        return {"draft": {"ready": True}, "relations": {"ready": True}}

    def grant_status(self, *, account_mode, expected_grants_sha256):
        return {
            "safe": True,
            "dedicated_safe": True,
            "shared_account_warning": False,
            "privileged_shared_override": False,
            "fingerprint_configured": True,
            "fingerprint_matches": True,
            "grants_sha256": "c" * 64,
        }


class FakeCompiler:
    def describe_registry(self):
        return {
            "bridge_sha256": "a" * 64,
            "registry_hash": "b" * 64,
            "rule_source_sha256": "c" * 64,
            "frontend_version": "test",
            "elements": [{"key": "Start"}],
        }


class UnavailableRuntimeValidator:
    def __init__(self, runtime):
        self.runtime = runtime

    def status(self):
        raise RuntimeError("runtime references unavailable")


class ServiceStatusTests(unittest.TestCase):
    def test_runtime_validation_failure_is_advisory_for_database_writes(self):
        config = editor_config()
        service = object.__new__(EditorService)
        service.state = FakeState()
        components = (
            config,
            FakeDatabase(),
            FakeRepository(),
            FakeCompiler(),
            UnavailableRuntimeValidator(config.runtime_references),
        )
        with patch.object(service, "_components", return_value=components), patch(
            "gxp_editor.service.approval_secret", return_value="test-secret"
        ):
            status = service.editor_status()

        self.assertTrue(status["planning_ready"])
        self.assertTrue(status["write_ready"])
        self.assertTrue(status["production_ready"])
        self.assertFalse(status["runtime_compatibility_verified"])
        self.assertTrue(status["gates"]["runtime_snapshot"]["ready"])
        self.assertFalse(status["gates"]["runtime_snapshot"]["blocking"])
        self.assertIn("does not block", status["gates"]["runtime_snapshot"]["warning"])


if __name__ == "__main__":
    unittest.main()
