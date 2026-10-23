from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from scripts.sync_skills import mirror_problems, sync_skills, tracked_skill_paths


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "gxp-lowcode-edit"


class SkillContractTests(unittest.TestCase):
    def test_editor_requires_explicit_activation_and_allows_authorized_continuation(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Explicit invocation gate", skill)
        self.assertIn("current user message explicitly invokes", skill)
        self.assertIn("is not an invocation", skill)
        self.assertIn("Do not call `editor_status`", skill)
        self.assertIn("readonly plan is only an input candidate", skill)
        self.assertIn("After activation, the same dialogue may continue its authorized draft", skill)
        self.assertIn("A new target, another dialogue or revoked authorization is not covered", skill)

    def test_conditional_session_workflows_are_linked_instead_of_loaded_in_entrypoint(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        workflows = (SKILL_ROOT / "references" / "session-workflows.md").read_text(encoding="utf-8")
        self.assertIn("references/session-workflows.md", skill)
        for title in ("Batch repair of legacy parameter references", "Restore workflow", "Session recovery", "Dialogue handoff"):
            self.assertNotIn("## " + title, skill)
            self.assertIn("## " + title, workflows)
        for boundary in ("signed human approval", "independently regenerated", "Nonblank candidate C#", "draft concurrency"):
            self.assertIn(boundary, skill)

    def test_implicit_invocation_is_disabled(self) -> None:
        policy = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("allow_implicit_invocation: false", policy)
        self.assertIn("$gxp-lowcode-edit", policy)

    def test_settings_entry_does_not_activate_business_editing_or_accept_secrets(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        settings = (SKILL_ROOT / "references" / "database-settings.md").read_text(encoding="utf-8")
        self.assertIn("Local database settings", skill)
        self.assertIn("without a conversation or edit session", skill)
        self.assertIn("never grants business-editing", skill)
        self.assertIn("expected_revision", settings)
        self.assertIn("Never pass a password", settings)
        self.assertIn("Active editing workflows", settings)
        self.assertIn("never auto-cancel", settings)

    def test_evidence_and_response_contracts_match_progressive_disclosure(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        targeting = (SKILL_ROOT / "references" / "targeting-and-source.md").read_text(encoding="utf-8")
        business = (SKILL_ROOT / "references" / "business-review.md").read_text(encoding="utf-8")
        patches = (SKILL_ROOT / "references" / "patch-schema.md").read_text(encoding="utf-8")
        for operation in ("inspect_edit_context", "read_plan_review", "scope_policy", "no_changes=true"):
            self.assertIn(operation, skill)
        self.assertIn("probe_runtime=false", skill)
        self.assertIn("1–10", targeting)
        self.assertIn("response_detail=\"summary\"", business)
        self.assertIn("256 KiB", business)
        self.assertIn("manual_review", business)
        self.assertIn("new user basis", business)
        self.assertIn("set_array_item", patches)
        self.assertIn("supported_operations", patches)
        self.assertIn("complete old and replacement row", patches)

    def test_tracked_skill_mirrors_match_file_set_and_content(self) -> None:
        source_files, mirror_files = tracked_skill_paths(PLUGIN_ROOT)
        self.assertTrue(source_files, "No tracked source skills found")
        self.assertEqual([], mirror_problems(PLUGIN_ROOT, source_files, mirror_files))


class SkillMirrorTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.relative = Path("example/SKILL.md")
        self.source = self.root / "skills" / self.relative
        self.target = self.root / ".claude/skills" / self.relative
        self.source.parent.mkdir(parents=True)
        self.target.parent.mkdir(parents=True)
        self.source.write_bytes(b"source\r\n")

    def test_sync_updates_tracked_mirror_and_keeps_user_files(self) -> None:
        self.target.write_bytes(b"old")
        user_file = self.target.parent / "notes.txt"
        user_file.write_bytes(b"user notes")
        self.assertEqual([self.relative], sync_skills(self.root, {self.relative}, {self.relative}))
        self.assertEqual(b"source\r\n", self.target.read_bytes())
        self.assertEqual(b"user notes", user_file.read_bytes())

    def test_sync_does_not_overwrite_untracked_conflict(self) -> None:
        self.target.write_bytes(b"user skill")
        with self.assertRaisesRegex(ValueError, "untracked user file"):
            sync_skills(self.root, {self.relative}, set())
        self.assertEqual(b"user skill", self.target.read_bytes())

    def test_linked_mirror_is_never_opened_for_writing(self) -> None:
        self.target.hardlink_to(self.source)
        before = self.source.stat().st_mtime_ns
        self.assertEqual([], sync_skills(self.root, {self.relative}, {self.relative}))
        self.assertEqual(before, self.source.stat().st_mtime_ns)
        self.assertEqual([], mirror_problems(self.root, {self.relative}, {self.relative}))

    def test_check_detects_missing_files_and_byte_differences(self) -> None:
        self.assertTrue(mirror_problems(self.root, {self.relative}, {self.relative}))
        self.target.write_bytes(b"source\n")
        self.assertIn("Different mirror content", mirror_problems(self.root, {self.relative}, {self.relative})[0])
        self.assertIn("Obsolete tracked mirror", mirror_problems(self.root, set(), {self.relative})[0])

    def test_sync_rejects_path_outside_skill_roots(self) -> None:
        with self.assertRaises(ValueError):
            sync_skills(self.root, {Path("../outside.txt")}, set())


if __name__ == "__main__":
    unittest.main()
