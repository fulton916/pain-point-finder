# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Fixture tests for the collectors: no network, just the record-building and query-building code."""

import unittest
from datetime import datetime, timezone
from unittest import mock

import requests

import collect_gh
import collect_hn
import collect_reddit
import collect_se
import common
from common import FIELDS, validate_record

HN_HIT = {
    "objectID": "12345",
    "title": "Ask HN: Why is Salesforce Flow so slow?",
    "url": "https://example.com/post",
    "story_text": "<p>First paragraph &amp; more.</p><p>Second<br>line</p>",
    "points": 123,
    "num_comments": 45,
    "created_at": "2026-01-02T03:04:05Z",
}

GH_ITEM = {
    "html_url": "https://github.com/acme/widgets/issues/7",
    "title": "Export to CSV drops the header row",
    "body": "x" * 5000,
    "reactions": {"total_count": 19},
    "comments": 6,
    "created_at": "2025-05-06T07:08:09Z",
    "updated_at": "2026-02-03T04:05:06Z",
    "state": "open",
}


class HackerNewsTests(unittest.TestCase):
    def test_to_record_has_all_fields_and_validates(self):
        rec = collect_hn.to_record(HN_HIT, "2026-11-24T00:00:00+00:00")
        self.assertEqual(list(rec), FIELDS)
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["source"], "hackernews")
        self.assertEqual(rec["url"], "https://news.ycombinator.com/item?id=12345")
        self.assertEqual(rec["score"], 123)
        self.assertEqual(rec["comments"], 45)
        self.assertEqual(rec["views"], "")
        self.assertEqual(rec["last_activity"], "")
        self.assertEqual(rec["status"], "")

    def test_body_keeps_external_link_and_cleans_html(self):
        rec = collect_hn.to_record(HN_HIT, "2026-11-24T00:00:00+00:00")
        self.assertTrue(rec["body"].startswith("https://example.com/post"))
        self.assertIn("First paragraph & more.", rec["body"])
        self.assertIn("Second\nline", rec["body"])
        self.assertNotIn("<p>", rec["body"])

    def test_title_only_adds_the_algolia_restriction(self):
        cfg = {"hacker_news": {"tags": "story", "more_than_points": 50, "hits_per_page": 100}}
        with_title = collect_hn.build_params(cfg, "flow", 1700000000, 0, title_only=True)
        without = collect_hn.build_params(cfg, "flow", 1700000000, 0, title_only=False)
        self.assertEqual(with_title["restrictSearchableAttributes"], "title")
        self.assertNotIn("restrictSearchableAttributes", without)
        self.assertEqual(with_title["numericFilters"], "points>50,created_at_i>=1700000000")
        lower = collect_hn.build_params(cfg, "flow", 1700000000, 0, title_only=True, more_than_points=10)
        self.assertEqual(lower["numericFilters"], "points>10,created_at_i>=1700000000")

    def test_hits_to_records_dedupes_by_story_id_and_sorts(self):
        low = dict(HN_HIT, objectID="1", points=5)
        high = dict(HN_HIT, objectID="2", points=500)
        records = collect_hn.hits_to_records([low, high, dict(low)], "2026-11-24T00:00:00+00:00")
        self.assertEqual([r["score"] for r in records], [500, 5])

    def test_hits_without_object_id_are_skipped_not_merged(self):
        a = dict(HN_HIT, objectID=None, title="A")
        b = dict(HN_HIT, objectID="", title="B")
        records = collect_hn.hits_to_records([a, b, HN_HIT], "2026-11-24T00:00:00+00:00")
        self.assertEqual([r["url"] for r in records], ["https://news.ycombinator.com/item?id=12345"])


class GitHubTests(unittest.TestCase):
    def test_to_record_has_all_fields_and_truncates_body(self):
        rec = collect_gh.to_record(GH_ITEM, "2026-11-24T00:00:00+00:00", 2000)
        self.assertEqual(list(rec), FIELDS)
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["score"], 19)
        self.assertEqual(rec["comments"], 6)
        self.assertEqual(rec["status"], "open")
        self.assertEqual(rec["last_activity"], "2026-02-03T04:05:06Z")
        self.assertEqual(len(rec["body"]), 2000)

    def test_pull_requests_are_skipped(self):
        item = dict(GH_ITEM, pull_request={"url": "x"})
        self.assertEqual(collect_gh.to_record(item, "2026-11-24T00:00:00+00:00", 2000), {})

    def test_search_query_words_qualifiers_and_repos(self):
        since = datetime(2024, 11, 24, tzinfo=timezone.utc)
        q = collect_gh.build_search_query('flow "fault path"', 1, since, ["acme/widgets", "acme/gadgets"])
        self.assertEqual(
            q, "flow fault path in:title is:issue reactions:>=1 updated:>=2024-11-24 repo:acme/widgets repo:acme/gadgets"
        )

    def test_search_query_without_repos_or_date(self):
        self.assertEqual(
            collect_gh.build_search_query("export csv", 3, None, [], "title,body"),
            "export csv in:title,body is:issue reactions:>=3",
        )

    def test_search_query_over_256_characters_raises(self):
        with self.assertRaises(ValueError):
            collect_gh.build_search_query("word " * 60, 1, None, [])

    def test_items_to_records_dedupes_by_url(self):
        records = collect_gh.items_to_records([GH_ITEM, dict(GH_ITEM)], "2026-11-24T00:00:00+00:00", 2000)
        self.assertEqual(len(records), 1)


SE_ITEM = {
    "question_id": 424242,
    "link": "https://salesforce.stackexchange.com/questions/424242/flow-fault-email-goes-to-the-wrong-user",
    "title": "Flow fault email goes to the &quot;wrong&quot; user",
    "body": "<p>When a <b>Flow</b> fails &amp; the fault email arrives,</p><p>it goes to the last modifier.</p>",
    "score": 7,
    "answer_count": 3,
    "view_count": 1234,
    "creation_date": 1735689600,       # 2025-01-01T00:00:00Z
    "last_activity_date": 1767225600,  # 2026-01-01T00:00:00Z
    "is_answered": True,
    "tags": ["flow", "email"],
}

SE_CFG = {
    "stackexchange": {
        "endpoint": "https://api.stackexchange.com/2.3/search/advanced",
        "site": "salesforce", "pagesize": 100, "delay_seconds": 0, "body_max_chars": 2000,
        "search_min_score": 0, "search_max_pages": 3, "search_in": "title",
    }
}


class StackExchangeTests(unittest.TestCase):
    def setUp(self):
        collect_se.BACKOFF_UNTIL.clear()

    def test_to_record_maps_the_api_fields(self):
        rec = collect_se.to_record(SE_ITEM, "2026-11-25T00:00:00+00:00", 2000)
        self.assertEqual(list(rec), FIELDS)
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["source"], "stackexchange")
        self.assertEqual(rec["url"], SE_ITEM["link"])
        self.assertEqual(rec["title"], 'Flow fault email goes to the "wrong" user')
        self.assertEqual(rec["body"], "When a Flow fails & the fault email arrives,\n\nit goes to the last modifier.")
        self.assertEqual(rec["score"], 7)
        self.assertEqual(rec["comments"], 3)       # answer_count
        self.assertEqual(rec["views"], 1234)       # view_count
        self.assertEqual(rec["created_at"], "2025-01-01T00:00:00+00:00")
        self.assertEqual(rec["last_activity"], "2026-01-01T00:00:00+00:00")
        self.assertEqual(rec["status"], "answered")

    def test_unanswered_question_has_empty_status_and_missing_dates_stay_empty(self):
        item = dict(SE_ITEM, is_answered=False)
        del item["last_activity_date"]
        rec = collect_se.to_record(item, "2026-11-25T00:00:00+00:00", 2000)
        self.assertEqual(rec["status"], "")
        self.assertEqual(rec["last_activity"], "")

    def test_body_is_truncated(self):
        item = dict(SE_ITEM, body="<p>" + "y" * 5000 + "</p>")
        self.assertEqual(len(collect_se.to_record(item, "2026-11-25T00:00:00+00:00", 2000)["body"]), 2000)

    def test_build_params_title_sort_by_votes_and_since(self):
        since = datetime(2024, 11, 25, tzinfo=timezone.utc)
        p = collect_se.build_params(SE_CFG, "flow  fault email", since, 2, "salesforce", 3)
        self.assertEqual(p["title"], "flow fault email")
        self.assertNotIn("q", p)
        self.assertEqual((p["site"], p["sort"], p["order"], p["min"], p["page"]), ("salesforce", "votes", "desc", 3, 2))
        self.assertEqual(p["fromdate"], int(since.timestamp()))
        self.assertEqual(p["filter"], "withbody")
        free = collect_se.build_params(SE_CFG, "flow", None, 1, "stackoverflow", 0, search_in="q", tagged="apex")
        self.assertEqual((free["q"], free["tagged"], free["site"]), ("flow", "apex", "stackoverflow"))
        self.assertNotIn("title", free)
        self.assertNotIn("fromdate", free)

    def test_items_to_records_dedupes_by_url_and_sorts(self):
        low = dict(SE_ITEM, link="https://salesforce.stackexchange.com/questions/1/a", score=1)
        records = collect_se.items_to_records([low, SE_ITEM, dict(SE_ITEM)], "2026-11-25T00:00:00+00:00", 2000)
        self.assertEqual([r["score"] for r in records], [7, 1])

    def test_backoff_is_honored_before_the_next_request(self):
        pages = [
            ({"items": [SE_ITEM], "has_more": True, "backoff": 7, "quota_remaining": 500, "quota_max": 10000}, None),
            ({"items": [dict(SE_ITEM, link="https://salesforce.stackexchange.com/questions/2/b")], "has_more": False}, None),
        ]
        slept = []
        with mock.patch.object(collect_se, "get_json", side_effect=pages), \
             mock.patch.object(collect_se.time, "sleep", side_effect=slept.append), \
             mock.patch.object(collect_se, "key_param", return_value={}):
            items = collect_se.fetch_items(object(), SE_CFG, "flow", None, "salesforce", 0, 3)
        self.assertEqual(len(items), 2)
        self.assertTrue(any(6 <= s <= 7 for s in slept), f"expected a ~7s backoff wait, slept {slept}")

    def test_quota_used_up_stops_paging(self):
        pages = [({"items": [SE_ITEM], "has_more": True, "quota_remaining": 0, "quota_max": 10000}, None)]
        with mock.patch.object(collect_se, "get_json", side_effect=pages) as gj, \
             mock.patch.object(collect_se.time, "sleep"), \
             mock.patch.object(collect_se, "key_param", return_value={}):
            collect_se.fetch_items(object(), SE_CFG, "flow", None, "salesforce", 0, 3)
        self.assertEqual(gj.call_count, 1)

    def test_api_error_uses_the_body_message_not_the_url(self):
        resp = mock.Mock()
        resp.json.return_value = {"error_id": 502, "error_name": "throttle_violation", "error_message": "too many requests"}
        err = collect_se.api_error(requests.HTTPError("HTTP 400 for url: https://x/?key=SECRET", response=resp))
        self.assertIn("throttle_violation 502: too many requests", str(err))
        self.assertNotIn("SECRET", str(err))

    def test_key_is_sent_as_a_parameter_and_registered_as_a_secret(self):
        with mock.patch.dict("os.environ", {"STACKEXCHANGE_KEY": "abc123key"}):
            self.assertEqual(collect_se.key_param(), {"key": "abc123key"})
        self.assertIn("abc123key", common.SECRETS)
        self.assertEqual(common.redact("url?key=abc123key&site=x"), "url?key=***&site=x")
        common.SECRETS.discard("abc123key")

    def test_search_uses_the_config_site_unless_an_option_overrides_it(self):
        since = datetime(2024, 11, 25, tzinfo=timezone.utc)
        with mock.patch.object(collect_se, "fetch_items", return_value=[SE_ITEM]) as fi:
            collect_se.search(object(), SE_CFG, "flow", since)
            self.assertEqual(fi.call_args.args[4], "salesforce")
            collect_se.search(object(), SE_CFG, "flow", since, site="stackoverflow")
            self.assertEqual(fi.call_args.args[4], "stackoverflow")


RD_POST = {
    "id": "1y531el",
    "subreddit": "salesforce",
    "title": "Flow fault email goes to the &quot;wrong&quot; user",
    "selftext": "When a Flow fails &amp; the fault email arrives, it goes to the last modifier.",
    "url": "https://www.reddit.com/r/salesforce/comments/1y531el/flow_fault_email_goes_to_the_wrong_user/",
    "score": 12,
    "num_comments": 4,
    "created_utc": 1735689600,   # 2025-01-01T00:00:00Z
    "retrieved_on": 1735689620,
}

RD_CFG = {
    "reddit": {
        "endpoint": "https://arctic-shift.photon-reddit.com/api/posts/search",
        "subreddits": ["salesforce", "salesforceadmin"],
        "fields": ["id", "subreddit", "title", "selftext", "url", "score", "num_comments", "created_utc", "retrieved_on"],
        "search_limit": 100, "search_min_score": 0, "delay_seconds": 0,
        "timeout_retries": 2, "timeout_pause_seconds": 0, "body_max_chars": 2000,
    }
}


def timeout_422() -> requests.HTTPError:
    resp = mock.Mock()
    resp.status_code = 422
    resp.json.return_value = {"data": None, "error": "Timeout. Maybe slow down a bit"}
    return requests.HTTPError("HTTP 422 Unprocessable Entity from https://x", response=resp)


class RedditTests(unittest.TestCase):
    def test_to_record_builds_the_permalink_and_maps_the_fields(self):
        rec = collect_reddit.to_record(RD_POST, "2026-11-25T00:00:00+00:00", 2000)
        self.assertEqual(list(rec), FIELDS)
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["source"], "reddit")
        self.assertEqual(rec["url"], "https://www.reddit.com/r/salesforce/comments/1y531el/")
        self.assertEqual(rec["title"], 'Flow fault email goes to the "wrong" user')
        self.assertEqual(rec["body"], "When a Flow fails & the fault email arrives, it goes to the last modifier.")
        self.assertEqual(rec["score"], 12)
        self.assertEqual(rec["comments"], 4)
        self.assertEqual(rec["views"], "")
        self.assertEqual(rec["created_at"], "2025-01-01T00:00:00+00:00")
        self.assertEqual((rec["last_activity"], rec["status"]), ("", ""))

    def test_link_post_keeps_the_external_url_in_the_body_and_removed_text_is_empty(self):
        post = dict(RD_POST, id="t3_abc", url="https://example.com/article", selftext="[removed]")
        rec = collect_reddit.to_record(post, "2026-11-25T00:00:00+00:00", 2000)
        self.assertEqual(rec["url"], "https://www.reddit.com/r/salesforce/comments/abc/")
        self.assertEqual(rec["body"], "Link: https://example.com/article")
        other = dict(RD_POST, url="https://www.reddit.com/r/SalesforceDeveloper/comments/xyz/some_thread/", selftext="")
        self.assertEqual(collect_reddit.to_record(other, "x", 2000)["body"],
                         "Link: https://www.reddit.com/r/SalesforceDeveloper/comments/xyz/some_thread/")
        self.assertIsNone(collect_reddit.to_record(dict(RD_POST, score=3), "2026-11-25T00:00:00+00:00", 2000, min_score=5))
        self.assertEqual(len(collect_reddit.to_record(dict(RD_POST, selftext="y" * 5000), "x", 2000)["body"]), 2000)

    def test_build_params_always_sends_fields_subreddit_words_and_after(self):
        since = datetime(2024, 11, 25, tzinfo=timezone.utc)
        p = collect_reddit.build_params(RD_CFG, "flow  fault email", since, "r/salesforce", 250)
        self.assertEqual(p["subreddit"], "salesforce")
        self.assertEqual(p["query"], "flow fault email")
        self.assertEqual(p["after"], int(since.timestamp()))
        self.assertEqual(p["limit"], 100)
        self.assertEqual(p["sort"], "desc")
        self.assertEqual(p["fields"], "id,subreddit,title,selftext,url,score,num_comments,created_utc,retrieved_on")
        bare = collect_reddit.build_params(RD_CFG, "", None, "salesforce", 10)
        self.assertNotIn("query", bare)
        self.assertNotIn("after", bare)
        self.assertIn("fields", bare)
        trimmed = collect_reddit.build_params({"reddit": dict(RD_CFG["reddit"], fields=["title", "score"])}, "x", None, "s", 10)
        for needed in ("id", "subreddit", "created_utc", "num_comments"):
            self.assertIn(needed, trimmed["fields"].split(","))

    def test_posts_to_records_dedupes_by_url_and_sorts(self):
        low = dict(RD_POST, id="zzz", score=1)
        down = dict(RD_POST, id="down", score=-4)
        none = dict(RD_POST, id="none", score=None)
        records = collect_reddit.posts_to_records([low, down, none, RD_POST, dict(RD_POST)], "2026-11-25T00:00:00+00:00", 2000)
        self.assertEqual([r["score"] for r in records], [12, 1, -4, ""])

    def test_timeout_is_retried_then_the_subreddit_is_skipped(self):
        answers = [timeout_422(), timeout_422(), timeout_422(),       # salesforce: 1 try + 2 retries, skipped
                   ({"data": [dict(RD_POST, subreddit="salesforceadmin")]}, None)]  # salesforceadmin answers
        with mock.patch.object(collect_reddit, "get_json", side_effect=answers) as gj, \
             mock.patch.object(collect_reddit.time, "sleep"):
            posts = collect_reddit.fetch_posts(object(), RD_CFG, "flow", None, ["salesforce", "salesforceadmin"], 100)
        self.assertEqual(gj.call_count, 4)
        self.assertEqual([p["subreddit"] for p in posts], ["salesforceadmin"])

    def test_every_subreddit_timing_out_fails_the_source(self):
        with mock.patch.object(collect_reddit, "get_json", side_effect=[timeout_422()] * 3), \
             mock.patch.object(collect_reddit.time, "sleep"):
            with self.assertRaises(RuntimeError) as cm:
                collect_reddit.fetch_posts(object(), RD_CFG, "flow", None, ["salesforce"], 100)
        self.assertIn("refused every subreddit", str(cm.exception))

    def test_other_4xx_errors_are_not_retried(self):
        resp = mock.Mock()
        resp.status_code = 400
        resp.json.return_value = {"data": None, "error": "'permalink' is not a valid field"}
        bad = requests.HTTPError("HTTP 400 Bad Request from https://x", response=resp)
        with mock.patch.object(collect_reddit, "get_json", side_effect=bad) as gj:
            with self.assertRaises(requests.HTTPError):
                collect_reddit.fetch_posts(object(), RD_CFG, "flow", None, ["salesforce"], 100)
        self.assertEqual(gj.call_count, 1)

    def test_search_uses_the_config_subreddits_unless_an_option_overrides_them(self):
        since = datetime(2024, 11, 25, tzinfo=timezone.utc)
        with mock.patch.object(collect_reddit, "fetch_posts", return_value=[RD_POST]) as fp:
            collect_reddit.search(object(), RD_CFG, "flow", since)
            self.assertEqual(fp.call_args.args[4], ["salesforce", "salesforceadmin"])
            collect_reddit.search(object(), RD_CFG, "flow", since, subreddits=["sysadmin"])
            self.assertEqual(fp.call_args.args[4], ["sysadmin"])
        with self.assertRaises(ValueError):
            collect_reddit.search(object(), {"reddit": dict(RD_CFG["reddit"], subreddits=[])}, "flow", since)


if __name__ == "__main__":
    unittest.main()
