# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy,
# modify, or distribute this software without written permission.
"""Acceptance tests for issue #29: CI workflow on pull requests plus the README roadmap line.

Checked offline: the workflow file is valid YAML, triggers on pull requests and pushes to main,
installs requirements.txt and runs the unittest suite; the README has the roadmap line under the
intro. Whether the check actually runs green or red on GitHub cannot be tested here.
"""

import unittest

import yaml

from common import ROOT

WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
README = ROOT / "README.md"
ROADMAP = (
    "**Roadmap:** [open features](https://github.com/fulton916/pain-point-finder/issues"
    "?q=is%3Aissue+is%3Aopen+label%3Aenhancement)"
)


class CiWorkflowTest(unittest.TestCase):
    """Done when: a pull request against main shows a "tests" check that runs the unittest suite."""

    def setUp(self):
        self.assertTrue(WORKFLOW.is_file(), f"{WORKFLOW} is missing")
        self.text = WORKFLOW.read_text(encoding="utf-8")
        self.wf = yaml.safe_load(self.text)

    def test_tests_check_triggers_on_pull_request_and_push_to_main(self):
        self.assertEqual(self.wf.get("name"), "tests")
        # PyYAML reads a bare `on` key as boolean True.
        on = self.wf.get("on", self.wf.get(True))
        self.assertIsNotNone(on, "workflow has no `on` section")
        for event in ("pull_request", "push"):
            self.assertIn(event, on)
            self.assertIn("main", on[event].get("branches", []))

    def test_tests_check_installs_requirements_and_runs_unittest_suite(self):
        jobs = self.wf["jobs"]
        self.assertEqual(len(jobs), 1)
        job = next(iter(jobs.values()))
        self.assertTrue(str(job["runs-on"]).startswith("ubuntu"))
        steps = job["steps"]
        setup = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python"))
        self.assertEqual(str(setup["with"]["python-version"]), "3.12")
        runs = [s["run"] for s in steps if "run" in s]
        self.assertTrue(any("pip install -r requirements.txt" in r for r in runs), runs)
        self.assertTrue(any("python -m unittest discover -s tests" in r for r in runs), runs)
        self.assertTrue((ROOT / "requirements.txt").is_file(), "requirements.txt is missing")

    def test_workflow_starts_with_license_comment(self):
        self.assertTrue(self.text.startswith("# Copyright (c) 2026 Sean R Fulton."))


class ReadmeRoadmapTest(unittest.TestCase):
    """Done when: the README has the roadmap line under the intro."""

    def test_readme_has_roadmap_line_under_intro(self):
        text = README.read_text(encoding="utf-8")
        self.assertIn(ROADMAP, text)
        intro = text.split("\n## ", 1)[0]
        self.assertIn(ROADMAP, intro, "roadmap line must come before the first ## heading")


if __name__ == "__main__":
    unittest.main()
