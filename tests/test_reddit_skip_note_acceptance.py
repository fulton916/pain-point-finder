# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Acceptance tests for issue #17: the two #9 gaps (last_activity, skipped subreddit in meta.note).

Each test class is named after the "Done when" line it checks. No network: requests.Session.get is
mocked, time.sleep is patched out, the store is in memory and .env is never loaded.
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

import collect_reddit
import common
import merge_and_score
import ppf
from common import ROOT, load_config

NOW_TS = int(datetime.now(timezone.utc).timestamp())

POST = {
    "id": "abc123",
    "subreddit": "salesforce",
    "title": "Flow keeps failing on record-triggered updates",
    "selftext": "Every night the flow fails and nobody gets the fault email.",
    "url": "https://www.reddit.com/r/salesforce/comments/abc123/flow_keeps_failing/",
    "score": 42,
    "num_comments": 9,
    "created_utc": 1735689600,  # 2025-01-01T00:00:00Z
    "retrieved_on": 1735689630,
}

TIMEOUT_422 = {"data": None, "error": "Timeout. Maybe slow down a bit"}


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


def archive(refuse: set[str]):
    """A fake Arctic Shift: subreddits in `refuse` always answer 422 timeout; the others return one
    fresh post each. Decided by the request's subreddit parameter (case-insensitive, since ppf.py
    lowercases --subreddits), not by call order."""
    refuse = {s.lower() for s in refuse}

    def get(*args, **kwargs):
        sub = (kwargs.get("params") or {}).get("subreddit", "")
        if sub.lower() in refuse:
            return fake_response(422, TIMEOUT_422)
        post = dict(POST, subreddit=sub, id=f"id{sub.lower()}", created_utc=NOW_TS - 86400,
                    title=f"Flow keeps failing in {sub}")
        return fake_response(200, {"data": [post]})
    return get


def run_cli(argv: list[str], refuse: set[str]) -> tuple[int, str]:
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["store"] = ":memory:"
    cfg["reddit"].update(delay_seconds=0, timeout_pause_seconds=0)
    out = io.StringIO()
    with mock.patch.object(common, "load_config", return_value=cfg), \
         mock.patch.object(common, "load_env"), \
         mock.patch.object(requests.Session, "get", side_effect=archive(refuse)), \
         mock.patch.object(collect_reddit.time, "sleep"), \
         mock.patch.object(common.time, "sleep"), redirect_stdout(out):
        rc = ppf.main(argv)
    return rc, out.getvalue()


def sources_header(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("Sources:"):
            return line
    raise AssertionError(f"no 'Sources:' header line in:\n{md}")


def skip_sentence(note: str) -> str:
    """The part of a note that reports skipped subreddits, lowercased ('' when there is none)."""
    low = note.lower()
    i = low.find("skipped")
    return low[i:low.find("only", i) if "only" in low[i:] else len(low)] if i >= 0 else ""


class DoneWhen1_DecoratorsRemovedAndSuiteGreen(unittest.TestCase):
    """The two expectedFailure decorators in tests/test_reddit_acceptance.py are removed and the
    suite is green with the project venv."""

    def test_no_expected_failure_decorator_left_in_reddit_acceptance(self):
        text = (ROOT / "tests" / "test_reddit_acceptance.py").read_text(encoding="utf-8")
        self.assertNotIn("expectedFailure", text)

    def test_skip_note_test_in_reddit_acceptance_still_exists_and_is_not_weakened(self):
        text = (ROOT / "tests" / "test_reddit_acceptance.py").read_text(encoding="utf-8")
        self.assertIn("def test_skipped_subreddit_is_named_in_meta_note", text)
        body = text.split("def test_skipped_subreddit_is_named_in_meta_note", 1)[1].split("\n    def ", 1)[0]
        self.assertIn('result["meta"]["note"]', body)


class DoneWhen2_LastActivityFilledOrClauseDropped(unittest.TestCase):
    """For item 1, either to_record fills last_activity from created_utc or #9 gets a comment saying
    the clause was dropped and why. The GitHub comment cannot be checked offline; the code half can."""

    def test_last_activity_is_created_utc_or_left_empty_never_another_value(self):
        rec = collect_reddit.to_record(POST, "2026-11-25T00:00:00+00:00", 2000)
        self.assertIn(rec["last_activity"], ("", rec["created_at"]))
        self.assertEqual(rec["created_at"], "2025-01-01T00:00:00+00:00")

    def test_ranking_shows_the_post_date_when_last_activity_is_empty(self):
        rec = collect_reddit.to_record(POST, "2026-11-25T00:00:00+00:00", 2000)
        rows, _ = merge_and_score.rank([rec], load_config()["scoring"],
                                       as_of=datetime(2025, 6, 1, tzinfo=timezone.utc), mode="search")
        self.assertTrue(rows[0]["latest_activity"].startswith("2025-01-01"), rows[0]["latest_activity"])


class DoneWhen3_SkippedSubredditNamedInJsonAndMarkdown(unittest.TestCase):
    """For item 2, a search where one subreddit is skipped shows the subreddit's name in meta.note
    (JSON) and in the markdown header."""

    SUBS = ["salesforce", "SalesforceDeveloper", "salesforceadmin"]

    def test_json_meta_note_names_the_skipped_subreddit(self):
        rc, out = run_cli(["search", "flow", "--sources", "rd", "--refresh", "--format", "json",
                           "--subreddits", ",".join(self.SUBS)], refuse={"SalesforceDeveloper"})
        self.assertEqual(rc, 0, out)
        meta = json.loads(out)["meta"]
        skipped = skip_sentence(meta["note"])
        self.assertIn("salesforcedeveloper", skipped, meta["note"])
        self.assertNotRegex(skipped, r"r/salesforce(?![a-z])")
        self.assertNotIn("salesforceadmin", skipped)
        self.assertNotIn("error", meta["per_source"]["reddit"])
        self.assertEqual(meta["per_source"]["reddit"]["records"], 2)

    def test_markdown_header_names_the_skipped_subreddit(self):
        rc, out = run_cli(["search", "flow", "--sources", "rd", "--refresh",
                           "--subreddits", ",".join(self.SUBS)], refuse={"SalesforceDeveloper"})
        self.assertEqual(rc, 0, out)
        header = sources_header(out)
        self.assertIn("salesforcedeveloper", header.lower())
        self.assertIn("reddit (2 records, fetched", header)

    def test_default_config_subreddits_skip_is_named_too(self):
        rc, out = run_cli(["search", "flow", "--sources", "rd", "--refresh", "--format", "json"],
                          refuse={"salesforceadmin"})
        self.assertEqual(rc, 0, out)
        self.assertIn("r/salesforceadmin", json.loads(out)["meta"]["note"])

    def test_two_skipped_subreddits_are_both_named(self):
        rc, out = run_cli(["search", "flow", "--sources", "rd", "--refresh", "--format", "json",
                           "--subreddits", ",".join(self.SUBS)], refuse={"salesforce", "salesforceadmin"})
        self.assertEqual(rc, 0, out)
        skipped = skip_sentence(json.loads(out)["meta"]["note"])
        self.assertRegex(skipped, r"r/salesforce(?![a-z])")
        self.assertIn("r/salesforceadmin", skipped)
        self.assertNotIn("salesforcedeveloper", skipped)

    def test_no_skip_means_no_skip_note_and_plain_header(self):
        rc, out = run_cli(["search", "flow", "--sources", "rd", "--refresh",
                           "--subreddits", ",".join(self.SUBS)], refuse=set())
        self.assertEqual(rc, 0, out)
        self.assertIn("reddit (3 records, fetched)", sources_header(out))
        self.assertNotIn("skipped", out.lower())
        rc, out = run_cli(["search", "flow", "--sources", "rd", "--refresh", "--format", "json",
                           "--subreddits", ",".join(self.SUBS)], refuse=set())
        meta = json.loads(out)["meta"]
        self.assertNotIn("note", meta["per_source"]["reddit"])
        self.assertTrue(meta["note"].startswith("Rows are ordered"), meta["note"])

    def test_every_subreddit_skipped_is_a_source_failure_naming_them(self):
        rc, out = run_cli(["search", "flow", "--sources", "rd", "--refresh", "--format", "json",
                           "--subreddits", "salesforce,salesforceadmin"], refuse={"salesforce", "salesforceadmin"})
        self.assertEqual(rc, 2, out)  # every source failed; the JSON is still printed
        meta = json.loads(out)["meta"]
        self.assertIn("salesforceadmin", meta["per_source"]["reddit"]["error"])

    def test_collect_reddit_search_without_notes_option_still_works(self):
        session = mock.Mock()
        session.get.side_effect = archive({"salesforce"})
        cfg = copy.deepcopy(load_config())
        cfg["reddit"].update(delay_seconds=0, timeout_pause_seconds=0)
        with mock.patch.object(collect_reddit.time, "sleep"), mock.patch.object(common.time, "sleep"):
            records = collect_reddit.search(session, cfg, "flow", datetime(2024, 1, 1, tzinfo=timezone.utc),
                                            subreddits=["salesforce", "salesforceadmin"])
        self.assertEqual(len(records), 1)
        self.assertTrue(re.search(r"/r/salesforceadmin/", records[0]["url"]))


if __name__ == "__main__":
    unittest.main()
