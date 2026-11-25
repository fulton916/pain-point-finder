"""Tests for the command line glue: argument parsing helpers and output formatting. No network."""

import json
import unittest
from datetime import datetime, timezone

import ppf
from common import parse_since

NOW = datetime(2026, 11, 24, tzinfo=timezone.utc)


class SinceTests(unittest.TestCase):
    def test_months_days_weeks_years(self):
        self.assertEqual(parse_since("24m", NOW).date().isoformat(), "2024-11-24")
        self.assertEqual(parse_since("30d", NOW).date().isoformat(), "2026-10-25")
        self.assertEqual(parse_since("2w", NOW).date().isoformat(), "2026-11-10")
        self.assertEqual(parse_since("1y", NOW).date().isoformat(), "2025-11-24")

    def test_month_end_is_clamped(self):
        march_31 = datetime(2026, 3, 31, tzinfo=timezone.utc)
        self.assertEqual(parse_since("1m", march_31).date().isoformat(), "2026-02-28")
        self.assertEqual(parse_since("13m", march_31).date().isoformat(), "2025-02-28")

    def test_plain_date(self):
        self.assertEqual(parse_since("2025-01-15", NOW), datetime(2025, 1, 15, tzinfo=timezone.utc))

    def test_bad_value_raises(self):
        with self.assertRaises(ValueError):
            parse_since("yesterday", NOW)


class SourceParsingTests(unittest.TestCase):
    def test_short_names_and_aliases(self):
        self.assertEqual(ppf.parse_sources("hn,github, HN"), ["hn", "gh"])

    def test_unknown_source_raises(self):
        with self.assertRaises(ValueError):
            ppf.parse_sources("hn,reddit")


class FormattingTests(unittest.TestCase):
    def result(self):
        return {
            "meta": {
                "query": "flow limits", "sources": ["hackernews", "github"], "since": "24m", "since_date": "2024-11-24",
                "generated": "2026-11-24T00:00:00+00:00", "requests_sent": 3,
                "per_source": {"hackernews": {"records": 2, "from_cache": False}, "github": {"records": 5, "from_cache": True}},
                "records": 7, "rows": 6, "shown": 1, "merges": 1, "dropped": 2, "store_totals": {"github": 5, "hackernews": 2},
                "note": "note text",
            },
            "rows": [{
                "rank": 1, "title": "Flow [beta] limits", "best_url": "https://github.com/a/b/issues/1",
                "best_source": "github",
                "source_count": 2, "sources": "github;hackernews", "top_normalized_score": 1.0, "raw_score": 42,
                "comments": 7, "latest_activity": "2026-03-04", "status": "closed", "evidence_label": "Strong",
                "final_score": 1.2, "snippet": "Something went wrong...",
            }],
        }

    def test_markdown_has_link_units_and_meta(self):
        md = ppf.format_md(self.result())
        self.assertIn('# Pain Point Finder: "flow limits"', md)
        self.assertIn("hackernews (2 records, fetched)", md)
        self.assertIn("github (5 records, cache)", md)
        self.assertIn("Requests sent: 3", md)
        self.assertIn("**[Flow \\[beta\\] limits](https://github.com/a/b/issues/1)**", md)
        self.assertIn("6 after dedupe (1 merges, 2 dropped)", md)
        self.assertIn("**Strong** · github + hackernews · 42 reactions · 7 comments · date 2026-03-04 · closed", md)
        self.assertIn("Something went wrong...", md)

    def test_unit_follows_the_best_record_not_the_first_source(self):
        r = self.result()
        r["rows"][0]["best_source"] = "hackernews"
        r["rows"][0]["raw_score"] = 300
        self.assertIn("github + hackernews · 300 points", ppf.format_md(r))

    def test_markdown_with_no_rows(self):
        r = self.result()
        r["rows"] = []
        self.assertIn("No results.", ppf.format_md(r))

    def test_json_round_trips(self):
        data = json.loads(ppf.format_json(self.result()))
        self.assertEqual(data["rows"][0]["raw_score"], 42)

    def test_snippet_collapses_whitespace_and_cuts(self):
        self.assertEqual(ppf.snippet("a  b\n\nc", 100), "a b c")
        self.assertEqual(ppf.snippet("abcdefghij", 8), "abcde...")


class FakeSource:
    """Stands in for a collector module: records SOURCE and a search() that counts calls."""
    SOURCE = "fakesource"

    def __init__(self, records, fail=False):
        self.records, self.fail, self.calls = records, fail, 0

    def search(self, session, cfg, query, since, **options):
        self.calls += 1
        if self.fail:
            raise ValueError("boom")
        return self.records


class RunSearchTests(unittest.TestCase):
    def setUp(self):
        from common import load_config, make_record
        from store import Store

        self.cfg = load_config()
        self.store = Store(":memory:")
        self.rec = make_record(
            source="fakesource", url="https://example.com/1", title="Flow limits hit", body="Body text here",
            score=10, comments=2, created_at="2026-01-01T00:00:00Z", fetched_at="2026-11-24T00:00:00+00:00",
        )
        self.fake = FakeSource([self.rec])
        ppf.SOURCES["fake"] = self.fake
        ppf.UNITS["fakesource"] = "votes"

    def tearDown(self):
        ppf.SOURCES.pop("fake", None)
        ppf.UNITS.pop("fakesource", None)
        self.store.close()

    def run_it(self, **kw):
        args = dict(cfg=self.cfg, query="flow limits", sources=["fake"], since_text="24m", refresh=False,
                    limit=10, repos=None, title_only=None, store=self.store, session=object())
        args.update(kw)
        return ppf.run_search(**args)

    def test_first_run_fetches_second_run_uses_cache(self):
        first = self.run_it()
        second = self.run_it(query="Flow  LIMITS")
        self.assertEqual(self.fake.calls, 1)
        self.assertFalse(first["meta"]["per_source"]["fakesource"]["from_cache"])
        self.assertTrue(second["meta"]["per_source"]["fakesource"]["from_cache"])
        self.assertEqual(second["rows"][0]["best_url"], "https://example.com/1")
        self.assertEqual(second["rows"][0]["best_source"], "fakesource")
        self.assertEqual(second["rows"][0]["snippet"], "Body text here")

    def test_different_options_do_not_share_a_cache(self):
        self.run_it()
        self.run_it(repos=["acme/x"])
        self.run_it(title_only=False)
        self.run_it(since_text="6m")
        self.assertEqual(self.fake.calls, 4)

    def test_refresh_bypasses_cache(self):
        self.run_it()
        self.run_it(refresh=True)
        self.assertEqual(self.fake.calls, 2)

    def test_source_error_is_reported_not_raised(self):
        self.fake.fail = True
        result = self.run_it()
        self.assertEqual(result["rows"], [])
        self.assertEqual(result["meta"]["per_source"]["fakesource"]["error"], "boom")
        self.assertIn("fakesource (failed: boom)", ppf.format_md(result))


if __name__ == "__main__":
    unittest.main()
