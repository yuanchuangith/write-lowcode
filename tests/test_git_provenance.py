"""Git batching must preserve the original workspace evidence byte for byte."""
from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from gxp_editor.compiler import CompilerBridge, CompilerError
from tests.helpers import editor_config


@unittest.skipUnless(shutil.which("git"), "Git executable required")
class GitProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="gxp git evidence ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.git("init", "--quiet")
        self.git("config", "user.name", "Offline Test")
        self.git("config", "user.email", "offline@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "commit.gpgsign", "false")
        (self.root / "bridge.js").write_text("// fixture\n", encoding="utf-8")
        (self.root / "tracked.txt").write_text("original\n", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"\0original\xff")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "Fixture")
        self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(self.root)))

    def git(self, *arguments):
        result = subprocess.run(["git", "-C", str(self.root), *arguments],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15, check=True)
        return result.stdout

    def legacy_evidence(self):
        root = Path(self.git("rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
        commit = self.git("rev-parse", "HEAD").decode("utf-8").strip()
        branch = self.git("branch", "--show-current").decode("utf-8").strip() or "DETACHED"
        status = self.git("status", "--porcelain=v1", "-z", "--untracked-files=all")
        diff = self.git("diff", "--no-ext-diff", "--binary", "HEAD", "--")
        fingerprint = hashlib.sha256(commit.encode("ascii") + b"\0" + status + b"\0" + diff).hexdigest()
        dirty_entries = [entry for entry in status.split(b"\0") if entry]
        return {"ready": True, "frontend_repo": str(root), "commit": commit, "branch": branch,
                "dirty": bool(dirty_entries), "dirty_file_count": len(dirty_entries),
                "workspace_sha256": fingerprint, "bridge_path": "bridge.js"}

    def assert_equivalent(self):
        expected = self.legacy_evidence()
        with patch.object(self.bridge, "_git", wraps=self.bridge._git) as calls:
            actual = self.bridge.source_provenance()
        self.assertEqual(actual, expected)
        self.assertEqual(calls.call_count, 3)
        return actual

    def test_clean_dirty_staged_binary_rename_and_untracked_states_match(self):
        self.assertFalse(self.assert_equivalent()["dirty"])
        (self.root / "tracked.txt").write_text("modified\n", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"\0changed\xfe")
        (self.root / "untracked name.txt").write_text("untracked\n", encoding="utf-8")
        self.assertTrue(self.assert_equivalent()["dirty"])
        self.git("add", "tracked.txt")
        self.git("mv", "bridge.js", "renamed bridge.js")
        # The configured bridge path need not be tracked, only inside the root.
        (self.root / "bridge.js").write_text("// replacement\n", encoding="utf-8")
        self.assertTrue(self.assert_equivalent()["dirty"])

    def test_detached_head_and_nested_configuration_match_root_contract(self):
        self.git("checkout", "--detach", "--quiet", "HEAD")
        self.assertEqual(self.assert_equivalent()["branch"], "DETACHED")
        nested = self.root / "nested"
        nested.mkdir()
        bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(nested)))
        with self.assertRaisesRegex(CompilerError, "must be its Git root"):
            bridge.source_provenance()

    def test_linked_worktree_matches_legacy_evidence(self):
        worktree = self.root / "linked worktree"
        self.assertTrue(worktree.resolve().is_relative_to(self.root.resolve()))
        self.git("worktree", "add", "--detach", "--quiet", str(worktree), "HEAD")
        original_root = self.root
        try:
            self.root = worktree
            self.bridge = CompilerBridge(replace(editor_config(), frontend_repo=str(worktree)))
            self.assertEqual(self.assert_equivalent()["branch"], "DETACHED")
        finally:
            self.root = original_root
            self.git("worktree", "remove", "--force", str(worktree))

    def test_incomplete_batched_identity_is_rejected(self):
        with patch.object(self.bridge, "_git", return_value=str(self.root) + "\n" + "a" * 40 + "\n"):
            with self.assertRaisesRegex(CompilerError, "invalid Git identity"):
                self.bridge.source_provenance()


if __name__ == "__main__":
    unittest.main()
