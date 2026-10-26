from __future__ import annotations

import unittest
import tempfile
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

from scripts.sync_skills import mirror_problems, sync_skills, tracked_skill_paths


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "gxp-lowcode-edit"


class SkillContractTests(unittest.TestCase):
    def test_frontmatter_identifies_a_bounded_editing_skill(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"\A---\n(.*?)\n---\n(.+)\Z", skill, re.DOTALL)
        self.assertIsNotNone(match, "Skill needs frontmatter and a nonempty body")
        fields = dict(re.findall(r"^(name|description): (.+)$", match.group(1), re.MULTILINE))
        self.assertEqual("gxp-lowcode-edit", fields.get("name"))
        description = fields.get("description", "")
        for capability in ("unpublished", "human SQL approval", "settings", "readonly"):
            self.assertIn(capability, description)
        self.assertLess(len(description), 1000, "Discovery metadata should remain concise")

    def test_entrypoint_routes_conditional_work_without_loading_procedures(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(skill.encode("utf-8")), 12 * 1024)
        links = set(re.findall(r"\]\((references/[^)#]+)(?:#[^)]+)?\)", skill))
        self.assertTrue({
            "references/targeting-and-source.md", "references/patch-schema.md",
            "references/business-review.md", "references/evidence-and-performance.md",
            "references/frontend-compiler-contract.md", "references/session-workflows.md",
            "references/database-settings.md", "references/safety-contract.md",
        }.issubset(links))
        for conditional_tool in ("cancel_unexecuted_session(", "release_local_session(", "attach_edit_session("):
            self.assertNotIn(conditional_tool, skill, "Detailed signatures belong in references")

    def test_implicit_invocation_is_disabled(self) -> None:
        policy = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertRegex(policy, r"(?m)^policy:\s*\n\s+allow_implicit_invocation:\s*false\s*$")
        self.assertIn("$gxp-lowcode-edit", policy)

    def test_markdown_references_resolve_to_local_files_and_sections(self) -> None:
        checked = 0
        for source in SKILL_ROOT.rglob("*.md"):
            for target in re.findall(r"\]\(([^)]+)\)", source.read_text(encoding="utf-8")):
                with self.subTest(source=source.relative_to(SKILL_ROOT), target=target):
                    parsed = urlsplit(target)
                    self.assertFalse(parsed.scheme, "Skill instructions should route to local resources")
                    destination = (source.parent / unquote(parsed.path)).resolve() if parsed.path else source
                    self.assertTrue(destination.is_relative_to(SKILL_ROOT.resolve()))
                    self.assertTrue(destination.is_file(), f"Missing reference: {destination}")
                    if parsed.fragment:
                        headings = re.findall(r"(?m)^#{1,6}\s+(.+)$", destination.read_text(encoding="utf-8"))
                        anchors = {re.sub(r"[^\w\- ]", "", heading.lower()).replace(" ", "-") for heading in headings}
                        self.assertIn(unquote(parsed.fragment), anchors, "Missing referenced section")
                    checked += 1
        self.assertGreaterEqual(checked, 8)

    def test_core_protocol_identifiers_remain_discoverable(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        identifiers = set(re.findall(r"`([^`]+)`", skill))
        for identifier in (
            "$gxp-lowcode-edit", "conversation_id", "review_spec", 'scope_policy="strict"',
            'response_detail="summary"', "no_changes=true", "find_edit_nodes", "inspect_edit_context",
            "ready_for_approval", "write_ready", "prepare_write", "verified=true",
            "session_released=true", "publication_targets", "record_graph_review",
        ):
            self.assertIn(identifier, identifiers)

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
