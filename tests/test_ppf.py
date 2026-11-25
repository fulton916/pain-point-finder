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
                "records": 7, "rows": 6, "shown": 1, "merges": 1, "store_totals": {"github": 5, "hackernews": 2},
                "note": "note text",
            },
            "rows": [{
                "rank": 1, "title": "Flow [beta] limits", "best_url": "https://github.com/a/b/issues/1",
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
        self.assertIn("github + hackernews · 42 reactions · 7 comments · date 2026-03-04 · closed", md)
        self.assertNotIn("Strong", md)
        self.assertIn("Something went wrong...", md)

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


if __name__ == "__main__":
    unittest.main()
