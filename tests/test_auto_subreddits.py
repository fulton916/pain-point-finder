# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Unit tests for issue #22: subreddits chosen from the question when --subreddits is absent.

Order: --subreddits, then the reddit.topics map, then the archive's subreddit name search (largest
first, at most auto_max, at least min_subscribers), then reddit.subreddits. No network: the session
is a mock that answers the posts endpoint and the name-search endpoint by URL, and time.sleep is
patched out.
"""

import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import requests

import collect_reddit
import common
import ppf
from common import load_config

SINCE = datetime(2024, 11, 25, tzinfo=timezone.utc)
POSTS = "https://arctic-shift.photon-reddit.com/api/posts/search"
NAMES = "https://arctic-shift.photon-reddit.com/api/subreddits/search"
TIMEOUT_422 = {"data": None, "error": "Timeout. Maybe slow down a bit"}

CFG = {
    "reddit": {
        "endpoint": POSTS,
        "subreddit_search_endpoint": NAMES,
        "subreddits": ["salesforce", "SalesforceDeveloper", "salesforceadmin"],
        "topics": {
            "salesforce": ["salesforce", "SalesforceDeveloper", "salesforceadmin"],
            "running": ["running", "RunningShoeGeeks"],
            "marathon": ["running", "RunningShoeGeeks"],
            "veterans": ["VeteransBenefits", "Veterans"],
            "va claim": ["VeteransBenefits", "Veterans"],
            "fitness": ["Fitness"],
        },
        "min_subscribers": 10000,
        "auto_max": 3,
        "name_search_skip_words": ["best", "the", "a", "for"],
        "fields": ["id", "subreddit", "title", "selftext", "url", "score", "num_comments", "created_utc", "retrieved_on"],
        "search_limit": 100, "search_min_score": 0, "delay_seconds": 5,
        "timeout_retries": 2, "timeout_pause_seconds": 0, "body_max_chars": 2000,
    }
}


def sub(name, subscribers, over18=False, kind="public"):
    return {"display_name": name, "subscribers": subscribers, "over18": over18, "subreddit_type": kind,
            "public_description": "x", "_meta": {"num_posts": 1}}


# What the archive's name search answers per word (made up, shaped like the live answer of 2026-11-25).
NAME_ANSWERS = {
    "sourdough": [sub("Sourdough", 300000), sub("SourdoughStarter", 5000), sub("sourdough_after_dark", 80000, over18=True)],
    "starter": [sub("StarterPacks", 900000), sub("starter", 20000, kind="private")],
    "hydration": [sub("Hydration", 12000), sub("HydroHomies", 1000000)],
}


def post(subreddit):
    return {"id": f"id{subreddit.lower()}", "subreddit": subreddit, "title": f"A post in {subreddit}",
            "selftext": "text", "url": f"https://www.reddit.com/r/{subreddit}/comments/id{subreddit.lower()}/",
            "score": 5, "num_comments": 1, "created_utc": 1735689600, "retrieved_on": 1735689630}


def fake_response(status, payload):
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


def fake_archive(refuse_words=()):
    """A session.get that answers both endpoints by URL. Name-search words in refuse_words always get
    the 422 throttle answer; every posts request returns one post from the asked subreddit."""
    def get(url, params=None, headers=None, timeout=None):
        if url == NAMES:
            if params["subreddit"] in refuse_words:
                return fake_response(422, TIMEOUT_422)
            return fake_response(200, {"data": NAME_ANSWERS.get(params["subreddit"], [])})
        return fake_response(200, {"data": [post(params["subreddit"])]})
    return get


def session_with(refuse_words=()):
    session = mock.Mock()
    session.get.side_effect = fake_archive(refuse_words)
    return session


def calls(session, url):
    return [c.kwargs["params"] for c in session.get.call_args_list if c.args[0] == url]


def run(session, query, cfg=CFG, **options):
    info, notes = [], []
    with mock.patch.object(collect_reddit.time, "sleep"), mock.patch.object(common.time, "sleep"):
        records = collect_reddit.search(session, cfg, query, SINCE, info=info, notes=notes, **options)
    return records, info, notes


class TopicMapTest(unittest.TestCase):
    def test_whole_word_match_ignoring_case(self):
        self.assertEqual(collect_reddit.topic_subreddits(CFG, "best RUNNING shoes"),
                         (["running", "RunningShoeGeeks"], ["running"]))

    def test_phrase_key_must_appear_as_consecutive_words(self):
        self.assertEqual(collect_reddit.topic_subreddits(CFG, "VA claim rating increase"),
                         (["VeteransBenefits", "Veterans"], ["va claim"]))
        self.assertEqual(collect_reddit.topic_subreddits(CFG, "claim my va rating"), ([], []))

    def test_partial_words_do_not_match(self):
        self.assertEqual(collect_reddit.topic_subreddits(CFG, "runningshoes marathoner"), ([], []))

    def test_two_topics_are_joined_without_repeats(self):
        subs, matched = collect_reddit.topic_subreddits(CFG, "marathon running fitness plan")
        self.assertEqual(subs, ["running", "RunningShoeGeeks", "Fitness"])
        self.assertEqual(matched, ["running", "marathon", "fitness"])

    def test_no_topics_map_means_no_match(self):
        self.assertEqual(collect_reddit.topic_subreddits({"reddit": {}}, "running"), ([], []))

    def test_search_uses_topic_subreddits_and_no_name_search(self):
        session = session_with()
        records, info, notes = run(session, "best running shoes")
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["running", "RunningShoeGeeks"])
        self.assertEqual(calls(session, NAMES), [])
        self.assertEqual(len(records), 2)
        self.assertEqual(info, ["reddit searched r/running, r/RunningShoeGeeks (topic map in config.yaml matched 'running')."])
        self.assertEqual(notes, [])

    def test_salesforce_question_still_uses_the_three_configured_subreddits(self):
        session = session_with()
        run(session, "Salesforce flow fault email")
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["salesforce", "SalesforceDeveloper", "salesforceadmin"])
        self.assertEqual(calls(session, NAMES), [])


class NameSearchFallbackTest(unittest.TestCase):
    def test_words_worth_a_lookup(self):
        self.assertEqual(collect_reddit.name_search_words(CFG, "the best sourdough starter for a sourdough"),
                         ["sourdough", "starter"])
        self.assertEqual(collect_reddit.words_of("crème brûlée, VA_claim?"), ["crème", "brûlée", "va", "claim"])
        capped = {"reddit": dict(CFG["reddit"], name_search_max_words=2)}
        self.assertEqual(collect_reddit.name_search_words(capped, "one two three four"), ["one", "two"])
        uncapped = {"reddit": dict(CFG["reddit"], name_search_max_words=0)}
        self.assertEqual(len(collect_reddit.name_search_words(uncapped, "one two three four")), 4)

    def test_config_skip_words_are_all_strings_and_cover_yaml_boolean_words(self):
        """YAML reads bare on/off/yes/no as booleans; the config must quote them or they are not skipped."""
        rd = load_config()["reddit"]
        self.assertTrue(all(isinstance(w, str) for w in rd["name_search_skip_words"]), rd["name_search_skip_words"])
        self.assertEqual(collect_reddit.name_search_words({"reddit": rd}, "tips on sourdough, yes or no"), ["sourdough"])
        self.assertGreater(int(rd["name_search_max_words"]), 0)
        self.assertEqual(collect_reddit.name_search_words(CFG, "I am ok, x"), ["am", "ok"])  # one-letter words dropped
        no_list = {"reddit": {}}
        self.assertEqual(collect_reddit.name_search_words(no_list, "how is my sourdough"), ["sourdough"])

    def test_each_word_is_looked_up_then_the_largest_three_public_subreddits_are_searched(self):
        session = session_with()
        records, info, notes = run(session, "sourdough starter hydration")
        self.assertEqual([p["subreddit"] for p in calls(session, NAMES)], ["sourdough", "starter", "hydration"])
        for p in calls(session, NAMES):
            self.assertEqual(p["limit"], collect_reddit.NAME_SEARCH_LIMIT)
        # Largest first; capped at 3 (r/Hydration, 12,000, is fourth); SourdoughStarter is below
        # min_subscribers, sourdough_after_dark is over18, starter is private.
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["HydroHomies", "StarterPacks", "Sourdough"])
        for p in calls(session, POSTS):
            self.assertTrue(p["fields"])
        self.assertEqual(len(info), 1)
        self.assertIn("name search", info[0])
        self.assertIn("r/HydroHomies (1,000,000 subscribers), r/StarterPacks (900,000 subscribers), r/Sourdough (300,000 subscribers)", info[0])
        self.assertIn("'sourdough', 'starter', 'hydration'", info[0])
        self.assertIn("--subreddits", info[0])

    def test_auto_max_and_min_subscribers_come_from_config(self):
        cfg = copy.deepcopy(CFG)
        cfg["reddit"].update(auto_max=1)
        session = session_with()
        run(session, "sourdough starter hydration", cfg)
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["HydroHomies"])
        cfg["reddit"].update(auto_max=3, min_subscribers=950000)
        session = session_with()
        run(session, "sourdough starter hydration", cfg)
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["HydroHomies"])

    def test_delay_between_name_search_words_and_before_the_first_posts_request(self):
        session = session_with()
        with mock.patch("time.sleep") as sleep:  # collect_reddit.time and common.time are the same module
            collect_reddit.search(session, CFG, "sourdough starter hydration", SINCE)
        # 2 waits between 3 words, 1 before the posts requests, 2 between 3 subreddits.
        self.assertEqual(sleep.call_args_list, [mock.call(5.0)] * 5)

    def test_throttled_word_is_skipped_without_crashing_and_named(self):
        session = session_with(refuse_words={"starter"})
        records, info, notes = run(session, "sourdough starter hydration")
        self.assertEqual(calls(session, NAMES).count({"subreddit": "starter", "limit": 25}), 3)  # 1 try + 2 retries
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["HydroHomies", "Sourdough", "Hydration"])
        self.assertIn("refused the name search for 'starter'", info[0])
        self.assertEqual(notes, [])

    def test_every_word_throttled_falls_back_to_the_config_default(self):
        session = session_with(refuse_words={"sourdough", "starter", "hydration"})
        records, info, notes = run(session, "sourdough starter hydration")
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["salesforce", "SalesforceDeveloper", "salesforceadmin"])
        self.assertIn("config default", info[0])
        self.assertIn("refused the name search for 'sourdough', 'starter', 'hydration'", info[0])
        self.assertEqual(len(records), 3)

    def test_other_http_errors_from_the_name_search_still_raise(self):
        session = mock.Mock()
        bad = fake_response(500, {"error": "boom"})
        session.get.return_value = bad
        with mock.patch.object(collect_reddit, "get_json", side_effect=requests.HTTPError("HTTP 400", response=fake_response(400, {}))):
            with self.assertRaises(requests.HTTPError):
                collect_reddit.name_search_subreddits(session, CFG, ["sourdough"])


class ConfigDefaultTest(unittest.TestCase):
    def test_no_topic_and_nothing_found_uses_the_config_list_and_says_so(self):
        session = session_with()
        records, info, notes = run(session, "zzqqxx")
        self.assertEqual([p["subreddit"] for p in calls(session, NAMES)], ["zzqqxx"])
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["salesforce", "SalesforceDeveloper", "salesforceadmin"])
        self.assertIn("config default", info[0])
        self.assertIn("found no public subreddit with 10,000+ subscribers for 'zzqqxx'", info[0])

    def test_only_skip_words_means_no_name_search_request(self):
        session = session_with()
        records, info, notes = run(session, "best for the a")
        self.assertEqual(calls(session, NAMES), [])
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["salesforce", "SalesforceDeveloper", "salesforceadmin"])
        self.assertIn("no words worth a name search", info[0])

    def test_no_name_search_endpoint_means_no_name_search(self):
        cfg = copy.deepcopy(CFG)
        del cfg["reddit"]["subreddit_search_endpoint"]
        session = session_with()
        records, info, notes = run(session, "sourdough starter", cfg)
        self.assertEqual(calls(session, NAMES), [])
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["salesforce", "SalesforceDeveloper", "salesforceadmin"])
        self.assertIn("not configured", info[0])

    def test_nothing_at_all_configured_is_a_clear_error(self):
        cfg = {"reddit": dict(CFG["reddit"], subreddits=[], topics={})}
        del cfg["reddit"]["subreddit_search_endpoint"]
        with self.assertRaises(ValueError):
            run(session_with(), "sourdough", cfg)


class FlagOverrideTest(unittest.TestCase):
    def test_subreddits_option_beats_topic_map_and_name_search(self):
        session = session_with()
        records, info, notes = run(session, "best running shoes sourdough", subreddits=["sysadmin", "r/devops"])
        self.assertEqual(calls(session, NAMES), [])
        self.assertEqual([p["subreddit"] for p in calls(session, POSTS)], ["sysadmin", "devops"])
        self.assertEqual(info, ["reddit searched r/sysadmin, r/devops (from --subreddits)."])

    def test_search_without_info_or_notes_options_still_works(self):
        session = session_with()
        with mock.patch.object(collect_reddit.time, "sleep"):
            records = collect_reddit.search(session, CFG, "best running shoes", SINCE)
        self.assertEqual(len(records), 2)


def run_cli(argv, refuse_words=()):
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["store"] = ":memory:"
    cfg["reddit"].update(delay_seconds=0, timeout_pause_seconds=0)
    out = io.StringIO()
    with mock.patch.object(common, "load_config", return_value=cfg), \
         mock.patch.object(common, "load_env"), \
         mock.patch.object(requests.Session, "get", side_effect=fake_archive(refuse_words)) as get, \
         mock.patch.object(collect_reddit.time, "sleep"), \
         mock.patch.object(common.time, "sleep"), redirect_stdout(out):
        rc = ppf.main(argv)
    searched = [c.kwargs["params"]["subreddit"] for c in get.call_args_list if c.args[0] == POSTS]
    return rc, out.getvalue(), searched


def header(md):
    return next(line for line in md.splitlines() if line.startswith("Sources:"))


class CommandLineTest(unittest.TestCase):
    """ppf.py with the real config.yaml, mocked HTTP."""

    def test_running_shoes_header_names_running_and_runningshoegeeks(self):
        rc, out, searched = run_cli(["search", "best running shoes", "--sources", "rd", "--refresh"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(searched, ["running", "RunningShoeGeeks"])
        self.assertIn("reddit (2 records, fetched; reddit searched r/running, r/RunningShoeGeeks (topic map in config.yaml matched 'running'))", header(out))

    def test_va_claim_uses_veterans_subreddits_from_the_topic_map(self):
        rc, out, searched = run_cli(["search", "VA claim rating increase", "--sources", "rd", "--refresh", "--format", "json"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(searched, ["VeteransBenefits", "Veterans"])
        meta = json.loads(out)["meta"]
        self.assertIn("r/VeteransBenefits, r/Veterans (topic map", meta["per_source"]["reddit"]["note"])
        self.assertTrue(meta["note"].startswith("reddit searched r/VeteransBenefits, r/Veterans"), meta["note"])

    def test_no_topic_match_uses_the_name_search_and_the_header_says_so(self):
        rc, out, searched = run_cli(["search", "sourdough starter hydration", "--sources", "rd", "--refresh"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(searched, ["HydroHomies", "StarterPacks", "Sourdough"])
        self.assertIn("name search", header(out))
        self.assertIn("r/HydroHomies (1,000,000 subscribers)", header(out))

    def test_flag_overrides_the_automatic_choice(self):
        rc, out, searched = run_cli(["search", "best running shoes", "--sources", "rd", "--refresh", "--subreddits", "sysadmin"])
        self.assertEqual(rc, 0, out)
        self.assertEqual(searched, ["sysadmin"])
        self.assertIn("r/sysadmin (from --subreddits)", header(out))

    def test_the_note_does_not_stop_caching_but_a_skip_does(self):
        cfg = copy.deepcopy(load_config())
        cfg["reddit"].update(delay_seconds=0, timeout_pause_seconds=0)
        store = ppf.Store(":memory:")
        session = session_with()
        with mock.patch.object(collect_reddit.time, "sleep"), mock.patch.object(common, "load_env"):
            first = ppf.run_search(cfg, "best running shoes", ["rd"], "24m", False, 30, None, None, store=store, session=session)
            second = ppf.run_search(cfg, "best running shoes", ["rd"], "24m", False, 30, None, None, store=store, session=session)
        self.assertFalse(first["meta"]["per_source"]["reddit"]["from_cache"])
        self.assertTrue(second["meta"]["per_source"]["reddit"]["from_cache"])
        self.assertIn("topic map", first["meta"]["per_source"]["reddit"]["note"])

    def test_cache_scope_says_auto_when_no_subreddits_flag(self):
        cfg = load_config()
        self.assertIn("subreddits=auto", ppf.search_scope(cfg, "24m", None, None))
        self.assertIn("subreddits=sysadmin", ppf.search_scope(cfg, "24m", None, None, subreddits=["sysadmin"]))


if __name__ == "__main__":
    unittest.main()
