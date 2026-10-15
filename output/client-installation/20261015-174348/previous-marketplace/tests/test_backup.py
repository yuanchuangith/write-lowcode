from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import baseline, relation
from gxp_editor.backup import BackupError, BackupStore


class BackupTests(unittest.TestCase):
    def test_backup_contains_runtime_status_and_loads_when_untampered(self):
        with tempfile.TemporaryDirectory() as temporary:
            draft = baseline(relations=[relation()])
            store = BackupStore(Path(temporary), integrity_key="test-backup-key")
            created = store.create(draft, {"bridge_sha256": "compiler-hash"}, {"available": True})
            loaded = store.load(draft["ref_id"], created["backup_id"])
            self.assertTrue(loaded["runtime-artifacts.json"]["available"])
            self.assertEqual(loaded["manifest"]["content_hash"], draft["content_hash"])

    def test_backup_hash_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            draft = baseline(relations=[])
            store = BackupStore(Path(temporary), integrity_key="test-backup-key")
            created = store.create(draft, {"bridge_sha256": "compiler-hash"})
            design_path = Path(created["path"]) / "design.json"
            design_path.write_text("{}\n", encoding="utf-8")
            with self.assertRaisesRegex(BackupError, "hash mismatch"):
                store.load(draft["ref_id"], created["backup_id"])

    def test_backup_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = BackupStore(Path(temporary), integrity_key="test-backup-key")
            with self.assertRaisesRegex(BackupError, "RefId"):
                store.create(baseline(ref_id="..\\outside", relations=[]), {"bridge_sha256": "compiler-hash"})
            with self.assertRaisesRegex(BackupError, "identity"):
                store.load("ref-1", "..\\outside")

    def test_manifest_and_file_cannot_be_rewritten_together_without_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            draft = baseline(relations=[])
            store = BackupStore(Path(temporary), integrity_key="test-backup-key")
            created = store.create(draft, {"bridge_sha256": "compiler-hash"})
            manifest_path = Path(created["path"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["content_hash"] = "tampered"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(BackupError, "signature"):
                store.load(draft["ref_id"], created["backup_id"])


if __name__ == "__main__":
    unittest.main()
