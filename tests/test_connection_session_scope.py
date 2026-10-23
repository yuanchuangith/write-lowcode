"""An existing edit flow must never follow a changed database destination."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import baseline, editor_config
from gxp_editor import connection_settings as connection_manager
from gxp_editor.config import load_config
from gxp_editor.handoff_store import environment_scope
from gxp_editor.recovery import recover_session
from gxp_editor.service import EditorService
from gxp_editor.state import StateError, StateStore, assert_session_database_scope
from gxp_editor.writer import _execute_approved_transaction


class ConnectionSessionScopeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = editor_config()
        self.changed = replace(self.config, database=replace(self.config.database, host="other-development"))
        self.settings = types.ModuleType("gxp_editor.connection_settings")
        self.settings.active_settings = Mock(return_value=None)
        self.enterContext(patch.dict(sys.modules, {"gxp_editor.connection_settings": self.settings}))
        self.store = StateStore(Path(self.temporary.name) / "state.sqlite3")

    def session(self, *, bound=True):
        return self.store.create_session(
            baseline(), "backup", "compiler",
            database_scope=environment_scope(self.config.database) if bound else "",
        )

    def service(self, config=None):
        service = object.__new__(EditorService)
        service._conversation_id = ""
        service.state = self.store
        service._components = Mock(return_value=(config or self.config, Mock(), Mock(), Mock(), Mock()))
        return service

    def test_scope_is_persisted_and_survives_reopening_state(self):
        session = self.session()
        reopened = StateStore(self.store.path).get_session(session["id"], include_baseline=False)
        self.assertEqual(reopened["database_scope"], environment_scope(self.config.database))
        assert_session_database_scope(reopened, self.config.database)

    def test_state_override_is_shared_with_connection_manager(self):
        # The manager must inspect the same file used by normal MCP sessions.
        state_path = Path(self.temporary.name) / "isolated/editor.sqlite3"
        with patch.dict(os.environ, {"GXP_LOWCODE_EDITOR_STATE": str(state_path)}):
            state = StateStore()
            session = state.create_session(baseline(), "backup", "compiler")
            self.assertEqual(state.path, state_path.resolve())
            self.assertEqual(connection_manager._editor_state_path(), state.path)
            self.assertEqual(connection_manager._active_sessions(), [{"id": session["id"], "state": "locked"}])

    def test_shared_loader_changes_destination_and_account_without_changing_editor_policy(self):
        original = replace(self.config, frontend_repo="existing/frontend", backend_repo="existing/backend",
            compiler_bridge="custom/bridge.js", database=replace(self.config.database,
                account_mode="shared_restricted", expected_grants_sha256="a" * 64,
                connect_timeout_seconds=12))
        base_path = Path(self.temporary.name) / "editor.json"
        shared_path = Path(self.temporary.name) / "connection.json"
        base_path.write_text(json.dumps(original.public_dict()), encoding="utf-8")
        shared = {"schema_version": 1, "revision": "b" * 32,
            "host": "replacement-development", "port": 3307, "database": "replacement_database",
            "accounts": {
                "look": {"user": "readonly-account", "credential_target": "GxpLowcode.Connection.example.look"},
                "write": {"user": "replacement-editor", "credential_target": "GxpLowcode.Connection.example.write"},
            }}
        shared_path.write_text(json.dumps(shared), encoding="utf-8")
        with patch.dict(sys.modules, {"gxp_editor.connection_settings": connection_manager}), \
                patch.dict(os.environ, {"GXP_LOWCODE_EDITOR_CONFIG": str(base_path),
                    "GXP_LOWCODE_SHARED_CONFIG": str(shared_path)}):
            loaded = load_config()
        expected = replace(original, database=replace(original.database,
            host=shared["host"], port=shared["port"], database=shared["database"],
            user=shared["accounts"]["write"]["user"],
            credential_target=shared["accounts"]["write"]["credential_target"]))
        self.assertEqual(loaded, expected)
        self.assertEqual(json.loads(base_path.read_text(encoding="utf-8")), original.public_dict())

    def test_legacy_session_requires_binding_only_after_shared_settings_enabled(self):
        session = self.session(bound=False)
        assert_session_database_scope(session, self.config.database)
        self.settings.active_settings.return_value = {"revision": "configured"}
        with self.assertRaisesRegex(StateError, "no database binding"):
            assert_session_database_scope(session, self.config.database)

    def test_password_reference_rotation_on_same_destination_keeps_binding(self):
        session = self.session()
        rotated = replace(self.config.database, credential_target="rotated-secret")
        assert_session_database_scope(session, rotated)

    def test_service_refuses_changed_destination_before_target_read(self):
        session = self.session()
        service = self.service(self.changed)
        with self.assertRaisesRegex(StateError, "connection changed"):
            service.begin_edit_session(session["action_code"])
        service._components.return_value[2].load_draft_by_identifier.assert_not_called()

    def test_local_cancellation_can_still_be_inspected_after_destination_changed(self):
        session = self.session()
        service = self.service(self.changed)
        with self.assertRaisesRegex(StateError, "connection changed"):
            service._owned_session(session["id"])
        self.assertEqual(service.inspect_session_cancellation(session["id"])["session_id"], session["id"])

    def test_writer_rejects_old_scope_before_approval_claim_or_database_connection(self):
        session = self.session()
        state = Mock()
        state.get_session.return_value = session
        state.get_transaction.return_value = {"session_id": session["id"], "artifact_path": "unused.json"}
        with patch("gxp_editor.writer.load_config", return_value=self.changed), \
                patch("gxp_editor.writer.StateStore", return_value=state), \
                patch("gxp_editor.writer.EditorDatabase") as database, \
                patch("gxp_editor.writer.load_artifact") as artifact:
            with self.assertRaisesRegex(StateError, "connection changed"):
                _execute_approved_transaction("transaction")
        database.assert_not_called()
        artifact.assert_not_called()
        state.claim_transaction.assert_not_called()

    def test_recovery_rejects_changed_scope_before_reading_clone_rows(self):
        session = self.session()
        with patch("gxp_editor.recovery.load_config", return_value=self.changed), \
                patch("gxp_editor.recovery.EditorDatabase") as database:
            with self.assertRaisesRegex(StateError, "connection changed"):
                recover_session(session["id"], state=self.store)
        database.assert_not_called()

    def test_real_service_entry_always_persists_destination_scope(self):
        service = self.service()
        repository, compiler = service._components.return_value[2:4]
        draft = baseline(csharp_code="")
        draft["data_json"] = json.loads(draft["data"])
        repository.load_draft_by_identifier.return_value = draft
        compiler.describe_registry.return_value = {"elements": [], "bridge_sha256": "a" * 64}
        compiler.compiler_binding.return_value = {"compiler_binding_sha256": "b" * 64}
        compiler.validate_design.return_value = {"validation": {"valid": True}}
        with patch("gxp_editor.service.BackupStore") as backup, \
                patch("gxp_editor.service.approval_secret", return_value="test-secret"), \
                patch.object(service, "editor_status", return_value={"planning_ready": True,
                    "write_ready": True, "production_ready": False, "gates": {}}), \
                patch("gxp_editor.handoff_import.discover_handoffs", return_value={}):
            backup.return_value.create.return_value = {"backup_id": "new-backup", "files": {}}
            result = service.begin_edit_session("ActionOne")
        persisted = self.store.get_session(result["session_id"], include_baseline=False)
        self.assertEqual(persisted["database_scope"], environment_scope(self.config.database))


if __name__ == "__main__":
    unittest.main()
