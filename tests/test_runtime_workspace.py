from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path

from tests.helpers import MCP_ROOT
from gxp_editor.runtime_workspace import WorkspaceBusyError, cleanup_interrupted_request, lease_workspace


class RuntimeWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.pool = Path(self.temporary.name).resolve() / "workspaces"
        self.key = "a" * 64

    @contextmanager
    def lease(self, request="b" * 32, *, slots=2, wait_seconds=0):
        with lease_workspace(self.pool, self.key, request, project_text="project", nuget_text_factory=lambda root: "nuget",
                             slots=slots, wait_seconds=wait_seconds) as workspace:
            yield workspace

    @staticmethod
    def restore(workspace):
        (workspace.root / "obj").mkdir(exist_ok=True)
        (workspace.root / "obj" / "project.assets.json").write_text('{}', encoding="utf-8")
        workspace.capture_restore_state()

    def test_only_restore_metadata_survives_success(self):
        with self.lease() as workspace:
            self.assertFalse(workspace.cache_hit)
            self.restore(workspace)
            workspace.source.write_text("business source", encoding="utf-8")
            (workspace.root / "bin").mkdir()
            (workspace.root / "bin" / "Candidate.dll").write_bytes(b"business binary")
            compiled = workspace.root / "obj" / "Debug"
            compiled.mkdir()
            (compiled / "Candidate.pdb").write_bytes(b"business debug information")
            root = workspace.root
            workspace.mark_restore_ready()
        self.assertFalse((root / "Candidate.cs").exists())
        self.assertFalse((root / "bin").exists())
        self.assertFalse((root / "obj" / "Debug").exists())
        self.assertFalse((root / "lease.json").exists())
        with self.lease() as again:
            self.assertEqual(again.root, root)
            self.assertTrue(again.cache_hit)

    def test_dirty_or_corrupt_cache_is_reset_once(self):
        with self.lease() as workspace:
            self.restore(workspace)
            root = workspace.root
            workspace.source.write_text("bad code")
        self.assertEqual(json.loads((root / "state.json").read_text())["status"], "dirty")
        with self.lease() as again:
            self.assertFalse(again.cache_hit)
            self.assertFalse((again.root / "obj").exists())
            self.restore(again)
            again.mark_restore_ready()
        (root / "state.json").write_text("broken")
        with self.lease() as again:
            self.assertFalse(again.cache_hit)
            self.assertFalse((again.root / "obj").exists())

    def test_mark_restore_ready_followed_by_error_remains_dirty(self):
        with self.assertRaisesRegex(RuntimeError, "late error"):
            with self.lease() as workspace:
                root = workspace.root
                self.restore(workspace)
                workspace.mark_restore_ready()
                raise RuntimeError("late error")
        self.assertEqual(json.loads((root / "state.json").read_text())["status"], "dirty")

    def test_restore_metadata_change_cannot_be_marked_reusable(self):
        with self.assertRaisesRegex(ValueError, "Restore metadata changed"):
            with self.lease() as workspace:
                root = workspace.root
                self.restore(workspace)
                (root / "obj" / "project.assets.json").write_text('{"changed":true}', encoding="utf-8")
                workspace.mark_restore_ready()
        self.assertEqual(json.loads((root / "state.json").read_text())["status"], "dirty")

    def test_old_ready_schema_is_not_treated_as_restore_evidence(self):
        with self.lease() as workspace:
            root = workspace.root
            self.restore(workspace)
            workspace.mark_restore_ready()
        state = json.loads((root / "state.json").read_text())
        state.update(schema_version=1, status="ready")
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
        with self.lease() as workspace:
            self.assertFalse(workspace.cache_hit)
            self.assertFalse((workspace.root / "obj").exists())

    def test_concurrent_leases_are_distinct_and_wait_is_bounded(self):
        with self.lease("b" * 32) as first:
            first.source.write_text("source A")
            with self.lease("c" * 32) as second:
                second.source.write_text("source B")
                self.assertNotEqual(first.root, second.root)
                self.assertEqual(first.source.read_text(), "source A")
                started = time.monotonic()
                with self.assertRaises(WorkspaceBusyError):
                    with self.lease("d" * 32, wait_seconds=0.1):
                        self.fail("must not acquire occupied slots")
                self.assertLess(time.monotonic() - started, 1)
                self.assertEqual(second.source.read_text(), "source B")

    def test_interrupted_cleanup_never_touches_live_or_other_request(self):
        with self.lease() as workspace:
            workspace.source.write_text("live source")
            root = workspace.root
            cleanup_interrupted_request(self.pool, "b" * 32)
            self.assertEqual(workspace.source.read_text(), "live source")
        (root / "lease.json").write_text(json.dumps({"request_id": "c" * 32}))
        (root / "Candidate.cs").write_text("orphan source")
        cleanup_interrupted_request(self.pool, "b" * 32)
        self.assertTrue((root / "Candidate.cs").exists())
        cleanup_interrupted_request(self.pool, "c" * 32)
        self.assertFalse((root / "Candidate.cs").exists())
        self.assertEqual(json.loads((root / "state.json").read_text())["status"], "dirty")

    def test_environment_key_and_request_id_cannot_escape_pool(self):
        for key, request in (("../escape", "b" * 32), (self.key, "../escape")):
            with self.assertRaises(ValueError):
                with lease_workspace(self.pool, key, request, project_text="project", nuget_text_factory=lambda root: "nuget"):
                    self.fail("invalid paths must not lease")

    def test_process_exit_releases_slot_and_next_lease_scrubs_orphan(self):
        script = (
            "import os,sys\nfrom pathlib import Path\n"
            "from gxp_editor.runtime_workspace import lease_workspace\n"
            "with lease_workspace(Path(sys.argv[1]), 'a'*64, 'b'*32, project_text='project', nuget_text_factory=lambda root:'nuget', slots=1) as workspace:\n"
            " workspace.source.write_text('orphan business source')\n"
            " print('ready', flush=True)\n import time; time.sleep(30)\n"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(MCP_ROOT)
        child = subprocess.Popen([sys.executable, "-c", script, str(self.pool)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 env=environment, text=True)
        try:
            # A bounded read via a helper avoids an unbounded UI/test wait.
            import queue
            import threading
            output = queue.Queue()
            threading.Thread(target=lambda: output.put(child.stdout.readline()), daemon=True).start()
            self.assertEqual(output.get(timeout=5).strip(), "ready")
            with self.assertRaises(WorkspaceBusyError):
                with self.lease(slots=1):
                    self.fail("child owns this slot")
            child.kill()
            child.communicate(timeout=5)
            with self.lease(slots=1) as workspace:
                self.assertFalse(workspace.cache_hit)
                self.assertFalse(workspace.source.exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
