# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Acceptance tests for issue #23: the Stack Exchange site picked from the question when --site is absent.

Written by the tester agent from the issue's "Done when" lines, not from the code. Each test class is
named after the line it checks. Everything runs through ppf.main with the real config.yaml (the store
path switched to ":memory:"), requests.Session.get mocked by URL so common.get_json runs for real,
time.sleep patched out and .env never loaded. No network, no key.
"""

import copy
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import requests

import collect_se
import common
import ppf
from common import ROOT, load_config

SE_URL = "https://api.stackexchange.com/2.3/search/advanced"
HN_URL = "https://hn.algolia.com/api/v1/search"
GH_URL = "https://api.github.com/search/issues"
SKILL = ROOT / "docs" / "skill" / "SKILL.md"
INSTALLED_SKILL = Path.home() / ".claude" / "skills" / "pain-point-finder" / "SKILL.md"
README = ROOT / "README.md"

# One question as the API returns it (shape from the v2.3 docs). Made up.
SE_ITEM = {
    "link": "https://cooking.stackexchange.com/questions/4242/how-do-i-keep-bread-from-going-stale",
    "title": "How do I keep bread from going stale?",
    "body": "<p>My loaf is hard the next morning.</p>",
    "score": 12, "answer_count": 3, "view_count": 900,
    "creation_date": 1735689600, "last_activity_date": 1735689600, "is_answered": True,
}
SE_PAYLOAD = {"items": [SE_ITEM], "has_more": False, "quota_max": 300, "quota_remaining": 250}


def fake_response(status: int, payload: dict) -> mock.Mock:
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = {}
    resp.text = json.dumps(payload)
    resp.reason = "fake"
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}", response=resp)
    return resp


class FakeNetwork:
    """Answers requests.Session.get by URL and records the Stack Exchange `site` parameters sent.
    A plain callable patched onto the class is not bound, so there is no `self` session argument."""

    def __init__(self):
        self.sites_sent: list[str] = []
        self.urls: list[str] = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        self.urls.append(url)
        if url == SE_URL:
            self.sites_sent.append((params or {}).get("site"))
            return fake_response(200, SE_PAYLOAD)
        if url == HN_URL:
            return fake_response(200, {"hits": [], "nbPages": 0})
        if url == GH_URL:
            return fake_response(200, {"items": [], "total_count": 0})
        raise AssertionError(f"unexpected request to {url}")


class AcceptanceBase(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.cfg["paths"]["store"] = ":memory:"
        self.net = FakeNetwork()
        patches = [
            mock.patch.object(requests.Session, "get", self.net),
            mock.patch.object(common, "load_config", return_value=copy.deepcopy(self.cfg)),
            mock.patch.object(common, "load_env"),
            mock.patch.object(collect_se.time, "sleep"),
            mock.patch.object(common.time, "sleep"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_cli(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out):
            rc = ppf.main(["search", *argv, "--refresh"])
        return rc, out.getvalue()

    def header_of(self, text: str) -> str:
        """The markdown header: the third line, which names each source and what it searched."""
        return text.splitlines()[2]


class BreadQuestionSearchesCookingAndTheHeaderSaysSo(AcceptanceBase):
    """Done when: `python ppf.py search "how to keep bread from going stale" --sources se` searches
    cooking.stackexchange.com and the header says so."""

    def test_the_api_is_asked_for_the_cooking_site(self):
        rc, _ = self.run_cli("how to keep bread from going stale", "--sources", "se")
        self.assertEqual(rc, 0)
        self.assertEqual(self.net.sites_sent, ["cooking"])
        self.assertEqual(self.net.urls, [SE_URL], "only the Stack Exchange endpoint should be called")

    def test_the_markdown_header_names_cooking_stackexchange_com(self):
        _, text = self.run_cli("how to keep bread from going stale", "--sources", "se")
        header = self.header_of(text)
        self.assertIn("stackexchange", header)
        self.assertIn("cooking.stackexchange.com", header)
        # The header should not claim a site that was not searched.
        self.assertNotIn("salesforce.stackexchange.com", header)

    def test_json_per_source_note_names_cooking(self):
        rc, text = self.run_cli("how to keep bread from going stale", "--sources", "se", "--format", "json")
        self.assertEqual(rc, 0)
        meta = json.loads(text)["meta"]
        self.assertIn("cooking.stackexchange.com", meta["per_source"]["stackexchange"]["note"])
        self.assertIn("cooking.stackexchange.com", meta["note"])
        self.assertEqual(self.net.sites_sent, ["cooking"])

    def test_the_result_rows_come_from_the_cooking_site(self):
        _, text = self.run_cli("how to keep bread from going stale", "--sources", "se")
        self.assertIn("How do I keep bread from going stale?", text)
        self.assertIn("https://cooking.stackexchange.com/questions/4242/", text)


class SalesforceQuestionWithNoFlagsStillSearchesSalesforce(AcceptanceBase):
    """Done when: a Salesforce question with no flags still searches the salesforce site."""

    def test_salesforce_word_searches_salesforce_site(self):
        self.run_cli("Salesforce report export limit", "--sources", "se")
        self.assertEqual(self.net.sites_sent, ["salesforce"])

    def test_flow_question_searches_salesforce_site(self):
        _, text = self.run_cli("flow fault email goes to the wrong user", "--sources", "se")
        self.assertEqual(self.net.sites_sent, ["salesforce"])
        self.assertIn("salesforce.stackexchange.com", self.header_of(text))

    def test_apex_question_searches_salesforce_site(self):
        self.run_cli("apex trigger recursion", "--sources", "se")
        self.assertEqual(self.net.sites_sent, ["salesforce"])

    def test_salesforce_question_without_a_mapped_word_falls_back_to_salesforce(self):
        # Nothing in this question is a key of stackexchange.sites; the default must still be salesforce.
        _, text = self.run_cli("report filter on a lookup field", "--sources", "se")
        self.assertEqual(self.net.sites_sent, ["salesforce"])
        self.assertIn("salesforce.stackexchange.com", self.header_of(text))

    def test_no_flags_at_all_with_default_sources_still_searches_salesforce(self):
        # No --sources, no --site: the config default source list (hn, gh, se) and the default site.
        rc, text = self.run_cli("salesforce flow fault email")
        self.assertEqual(rc, 0)
        self.assertIn(SE_URL, self.net.urls)
        self.assertEqual(self.net.sites_sent, ["salesforce"])
        self.assertIn("salesforce.stackexchange.com", self.header_of(text))

    def test_config_default_site_is_salesforce(self):
        self.assertEqual(self.cfg["stackexchange"]["site"], "salesforce")


class SiteFlagStillOverridesTheAutomaticChoice(AcceptanceBase):
    """Done when: `--site` still overrides the automatic choice."""

    def test_site_flag_beats_a_map_match(self):
        _, text = self.run_cli("how to keep bread from going stale", "--sources", "se", "--site", "stackoverflow")
        self.assertEqual(self.net.sites_sent, ["stackoverflow"])
        header = self.header_of(text)
        self.assertIn("stackoverflow", header)
        self.assertNotIn("cooking.stackexchange.com", header)

    def test_site_flag_beats_the_salesforce_default(self):
        _, text = self.run_cli("report filter on a lookup field", "--sources", "se", "--site", "cooking")
        self.assertEqual(self.net.sites_sent, ["cooking"])
        self.assertIn("cooking.stackexchange.com", self.header_of(text))

    def test_site_flag_beats_a_salesforce_word(self):
        self.run_cli("salesforce flow fault email", "--sources", "se", "--site", "diy")
        self.assertEqual(self.net.sites_sent, ["diy"])

    def test_header_says_the_site_came_from_the_flag(self):
        _, text = self.run_cli("how to keep bread from going stale", "--sources", "se", "--site", "stackoverflow")
        self.assertIn("--site", self.header_of(text))

    def test_json_note_says_the_site_came_from_the_flag(self):
        _, text = self.run_cli("how to keep bread from going stale", "--sources", "se", "--site", "stackoverflow",
                               "--format", "json")
        note = json.loads(text)["meta"]["per_source"]["stackexchange"]["note"]
        self.assertIn("stackoverflow", note)
        self.assertIn("--site", note)


class SkillTellsClaudeWhichSitesTheMapCoversAndHowToPassSite(unittest.TestCase):
    """Done when: the skill tells Claude which sites the map covers and how to pass `--site` for one it
    does not."""

    def setUp(self):
        self.skill = SKILL.read_text(encoding="utf-8")
        self.sites = load_config()["stackexchange"]["sites"]

    def test_skill_names_every_site_the_map_covers(self):
        for site in sorted(set(self.sites.values())):
            with self.subTest(site=site):
                self.assertRegex(self.skill, rf"\b{re.escape(site)}\b", f"site {site!r} is in config.yaml but not in SKILL.md")

    def test_skill_lists_every_topic_word_in_the_map(self):
        for key in self.sites:
            with self.subTest(key=key):
                self.assertIn(f"`{key}`", self.skill, f"map key {key!r} is in config.yaml but not in SKILL.md")

    def test_skill_explains_passing_site_for_a_site_the_map_does_not_cover(self):
        self.assertIn("--site", self.skill)
        # An example of the flag with a site name, and where to find the names for the other sites.
        self.assertRegex(self.skill, r"--site \w+")
        self.assertTrue("api.stackexchange.com/2.3/sites" in self.skill or "api_site_parameter" in self.skill,
                        "the skill should say where the list of site names comes from")

    def test_skill_says_what_happens_when_no_word_matches(self):
        self.assertIn("salesforce.stackexchange.com", self.skill)

    def test_skill_says_the_header_names_the_site_searched(self):
        self.assertIn("header", self.skill)
        self.assertIn("cooking.stackexchange.com", self.skill)

    @unittest.skipUnless(INSTALLED_SKILL.exists(), "no installed skill copy on this machine (for example a CI runner)")
    def test_installed_skill_copy_is_identical(self):
        self.assertEqual(INSTALLED_SKILL.read_text(encoding="utf-8"), self.skill,
                         "docs/skill/SKILL.md and the installed copy differ")


class ReadmeDocumentsTheSiteMap(unittest.TestCase):
    """Work step 4 (not a Done when line, reported separately): the README `se` section describes the map."""

    def test_readme_names_the_map_the_default_and_the_flag(self):
        readme = README.read_text(encoding="utf-8")
        self.assertIn("stackexchange.sites", readme)
        self.assertIn("--site", readme)
        self.assertIn("cooking.stackexchange.com", readme)
        self.assertIn("salesforce", readme)


if __name__ == "__main__":
    unittest.main()
