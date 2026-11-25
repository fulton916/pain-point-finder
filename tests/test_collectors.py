# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Fixture tests for the collectors: no network, just the record-building and query-building code."""

import unittest
from datetime import datetime, timezone

import collect_gh
import collect_hn
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


if __name__ == "__main__":
    unittest.main()
