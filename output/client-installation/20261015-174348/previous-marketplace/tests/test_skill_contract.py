from __future__ import annotations

import unittest
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = PLUGIN_ROOT / "skills" / "gxp-lowcode-edit"


class SkillContractTests(unittest.TestCase):
    def test_editor_requires_explicit_current_message_invocation(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("Explicit invocation gate", skill)
        self.assertIn("current user message explicitly invokes", skill)
        self.assertIn("is not an invocation", skill)
        self.assertIn("Do not call `editor_status`", skill)
        self.assertIn("readonly plan is only an input candidate", skill)

    def test_implicit_invocation_is_disabled(self) -> None:
        policy = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn("allow_implicit_invocation: false", policy)
        self.assertIn("$gxp-lowcode-edit", policy)


if __name__ == "__main__":
    unittest.main()
