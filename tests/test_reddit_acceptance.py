# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Acceptance tests for issue #9: Reddit collector via the Arctic Shift archive API.

Each test is named after the "Done when" line it checks. No network: requests.Session.get and
common.get_json are mocked, and time.sleep is patched out.
"""

import copy
import inspect
import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import requests

import collect_hn
import collect_reddit
import common
import merge_and_score
import ppf
from common import FIELDS, ROOT, load_config, validate_record

SINCE = datetime(2024, 11, 25, tzinfo=timezone.utc)

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

CFG = {
    "reddit": {
        "endpoint": "https://arctic-shift.photon-reddit.com/api/posts/search",
        "subreddits": ["salesforce", "SalesforceDeveloper", "salesforceadmin"],
        "fields": ["id", "subreddit", "title", "selftext", "url", "score", "num_comments", "created_utc", "retrieved_on"],
        "search_limit": 100, "search_min_score": 0, "delay_seconds": 7,
        "timeout_retries": 2, "timeout_pause_seconds": 0, "body_max_chars": 2000,
    }
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


TIMEOUT_422 = {"data": None, "error": "Timeout. Maybe slow down a bit"}


def ok(posts):
    return fake_response(200, {"data": posts})


class DoneWhen1_SearchInterfaceAndRecordFormat(unittest.TestCase):
    """collect_reddit.py exposes SOURCE and search(...) matching collect_hn.search; one call per
    subreddit with query, after=since, limit, fields; --subreddits overrides; 11-field records."""

    def test_source_is_reddit_and_search_signature_matches_collect_hn(self):
        self.assertEqual(collect_reddit.SOURCE, "reddit")
        hn = [p.name for p in inspect.signature(collect_hn.search).parameters.values()]
        rd = [p.name for p in inspect.signature(collect_reddit.search).parameters.values()]
        self.assertEqual(rd, hn)
        self.assertEqual(rd, ["session", "cfg", "query", "since", "options"])
        kinds = [p.kind for p in inspect.signature(collect_reddit.search).parameters.values()]
        self.assertEqual(kinds[-1], inspect.Parameter.VAR_KEYWORD)

    def test_registered_in_ppf_sources_and_units(self):
        self.assertIs(ppf.SOURCES[ppf.parse_sources("reddit")[0]], collect_reddit)
        self.assertIn("reddit", ppf.UNITS)

    def test_one_posts_search_call_per_configured_subreddit_with_query_after_limit_fields(self):
        session = mock.Mock()
        session.get.side_effect = [ok([POST]), ok([]), ok([])]
        with mock.patch.object(collect_reddit.time, "sleep"), mock.patch.object(common.time, "sleep"):
            collect_reddit.search(session, CFG, "flow failing", SINCE)
        self.assertEqual(session.get.call_count, 3)
        subs = []
        for call in session.get.call_args_list:
            url = call.args[0] if call.args else call.kwargs["url"]
            self.assertTrue(url.endswith("/api/posts/search"), url)
            params = call.kwargs["params"]
            subs.append(params["subreddit"])
            self.assertEqual(params["query"], "flow failing")
            self.assertIn(params["after"], (int(SINCE.timestamp()), SINCE.isoformat(), SINCE.date().isoformat()))
            self.assertTrue(1 <= int(params["limit"]) <= 100)
            self.assertTrue(params["fields"])
        self.assertEqual(subs, ["salesforce", "SalesforceDeveloper", "salesforceadmin"])

    def test_subreddits_command_line_overrides_the_config_list(self):
        captured = {}

        def fake_run_search(cfg, query, sources, since_text, refresh, limit, repos, title_only, **kw):
            captured.update(kw)
            return {"meta": {"per_source": {}}, "rows": []}

        with mock.patch.object(ppf, "run_search", side_effect=fake_run_search), \
             mock.patch.object(ppf, "format_md", return_value="x"), redirect_stdout(io.StringIO()):
            ppf.main(["search", "flow", "--sources", "reddit", "--subreddits", "sysadmin,devops"])
        self.assertEqual(sorted(captured["subreddits"]), ["devops", "sysadmin"])

        session = mock.Mock()
        session.get.side_effect = [ok([]), ok([])]
        with mock.patch.object(collect_reddit.time, "sleep"):
            collect_reddit.search(session, CFG, "flow", SINCE, subreddits=["sysadmin", "devops"])
        self.assertEqual([c.kwargs["params"]["subreddit"] for c in session.get.call_args_list], ["sysadmin", "devops"])

    def test_records_use_the_11_field_format_and_mapping(self):
        rec = collect_reddit.to_record(POST, "2026-11-25T00:00:00+00:00", 2000)
        self.assertEqual(list(rec), FIELDS)
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["source"], "reddit")
        self.assertEqual(rec["score"], 42)
        self.assertEqual(rec["comments"], 9)
        self.assertEqual(rec["url"], "https://www.reddit.com/r/salesforce/comments/abc123/")
        self.assertEqual(rec["status"], "")
        self.assertEqual(rec["views"], "")
        self.assertEqual(rec["created_at"], "2025-01-01T00:00:00+00:00")

    def test_last_activity_stays_empty_and_ranking_falls_back_to_the_post_date(self):
        """Issue #9 said 'last_activity = created_utc'; #17 dropped that clause (CLAUDE.md: never guess a
        value; the archive has no activity date, same as Hacker News). The ranking uses created_at instead."""
        rec = collect_reddit.to_record(POST, "2026-11-25T00:00:00+00:00", 2000)
        self.assertEqual(rec["last_activity"], "")
        rows, _ = merge_and_score.rank([rec], load_config()["scoring"], as_of=datetime(2025, 1, 2, tzinfo=timezone.utc),
                                       mode="search")
        self.assertTrue(rows[0]["latest_activity"].startswith("2025-01-01"), rows[0]["latest_activity"])


class DoneWhen2_GetJsonDelayRetryAndSkip(unittest.TestCase):
    """Requests go through common.get_json with the config delay; retry on 422 timeout and 429/5xx;
    a subreddit that keeps timing out is skipped with a note in meta.note, not a crash."""

    def test_requests_go_through_common_get_json(self):
        with mock.patch.object(collect_reddit, "get_json", wraps=common.get_json) as gj, \
             mock.patch.object(collect_reddit.time, "sleep"):
            session = mock.Mock()
            session.get.return_value = ok([POST])
            collect_reddit.search(session, CFG, "flow", SINCE, subreddits=["salesforce"])
        self.assertEqual(gj.call_count, 1)
        self.assertEqual(session.get.call_count, 1)  # the HTTP call came from inside common.get_json

    def test_config_delay_is_waited_between_subreddits(self):
        session = mock.Mock()
        session.get.side_effect = [ok([]), ok([]), ok([])]
        slept = []
        with mock.patch.object(collect_reddit.time, "sleep", side_effect=slept.append):
            collect_reddit.search(session, CFG, "flow", SINCE)
        self.assertEqual(slept.count(7), 2, f"expected two 7s waits, got {slept}")

    def test_422_timeout_is_retried(self):
        session = mock.Mock()
        session.get.side_effect = [fake_response(422, TIMEOUT_422), ok([POST])]
        with mock.patch.object(collect_reddit.time, "sleep"), mock.patch.object(common.time, "sleep"):
            records = collect_reddit.search(session, CFG, "flow", SINCE, subreddits=["salesforce"])
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(len(records), 1)

    def test_429_and_5xx_are_retried_with_backoff(self):
        for first in (429, 500, 503):
            with self.subTest(status=first):
                session = mock.Mock()
                session.get.side_effect = [fake_response(first, {"error": "busy"}), ok([POST])]
                slept = []
                with mock.patch.object(collect_reddit.time, "sleep"), \
                     mock.patch.object(common.time, "sleep", side_effect=slept.append):
                    records = collect_reddit.search(session, CFG, "flow", SINCE, subreddits=["salesforce"])
                self.assertEqual(session.get.call_count, 2)
                self.assertEqual(len(records), 1)
                self.assertTrue(slept and slept[0] > 0, f"expected a backoff wait, got {slept}")

    def test_subreddit_that_keeps_timing_out_is_skipped_not_a_crash(self):
        session = mock.Mock()
        session.get.side_effect = [fake_response(422, TIMEOUT_422)] * 3 + [ok([dict(POST, subreddit="salesforceadmin")])]
        with mock.patch.object(collect_reddit.time, "sleep"):
            records = collect_reddit.search(session, CFG, "flow", SINCE, subreddits=["salesforce", "salesforceadmin"])
        self.assertEqual([r["url"] for r in records], ["https://www.reddit.com/r/salesforceadmin/comments/abc123/"])

    def test_skipped_subreddit_is_named_in_meta_note(self):
        """Done when: 'skipped with a note in meta.note' (and, per #17, in the markdown header)."""
        cfg = load_config()
        cfg = copy.deepcopy(cfg)
        cfg["reddit"].update(delay_seconds=0, timeout_retries=2, timeout_pause_seconds=0)
        session = mock.Mock()
        session.get.side_effect = [fake_response(422, TIMEOUT_422)] * 3 + [ok([dict(POST, subreddit="salesforceadmin")])]
        store = ppf.Store(":memory:")
        with mock.patch.object(collect_reddit.time, "sleep"), mock.patch.object(common, "load_env"):
            result = ppf.run_search(cfg, "flow", ["rd"], "24m", True, 30, None, None, store=store,
                                    session=session, subreddits=["salesforce", "salesforceadmin"])
        self.assertNotIn("error", result["meta"]["per_source"]["reddit"])
        self.assertEqual(result["meta"]["per_source"]["reddit"]["records"], 1)
        self.assertIn("salesforce", result["meta"]["note"].replace("salesforceadmin", ""),
                      f"meta.note does not name the skipped subreddit: {result['meta']['note']!r}")
        self.assertIn("r/salesforce ", result["meta"]["per_source"]["reddit"]["note"] + " ")
        header = ppf.format_md(result).split("\n")[2]
        self.assertIn("reddit (1 records, fetched; reddit skipped r/salesforce ", header)
        # A partial answer must not be served from the cache later with the note gone.
        scope = ppf.search_scope(cfg, "24m", None, None, None, ["salesforce", "salesforceadmin"])
        self.assertIsNone(store.cached("flow", "reddit", scope, 24, datetime.now(timezone.utc)))


class DoneWhen3_ConfigDefaults(unittest.TestCase):
    """Default reddit.subreddits is the Salesforce list; thresholds set; Reddit capped at Moderate."""

    def setUp(self):
        self.cfg = load_config()

    def test_default_subreddits_are_the_salesforce_list(self):
        self.assertEqual(self.cfg["reddit"]["subreddits"], ["salesforce", "SalesforceDeveloper", "salesforceadmin"])

    def test_reddit_block_has_delay_fields_and_limit(self):
        rd = self.cfg["reddit"]
        self.assertIn("delay_seconds", rd)
        self.assertTrue(rd["fields"])
        self.assertTrue(any(k == "limit" or k.endswith("_limit") for k in rd), list(rd))

    def test_source_thresholds_reddit_has_moderate_and_strong(self):
        t = self.cfg["scoring"]["source_thresholds"]["reddit"]
        self.assertIsInstance(t["moderate"], (int, float))
        self.assertIsInstance(t["strong"], (int, float))
        self.assertLess(t["moderate"], t["strong"])

    def test_label_caps_reddit_at_moderate(self):
        self.assertEqual(self.cfg["scoring"]["label_caps"]["reddit"], "Moderate")
        rec = collect_reddit.to_record(dict(POST, score=100000, created_utc=int(datetime.now(timezone.utc).timestamp())),
                                       "2026-11-25T00:00:00+00:00", 2000)
        rows, _ = merge_and_score.rank([rec], self.cfg["scoring"], as_of=datetime.now(timezone.utc), mode="search")
        self.assertEqual(rows[0]["evidence_label"], "Moderate")


class DoneWhen4_Tests(unittest.TestCase):
    """Fixture test for to_record and the permalink; the query builder always includes fields."""

    def test_permalink_built_from_subreddit_and_id_not_the_url_field(self):
        post = dict(POST, subreddit="SalesforceDeveloper", id="xyz9", url="https://example.com/article")
        rec = collect_reddit.to_record(post, "2026-11-25T00:00:00+00:00", 2000)
        self.assertEqual(rec["url"], "https://www.reddit.com/r/SalesforceDeveloper/comments/xyz9/")

    def test_query_builder_always_includes_fields(self):
        cases = [
            (CFG, "flow", SINCE, "salesforce", 100),
            (CFG, "", None, "salesforce", 1),
            ({"reddit": dict(CFG["reddit"], fields=[])}, "flow", None, "salesforce", 10),
            ({"reddit": {k: v for k, v in CFG["reddit"].items() if k != "fields"}}, "flow", SINCE, "x", 10),
        ]
        for cfg, q, since, sub, limit in cases:
            with self.subTest(q=q, fields=cfg["reddit"].get("fields")):
                p = collect_reddit.build_params(cfg, q, since, sub, limit)
                self.assertTrue(p.get("fields"), p)
                for needed in ("id", "subreddit", "score", "num_comments", "created_utc"):
                    self.assertIn(needed, p["fields"].split(","))


class DoneWhen5_DocsAndEndToEnd(unittest.TestCase):
    """README and the skill name the source, say archive / stale scores / no key, give the GitHub
    repo; ppf.py search "x" --sources reddit works end to end (mocked HTTP here)."""

    def check_doc(self, path):
        text = path.read_text(encoding="utf-8")
        low = text.lower()
        self.assertIn("arctic shift", low, f"{path.name}: source not named")
        self.assertIn("reddit", low)
        self.assertIn("archive", low, f"{path.name}: does not say it is an archive")
        self.assertTrue("stale" in low or "as archived" in low or "posting time" in low,
                        f"{path.name}: does not say the scores are stale")
        self.assertRegex(low, r"reddit[^\n]*no key|no key[^\n]*(reddit|arctic)|reddit need no key|need no key",
                         f"{path.name}: does not say Reddit needs no key")
        self.assertIn("github.com/arthurheitmann/arctic_shift", low, f"{path.name}: no GitHub repo attribution")

    def test_readme_names_source_archive_stale_scores_no_key_and_repo(self):
        self.check_doc(ROOT / "README.md")

    def test_skill_names_source_archive_stale_scores_no_key_and_repo(self):
        self.check_doc(ROOT / "docs" / "skill" / "SKILL.md")

    def test_ppf_search_sources_reddit_works_end_to_end_with_mocked_http(self):
        cfg = copy.deepcopy(load_config())
        cfg["paths"]["store"] = ":memory:"
        cfg["reddit"]["delay_seconds"] = 0
        fresh = dict(POST, created_utc=int(datetime.now(timezone.utc).timestamp()) - 86400)
        answers = [ok([fresh]), ok([]), ok([])]
        out = io.StringIO()
        with mock.patch.object(common, "load_config", return_value=cfg), \
             mock.patch.object(common, "load_env"), \
             mock.patch.object(requests.Session, "get", side_effect=answers) as get, \
             mock.patch.object(collect_reddit.time, "sleep"), redirect_stdout(out):
            rc = ppf.main(["search", "x", "--sources", "reddit", "--refresh"])
        text = out.getvalue()
        self.assertEqual(rc, 0, text)
        self.assertEqual(get.call_count, 3)
        self.assertIn("reddit (1 records, fetched)", text)
        self.assertIn("https://www.reddit.com/r/salesforce/comments/abc123/", text)
        self.assertIn("42 " + ppf.UNITS["reddit"], text)
        self.assertIn("arctic_shift", text)  # attribution line printed with the result


if __name__ == "__main__":
    unittest.main()
