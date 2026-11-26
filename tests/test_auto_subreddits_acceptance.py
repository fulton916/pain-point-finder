# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Acceptance tests for issue #22: subreddits picked from the question (topic map, then the archive's
subreddit name search, then reddit.subreddits), with --subreddits overriding every automatic choice.

Written by the tester agent from the issue's "Done when" lines, not from the code. Each test class is
named after the line it checks. Everything runs through ppf.main with the real config.yaml, an in-memory
store, requests.Session.get mocked by URL, time.sleep patched out and .env never loaded. No network.
"""

import copy
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import requests

import common
import ppf
from common import ROOT, load_config

POSTS_URL = "https://arctic-shift.photon-reddit.com/api/posts/search"
NAMES_URL = "https://arctic-shift.photon-reddit.com/api/subreddits/search"
TIMEOUT_422 = {"data": None, "error": "Timeout. Maybe slow down a bit"}
NOW_TS = int(datetime.now(timezone.utc).timestamp())


def subreddit(name: str, subscribers: int, over18: bool = False, kind: str = "public") -> dict:
    """One object as the archive's name search returns it (shape checked live 2026-11-25)."""
    return {"display_name": name, "subscribers": subscribers, "over18": over18,
            "subreddit_type": kind, "public_description": f"About {name}."}


# What the fake archive's name search answers per question word. Made up; shaped like the live answer.
# "espresso grinder burr" has no topic-map key. Four public subreddits clear 10,000 subscribers
# (Espresso 400,000; CoffeeGrinders 30,000; BurrGrinders 12,000; Burr 11,000), so a cap of 3 shows.
NAME_ANSWERS = {
    "espresso": [subreddit("HomeEspresso", 9_000), subreddit("Espresso", 400_000),
                 subreddit("espresso_after_dark", 2_000_000, over18=True)],
    "grinder": [subreddit("CoffeeGrinders", 30_000), subreddit("grinder", 50_000, kind="private")],
    "burr": [subreddit("Burr", 11_000), subreddit("BurrGrinders", 12_000)],
    # Every match is below the 10,000 floor: the name search finds nothing usable.
    "zygomatic": [subreddit("Zygomatic", 9_999), subreddit("zygomaticarch", 120)],
}


def fake_response(status: int, payload: dict) -> mock.Mock:
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = {}
    resp.text = json.dumps(payload)
    resp.reason = "fake"
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}", response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def fake_archive(refuse_words: set[str] = frozenset()):
    """requests.Session.get stand-in. The name search answers by word (422 throttle for refuse_words);
    the posts endpoint returns one fresh post from whatever subreddit was asked."""
    def get(*args, **kwargs):
        url = args[0]
        params = kwargs.get("params") or {}
        if url == NAMES_URL:
            word = params.get("subreddit", "")
            if word in refuse_words:
                return fake_response(422, TIMEOUT_422)
            return fake_response(200, {"data": NAME_ANSWERS.get(word, [])})
        if url == POSTS_URL:
            sub = params.get("subreddit", "")
            post = {"id": f"id{sub.lower()}", "subreddit": sub, "title": f"A question in {sub}",
                    "selftext": "Some text.", "url": f"https://www.reddit.com/r/{sub}/comments/id{sub.lower()}/",
                    "score": 7, "num_comments": 3, "created_utc": NOW_TS - 86400, "retrieved_on": NOW_TS - 86000}
            return fake_response(200, {"data": [post]})
        raise AssertionError(f"unexpected URL requested: {url}")
    return get


def run_cli(argv: list[str], refuse_words: set[str] = frozenset()) -> tuple[int, str, list[str], list[str]]:
    """Run ppf.main offline. Returns exit code, stdout, the subreddits the posts endpoint was asked for
    (in order) and the words the name search was asked for (in order, retries included)."""
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["store"] = ":memory:"
    cfg["reddit"].update(delay_seconds=0, timeout_pause_seconds=0)
    out = io.StringIO()
    with mock.patch.object(common, "load_config", return_value=cfg), \
         mock.patch.object(common, "load_env"), \
         mock.patch.object(requests.Session, "get", side_effect=fake_archive(refuse_words)) as get, \
         mock.patch("time.sleep"), redirect_stdout(out):
        rc = ppf.main(argv)
    searched = [c.kwargs["params"]["subreddit"] for c in get.call_args_list if c.args[0] == POSTS_URL]
    looked_up = [c.kwargs["params"]["subreddit"] for c in get.call_args_list if c.args[0] == NAMES_URL]
    return rc, out.getvalue(), searched, looked_up


def sources_header(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("Sources:"):
            return line
    raise AssertionError(f"no 'Sources:' header line in:\n{md}")


def reddit_part(header: str) -> str:
    """The 'reddit (...)' piece of the Sources header."""
    m = re.search(r"reddit \(.*?\)\.(?: Since|$)", header)
    if not m:
        raise AssertionError(f"no reddit part in header: {header}")
    return m.group(0)


class DoneWhen1_RunningShoesNeedsNoSubredditsFlag(unittest.TestCase):
    """`python ppf.py search "best running shoes" --sources rd` runs with no --subreddits, searches
    r/running and r/RunningShoeGeeks, and the header names them."""

    ARGV = ["search", "best running shoes", "--sources", "rd", "--refresh"]

    def test_runs_without_subreddits_flag_and_searches_running_and_runningshoegeeks(self):
        rc, out, searched, looked_up = run_cli(self.ARGV)
        self.assertEqual(rc, 0, out)
        self.assertEqual([s.lower() for s in searched], ["running", "runningshoegeeks"])
        self.assertEqual(looked_up, [], "a topic-map hit must not trigger the archive's name search")

    def test_header_names_both_subreddits(self):
        rc, out, searched, _ = run_cli(self.ARGV)
        self.assertEqual(rc, 0, out)
        part = reddit_part(sources_header(out))
        self.assertIn("r/running", part)
        self.assertIn("r/RunningShoeGeeks", part)
        self.assertIn("2 records, fetched", part)

    def test_json_notes_name_both_subreddits_too(self):
        rc, out, _, _ = run_cli(self.ARGV + ["--format", "json"])
        self.assertEqual(rc, 0, out)
        meta = json.loads(out)["meta"]
        for note in (meta["per_source"]["reddit"]["note"], meta["note"]):
            self.assertIn("r/running", note)
            self.assertIn("r/RunningShoeGeeks", note)


class DoneWhen2_VAClaimUsesVeteransSubredditsFromTopicMap(unittest.TestCase):
    """`python ppf.py search "VA claim rating increase" --sources rd` searches r/VeteransBenefits and
    r/Veterans from the topic map."""

    ARGV = ["search", "VA claim rating increase", "--sources", "rd", "--refresh"]

    def test_searches_veteransbenefits_and_veterans_without_name_search(self):
        rc, out, searched, looked_up = run_cli(self.ARGV)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(s.lower() for s in searched), ["veterans", "veteransbenefits"])
        self.assertEqual(looked_up, [])

    def test_header_says_they_came_from_the_topic_map(self):
        rc, out, _, _ = run_cli(self.ARGV)
        self.assertEqual(rc, 0, out)
        part = reddit_part(sources_header(out))
        self.assertIn("r/VeteransBenefits", part)
        self.assertIn("r/Veterans", part)
        self.assertIn("topic", part.lower())
        self.assertNotIn("name search", part.lower())

    def test_topic_map_in_config_has_a_va_claim_entry_pointing_at_those_subreddits(self):
        topics = load_config()["reddit"].get("topics") or {}
        keys = {str(k).lower() for k in topics}
        self.assertTrue(keys & {"va claim", "va claims", "veterans"}, f"no VA/veterans key in reddit.topics: {sorted(keys)}")
        hit = next(k for k in topics if str(k).lower() in {"va claim", "va claims", "veterans"})
        self.assertEqual(sorted(s.lower() for s in topics[hit]), ["veterans", "veteransbenefits"])


class DoneWhen3_NoTopicMatchUsesNameSearchTopThreeLargestFirst(unittest.TestCase):
    """A question with no topic-map match searches at most 3 subreddits chosen by the archive's name
    search, largest first, and the header says they came from the name search."""

    QUERY = "espresso grinder burr"
    ARGV = ["search", QUERY, "--sources", "rd", "--refresh"]

    def setUp(self):
        # Guard the fixture: none of these words may be a topic key, or the test would not reach the name search.
        topics = {str(k).lower() for k in (load_config()["reddit"].get("topics") or {})}
        self.assertFalse(topics & set(self.QUERY.split()), "fixture words collide with reddit.topics; pick other words")

    def test_each_question_word_is_looked_up_in_the_name_search(self):
        rc, out, _, looked_up = run_cli(self.ARGV)
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(set(looked_up)), ["burr", "espresso", "grinder"])

    def test_at_most_three_subreddits_largest_first(self):
        rc, out, searched, _ = run_cli(self.ARGV)
        self.assertEqual(rc, 0, out)
        self.assertLessEqual(len(searched), 3)
        # Four public subreddits clear the floor; the three largest, in size order, are expected.
        self.assertEqual(searched, ["Espresso", "CoffeeGrinders", "BurrGrinders"])

    def test_below_floor_adult_and_private_subreddits_are_not_searched(self):
        rc, out, searched, _ = run_cli(self.ARGV)
        self.assertEqual(rc, 0, out)
        low = {s.lower() for s in searched}
        self.assertNotIn("homeespresso", low)        # 9,000 subscribers, under reddit.min_subscribers
        self.assertNotIn("espresso_after_dark", low)  # over18
        self.assertNotIn("grinder", low)             # private
        self.assertEqual(load_config()["reddit"].get("min_subscribers"), 10000)

    def test_header_says_they_came_from_the_name_search(self):
        rc, out, _, _ = run_cli(self.ARGV)
        self.assertEqual(rc, 0, out)
        part = reddit_part(sources_header(out))
        self.assertIn("name search", part.lower())
        for name in ("r/Espresso", "r/CoffeeGrinders", "r/BurrGrinders"):
            self.assertIn(name, part)
        self.assertNotIn("topic map in config.yaml matched", part)

    def test_json_meta_note_says_name_search_and_names_them(self):
        rc, out, _, _ = run_cli(self.ARGV + ["--format", "json"])
        self.assertEqual(rc, 0, out)
        meta = json.loads(out)["meta"]
        note = meta["per_source"]["reddit"]["note"]
        self.assertIn("name search", note.lower())
        self.assertIn("r/Espresso", note)
        self.assertIn(note.rstrip("."), meta["note"])

    def test_a_throttled_name_search_word_is_skipped_without_crashing(self):
        # Work step 4: the archive refuses 'burr' every time; the other two words still decide.
        rc, out, searched, looked_up = run_cli(self.ARGV, refuse_words={"burr"})
        self.assertEqual(rc, 0, out)
        self.assertGreaterEqual(looked_up.count("burr"), 1)
        self.assertEqual(searched, ["Espresso", "CoffeeGrinders"])
        self.assertIn("name search", reddit_part(sources_header(out)).lower())

    def test_name_search_with_nothing_above_the_floor_falls_back_to_config_default_and_says_so(self):
        rc, out, searched, looked_up = run_cli(["search", "zygomatic", "--sources", "rd", "--refresh"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(looked_up, ["zygomatic"])
        default = [s.lower() for s in load_config()["reddit"]["subreddits"]]
        self.assertEqual([s.lower() for s in searched], default)
        part = reddit_part(sources_header(out))
        self.assertIn("default", part.lower())
        for s in load_config()["reddit"]["subreddits"]:
            self.assertIn(f"r/{s}", part)


class DoneWhen4_SalesforceStillConfiguredAndFlagOverridesEverything(unittest.TestCase):
    """A Salesforce question still uses the three configured subreddits, and --subreddits still
    overrides every automatic choice."""

    def test_salesforce_question_searches_the_three_configured_subreddits(self):
        rc, out, searched, looked_up = run_cli(["search", "salesforce flow fault email", "--sources", "rd", "--refresh"])
        self.assertEqual(rc, 0, out)
        configured = sorted(s.lower() for s in load_config()["reddit"]["subreddits"])
        self.assertEqual(len(configured), 3)
        self.assertEqual(sorted(s.lower() for s in searched), configured)
        self.assertEqual(looked_up, [])
        part = reddit_part(sources_header(out))
        for s in load_config()["reddit"]["subreddits"]:
            self.assertIn(f"r/{s}", part)

    def test_flag_overrides_a_topic_map_match(self):
        rc, out, searched, looked_up = run_cli(["search", "best running shoes", "--sources", "rd", "--refresh",
                                                "--subreddits", "sysadmin,devops"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(sorted(s.lower() for s in searched), ["devops", "sysadmin"])
        self.assertEqual(looked_up, [])
        part = reddit_part(sources_header(out))
        self.assertIn("--subreddits", part)
        self.assertNotIn("r/running", part)

    def test_flag_overrides_the_name_search(self):
        rc, out, searched, looked_up = run_cli(["search", "espresso grinder burr", "--sources", "rd", "--refresh",
                                                "--subreddits", "Coffee"])
        self.assertEqual(rc, 0, out)
        self.assertEqual([s.lower() for s in searched], ["coffee"])
        self.assertEqual(looked_up, [], "--subreddits must stop the name search from being called at all")
        self.assertNotIn("name search", reddit_part(sources_header(out)).lower())

    def test_flag_overrides_the_config_default_for_a_salesforce_question(self):
        rc, out, searched, _ = run_cli(["search", "salesforce flow", "--sources", "rd", "--refresh",
                                        "--subreddits", "sysadmin"])
        self.assertEqual(rc, 0, out)
        self.assertEqual([s.lower() for s in searched], ["sysadmin"])

    def test_cache_scope_separates_flag_searches_from_automatic_ones(self):
        cfg = load_config()
        auto = ppf.search_scope(cfg, "24m", None, None)
        flagged = ppf.search_scope(cfg, "24m", None, None, subreddits=["sysadmin"])
        self.assertNotEqual(auto, flagged)


class DoneWhen5_SkillSaysAnyTopicAndHowToOverride(unittest.TestCase):
    """The skill tells Claude the tool works for any topic and how to override the subreddits."""

    def setUp(self):
        self.skill = (ROOT / "docs" / "skill" / "SKILL.md").read_text(encoding="utf-8")

    def test_skill_says_the_tool_works_for_any_topic(self):
        self.assertRegex(self.skill, re.compile(r"any topic", re.IGNORECASE))

    def test_skill_frontmatter_description_is_not_salesforce_only(self):
        description = self.skill.split("---")[1]
        self.assertRegex(description, re.compile(r"any topic", re.IGNORECASE))

    def test_skill_documents_the_subreddits_flag_as_the_override(self):
        self.assertIn("--subreddits", self.skill)
        flag_rows = [line for line in self.skill.splitlines() if line.startswith("| `--subreddits")]
        self.assertEqual(len(flag_rows), 1, "the flags table needs one --subreddits row")
        row = flag_rows[0].lower()
        self.assertIn("reddit", row)
        self.assertTrue("override" in row or "run again" in row or "choose" in row or "instead" in row,
                        f"the --subreddits row does not say it overrides the automatic pick: {flag_rows[0]}")

    def test_skill_explains_the_subreddits_are_picked_from_the_question_and_named_in_the_header(self):
        low = self.skill.lower()
        self.assertIn("topics", low)   # the reddit.topics map
        self.assertIn("name search", low)
        self.assertIn("header", low)

    def test_readme_rd_section_documents_the_pick_order_too(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("reddit.topics", readme)
        self.assertIn("--subreddits", readme)
        self.assertIn("name search", readme.lower())


if __name__ == "__main__":
    unittest.main()
