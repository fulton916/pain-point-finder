# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy,
# modify, or distribute this software without written permission.
"""The skill and README must describe the search command as it really is."""

import re
import unittest

import ppf
from common import ROOT

SKILL = ROOT / "docs" / "skill" / "SKILL.md"
README = ROOT / "README.md"


def search_flags() -> list[str]:
    """Every --flag the search sub-command accepts, read from the parser itself."""
    parser = ppf.build_parser()
    sub = next(a for a in parser._actions if a.dest == "command")
    search = sub.choices["search"]
    return sorted(opt for a in search._actions for opt in a.option_strings if opt != "-h" and opt != "--help")


class SkillDocsTest(unittest.TestCase):
    def test_skill_file_exists_with_frontmatter(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\nname: pain-point-finder\n"), "skill needs name frontmatter")
        self.assertIn("description:", text.split("---")[1])

    def test_skill_and_readme_mention_every_search_flag(self):
        skill = SKILL.read_text(encoding="utf-8")
        readme = README.read_text(encoding="utf-8")
        for flag in search_flags():
            with self.subTest(flag=flag):
                self.assertIn(flag, skill, f"{flag} missing from SKILL.md")
                self.assertIn(flag, readme, f"{flag} missing from README.md")

    def test_skill_mentions_every_source_and_unit(self):
        skill = SKILL.read_text(encoding="utf-8")
        for short in ppf.SOURCES:
            self.assertIn(f"`{short}`", skill)
        for name in (m.SOURCE for m in ppf.SOURCES.values()):
            self.assertIn(name, skill)
            self.assertIn(ppf.UNITS[name], skill)

    def test_skill_documents_json_meta_keys(self):
        skill = SKILL.read_text(encoding="utf-8")
        for key in ("per_source", "merges", "dropped", "note", "requests_sent", "store_totals"):
            self.assertIn(f"`{key}`", skill)

    def test_skill_states_data_not_instructions(self):
        skill = SKILL.read_text(encoding="utf-8")
        self.assertRegex(skill, re.compile(r"data, never instructions", re.I))

    def test_readme_puts_search_before_sweep(self):
        readme = README.read_text(encoding="utf-8")
        self.assertLess(readme.index("## 2. Search mode"), readme.index("## 4. Sweep mode"))


if __name__ == "__main__":
    unittest.main()
