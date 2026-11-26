# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Acceptance tests for issue #24: Bluesky source (`bs`) via the public app.bsky.feed.searchPosts API.

Each test class is named after the "Done when" line it checks. No network: requests.Session.get is
mocked by URL (the whole stack from ppf.main down through common.get_json runs), time.sleep is
patched out and common.load_env is mocked so no .env is read and no key is available.

Naming: the short name is `bs` (the key in ppf.SOURCES); the long name used in config.yaml and in the
printed output is `bluesky` (collect_bluesky.SOURCE), the same convention as `rd`/`reddit`. The issue
text writes `scoring.source_thresholds.bs`; the codebase's convention puts it under `bluesky`.
"""

import copy
import io
import json
import re
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

import collect_bluesky
import collect_reddit
import common
import merge_and_score
import ppf
from common import ROOT, load_config

NOW = datetime.now(timezone.utc).replace(microsecond=0)
POSTED = (NOW - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
POSTED_DATE = POSTED[:10]

# Shape of one post view as the live API returns it (issue #24), made-up values.
POST = {
    "uri": "at://did:plc:j2kmiyhld5btzozgzwy3lc2m/app.bsky.feed.post/3n33smmejik2s",
    "author": {"did": "did:plc:j2kmiyhld5btzozgzwy3lc2m", "handle": "sky.skymarchini.net"},
    "record": {
        "$type": "app.bsky.feed.post",
        "createdAt": POSTED,
        "langs": ["en"],
        "text": "Fellow running idiots: I need new running shoes...",
    },
    "replyCount": 22,
    "repostCount": 0,
    "likeCount": 41,
    "quoteCount": 1,
    "indexedAt": POSTED,
}
BSKY_LINK = "https://bsky.app/profile/sky.skymarchini.net/post/3n33smmejik2s"

REDDIT_POST = {
    "id": "abc123",
    "subreddit": "running",
    "title": "Which running shoes stop my heel blisters",
    "selftext": "Every pair I try rubs the heel raw after 5k.",
    "url": "https://www.reddit.com/r/running/comments/abc123/which_running_shoes/",
    "score": 42,
    "num_comments": 9,
    "created_utc": int((NOW - timedelta(days=3)).timestamp()),
    "retrieved_on": int((NOW - timedelta(days=3)).timestamp()) + 30,
}

SEARCH_POSTS_PATH = "/xrpc/app.bsky.feed.searchPosts"


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


def fake_internet(bluesky_posts: list[dict], reddit_posts: list[dict] | None = None):
    """requests.Session.get stand-in. Bluesky's searchPosts (either host) answers with bluesky_posts and
    no cursor; the Reddit archive's posts search answers with reddit_posts; its subreddit name search
    answers empty. Anything else is a test failure: no live call may slip through."""
    def get(*args, **kwargs):
        url = args[0] if args else kwargs["url"]
        if url.endswith(SEARCH_POSTS_PATH):
            return fake_response(200, {"posts": bluesky_posts, "hitsTotal": len(bluesky_posts)})
        if url.endswith("/api/posts/search"):
            return fake_response(200, {"data": list(reddit_posts or [])})
        if url.endswith("/api/subreddits/search"):
            return fake_response(200, {"data": []})
        raise AssertionError(f"unexpected URL requested: {url}")
    return get


def run_cli(argv: list[str], bluesky_posts: list[dict], reddit_posts: list[dict] | None = None):
    """Run ppf.main offline with an in-memory store, no delays, and no .env. Returns the exit code,
    stdout and the mocked Session.get."""
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["store"] = ":memory:"
    cfg["bluesky"]["delay_seconds"] = 0
    cfg["reddit"].update(delay_seconds=0, timeout_pause_seconds=0)
    collect_bluesky.REFUSED.clear()
    out = io.StringIO()
    with mock.patch.object(common, "load_config", return_value=cfg), \
         mock.patch.object(common, "load_env"), \
         mock.patch.dict("os.environ", {}, clear=True), \
         mock.patch.object(requests.Session, "get", side_effect=fake_internet(bluesky_posts, reddit_posts)) as get, \
         mock.patch("time.sleep"), redirect_stdout(out):
        rc = ppf.main(argv)
    collect_bluesky.REFUSED.clear()
    return rc, out.getvalue(), get


def sources_header(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("Sources:"):
            return line
    raise AssertionError(f"no 'Sources:' header line in:\n{md}")


class DoneWhen1_SearchBsSince3mReturnsLikesRepliesDateAndLinkWithNoKey(unittest.TestCase):
    """python ppf.py search "best running shoes" --sources bs --since 3m returns posts with likes,
    replies, date and a link that opens the post on bsky.app, with no key in .env."""

    def setUp(self):
        self.rc, self.text, self.get = run_cli(
            ["search", "best running shoes", "--sources", "bs", "--since", "3m", "--refresh"], [POST])

    def test_exit_code_is_0_and_bluesky_answered_in_the_header(self):
        self.assertEqual(self.rc, 0, self.text)
        self.assertRegex(sources_header(self.text), r"bluesky \(1 records, fetched")

    def test_the_row_shows_likes_replies_and_the_post_date(self):
        self.assertIn(f"41 {ppf.UNITS['bluesky']}", self.text)
        self.assertEqual(ppf.UNITS["bluesky"], "likes")
        self.assertIn("22 replies", self.text)
        self.assertIn(f"date {POSTED_DATE}", self.text)

    def test_the_link_opens_the_post_on_bsky_app_built_from_handle_and_rkey(self):
        self.assertIn(f"]({BSKY_LINK})", self.text)
        self.assertRegex(BSKY_LINK, r"^https://bsky\.app/profile/[^/]+/post/[^/]+$")

    def test_the_request_went_to_the_bluesky_search_endpoint_with_the_words_and_a_3m_since(self):
        calls = [c for c in self.get.call_args_list if c.args[0].endswith(SEARCH_POSTS_PATH)]
        self.assertTrue(calls, "no request reached app.bsky.feed.searchPosts")
        first = calls[0]
        host = first.args[0].split("//", 1)[1].split("/", 1)[0]
        self.assertIn(host, ("public.api.bsky.app", "api.bsky.app"))
        params = first.kwargs["params"]
        self.assertEqual(params["q"], "best running shoes")
        self.assertTrue(1 <= int(params["limit"]) <= 100, params)
        # --since 3m: the API's `since` is an ISO date three months back (same calendar date as parse_since).
        expected = {common.parse_since("3m", NOW).date().isoformat(),
                    common.parse_since("3m", datetime.now(timezone.utc)).date().isoformat()}
        self.assertIn(params["since"][:10], expected, params)

    def test_no_key_is_sent_and_no_env_is_needed(self):
        for call in self.get.call_args_list:
            headers = call.kwargs.get("headers") or {}
            self.assertNotIn("Authorization", headers)
            self.assertFalse(any(k.lower().startswith("x-api") for k in headers), headers)
        self.assertNotIn("bluesky (failed", self.text)

    def test_bs_is_registered_in_ppf_sources_and_units(self):
        self.assertIs(ppf.SOURCES["bs"], collect_bluesky)
        self.assertEqual(collect_bluesky.SOURCE, "bluesky")
        self.assertEqual(ppf.UNITS["bluesky"], "likes")
        self.assertEqual(ppf.parse_sources("bs"), ["bs"])
        self.assertNotIn("bs", load_config()["search"]["default_sources"])

    def test_empty_result_prints_no_results_not_an_error(self):
        rc, text, _ = run_cli(["search", "zxqv nothing", "--sources", "bs", "--since", "3m", "--refresh"], [])
        self.assertEqual(rc, 0, text)
        self.assertRegex(sources_header(text), r"bluesky \(0 records, fetched")
        self.assertIn("No results.", text)


class DoneWhen2_NoBlueskyRowAboveModerateAndThresholdsInConfig(unittest.TestCase):
    """No Bluesky row is labeled above Moderate, and the thresholds live in config.yaml."""

    def setUp(self):
        self.cfg = load_config()
        self.scoring = self.cfg["scoring"]

    def test_thresholds_for_bluesky_live_in_config_moderate_10_strong_50(self):
        t = self.scoring["source_thresholds"]["bluesky"]
        self.assertEqual(t, {"moderate": 10, "strong": 50})

    def test_label_cap_for_bluesky_is_moderate_in_config(self):
        self.assertEqual(self.scoring["label_caps"]["bluesky"], "Moderate")

    def rank(self, scoring: dict, likes_list: list[int]) -> list[dict]:
        posts = []
        for i, likes in enumerate(likes_list):
            posts.append(dict(POST, uri=f"at://did:plc:x/app.bsky.feed.post/rk{i}", likeCount=likes, replyCount=likes,
                              record=dict(POST["record"], text=f"Distinct problem number {i}: {'x' * (i + 1)} shoe trouble")))
        records = collect_bluesky.posts_to_records(posts, NOW.isoformat(), 2000)
        rows, _ = merge_and_score.rank(records, scoring, as_of=NOW, mode="search")
        return rows

    def test_a_bluesky_row_far_above_the_strong_threshold_is_still_moderate(self):
        rows = self.rank(self.scoring, [100000, 51, 50])
        self.assertEqual(len(rows), 3)
        for r in rows:
            self.assertEqual(r["evidence_label"], "Moderate", r)

    def test_labels_follow_the_config_thresholds_moderate_at_10_anecdotal_below(self):
        rows = self.rank(self.scoring, [10, 9, 0])
        by_score = {r["raw_score"]: r["evidence_label"] for r in rows}
        self.assertEqual(by_score, {10: "Moderate", 9: "Anecdotal", 0: "Anecdotal"})

    def test_the_cap_comes_from_config_not_code(self):
        """Remove the cap from a copy of the config: the same row becomes Strong, so the cap is read from
        config.yaml rather than being hard-wired."""
        scoring = copy.deepcopy(self.scoring)
        del scoring["label_caps"]["bluesky"]
        rows = self.rank(scoring, [100000])
        self.assertEqual(rows[0]["evidence_label"], "Strong")

    def test_cli_output_never_prints_strong_for_a_bluesky_only_row(self):
        hot = dict(POST, likeCount=250000, replyCount=9000)
        rc, text, _ = run_cli(["search", "best running shoes", "--sources", "bs", "--since", "3m", "--refresh"], [hot])
        self.assertEqual(rc, 0, text)
        self.assertIn("**Moderate** · bluesky", text)
        self.assertNotIn("**Strong**", text)


class DoneWhen3_SourcesRdBsRunsBothAndPerSourceCountsShowInHeader(unittest.TestCase):
    """--sources rd,bs runs both and the per-source counts show in the header."""

    def setUp(self):
        self.rc, self.text, self.get = run_cli(
            ["search", "best running shoes", "--sources", "rd,bs", "--since", "3m", "--refresh"],
            [POST], [REDDIT_POST])

    def test_both_sources_were_asked(self):
        self.assertEqual(self.rc, 0, self.text)
        urls = [c.args[0] for c in self.get.call_args_list]
        self.assertTrue(any(u.endswith("/api/posts/search") for u in urls), urls)
        self.assertTrue(any(u.endswith(SEARCH_POSTS_PATH) for u in urls), urls)

    def test_header_shows_a_count_for_reddit_and_for_bluesky(self):
        header = sources_header(self.text)
        self.assertRegex(header, r"reddit \(\d+ records, fetched")
        self.assertRegex(header, r"bluesky \(1 records, fetched")
        self.assertNotIn("(failed", header)

    def test_rows_from_both_sources_are_printed(self):
        self.assertIn(f"]({BSKY_LINK})", self.text)
        self.assertIn("](https://www.reddit.com/r/running/comments/abc123/", self.text)

    def test_parse_sources_keeps_the_order_rd_then_bs(self):
        self.assertEqual(ppf.parse_sources("rd,bs"), ["rd", "bs"])
        self.assertEqual(ppf.parse_sources("reddit,bluesky"), ["rd", "bs"])


class DoneWhen4_ClaudeMdSourcesTableHasBsRowWithRateLimitAndDate(unittest.TestCase):
    """The CLAUDE.md Sources table has a `bs` row with the rate limit and the date it was checked."""

    DATE = re.compile(r"\b20\d\d-\d\d-\d\d\b")

    def bs_row(self) -> str:
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        rows = [line for line in text.splitlines() if re.match(r"^\|\s*`bs`\s*\|", line)]
        self.assertEqual(len(rows), 1, f"expected exactly one `bs` table row in CLAUDE.md, found {len(rows)}")
        return rows[0]

    def test_sources_table_has_a_bs_row_naming_bluesky(self):
        row = self.bs_row()
        self.assertIn("bluesky", row.lower())
        self.assertGreaterEqual(row.count("|"), 5, "row does not have the table's four cells")

    def test_bs_row_states_the_rate_limit_and_the_date_checked(self):
        row = self.bs_row()
        self.assertIn("rate limit", row.lower())
        self.assertRegex(row, self.DATE)

    def test_rate_limit_paragraph_mentions_bluesky_with_a_date(self):
        text = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        para = next((p for p in text.split("\n\n") if p.startswith("Rate limits as checked")), "")
        self.assertTrue(para, "no 'Rate limits as checked' paragraph in CLAUDE.md")
        self.assertIn("bluesky", para.lower())
        bluesky_part = para[para.lower().index("bluesky"):]
        self.assertRegex(bluesky_part, self.DATE)


class DoneWhen5_SkillListsBsForGeneralTopics(unittest.TestCase):
    """The skill lists `bs` as an option for general topics."""

    SKILL = ROOT / "docs" / "skill" / "SKILL.md"
    INSTALLED = Path.home() / ".claude" / "skills" / "pain-point-finder" / "SKILL.md"
    GENERAL = re.compile(r"general topic|non-software|any topic|everyday topic|outside software", re.I)

    def test_skill_lists_bs_in_the_sources_flag_row(self):
        text = self.SKILL.read_text(encoding="utf-8")
        self.assertIn("`bs`", text)
        self.assertIn("bluesky", text.lower())
        flag_rows = [line for line in text.splitlines() if line.startswith("| `--sources")]
        self.assertEqual(len(flag_rows), 1, "expected one --sources row in the skill's flag table")
        self.assertIn("`bs`", flag_rows[0])

    def test_skill_recommends_bs_for_general_topics(self):
        text = self.SKILL.read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if "`bs`" in line and self.GENERAL.search(line)]
        self.assertTrue(lines, "no line in SKILL.md ties `bs` to general / non-software topics")

    @unittest.skipUnless(INSTALLED.exists(), "installed skill copy not present on this machine")
    def test_installed_skill_copy_is_identical_to_docs_skill(self):
        self.assertEqual(self.INSTALLED.read_text(encoding="utf-8"), self.SKILL.read_text(encoding="utf-8"),
                         f"{self.INSTALLED} differs from {self.SKILL}")


if __name__ == "__main__":
    unittest.main()
