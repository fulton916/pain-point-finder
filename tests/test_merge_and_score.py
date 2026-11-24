"""Tests for merge_and_score.py.  Run:  python -m unittest -v"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import merge_and_score as ms  # noqa: E402
from common import FIELDS, make_record  # noqa: E402

SCORING = {
    "recency_half_life_months": 12,
    "missing_date_weight": 0.5,
    "comment_bonus_scale": 0.02,
    "comment_bonus_cap": 0.10,
    "cross_source_bonus": 0.25,
    "title_similarity_threshold": 0.95,
    "title_min_length": 25,
    "strong_percentile": 0.90,
    "moderate_percentile": 0.75,
    "strong_activity_months": 24,
    "label_caps": {"hackernews": "Moderate"},
}
AS_OF = datetime(2026, 11, 24, tzinfo=timezone.utc)


def rec(source, url, title, score, created="2026-06-01T00:00:00+00:00", comments="", last_activity=""):
    return make_record(
        source=source, url=url, title=title, score=score, comments=comments,
        created_at=created, last_activity=last_activity, fetched_at="2026-11-24T00:00:00+00:00",
    )


def gh_spread(n=10):
    """n GitHub issues with scores 1..n so percentiles are easy to reason about."""
    return [rec("github", f"https://github.com/o/r/issues/{i}", f"GitHub issue number {i} about something", i) for i in range(1, n + 1)]


class NormalizeTests(unittest.TestCase):
    def test_url_normalization_is_conservative(self):
        self.assertEqual(ms.normalize_url("HTTPS://GitHub.com/a/b/#frag"), "https://github.com/a/b")
        self.assertEqual(ms.normalize_url("https://x.com/a?id=1"), "https://x.com/a?id=1")
        self.assertNotEqual(ms.normalize_url("https://x.com/a?id=1"), ms.normalize_url("https://x.com/a?id=2"))

    def test_title_normalization(self):
        self.assertEqual(ms.normalize_title("  Flow: can't  Schedule!! "), "flow can t schedule")

    def test_percentiles(self):
        self.assertEqual(ms.percentile_ranks([10, 20, 30, 40]), [0.25, 0.5, 0.75, 1.0])
        self.assertEqual(ms.percentile_ranks([5, 5]), [1.0, 1.0])
        self.assertEqual(ms.percentile_ranks([]), [])

    def test_parse_date_forms(self):
        self.assertIsNotNone(ms.parse_date("2026-01-02T03:04:05Z"))
        self.assertIsNotNone(ms.parse_date("2026-01-02"))
        self.assertIsNone(ms.parse_date(""))
        self.assertIsNone(ms.parse_date("yesterday"))


class DedupeTests(unittest.TestCase):
    def test_exact_url_merge_is_logged(self):
        a = rec("github", "https://github.com/o/r/issues/1", "Some title here", 10)
        b = rec("hackernews", "https://github.com/o/r/issues/1/", "Completely different title", 5)
        rows, log = ms.rank([a, b], SCORING, AS_OF)
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(log), 1)
        self.assertEqual(log[0]["reason"], "url_exact")
        self.assertEqual(rows[0]["source_count"], 2)

    def test_exact_title_merge_is_logged(self):
        a = rec("github", "https://github.com/o/r/issues/1", "Cannot export report to Excel", 10)
        b = rec("ideaexchange", "https://ideas.example.com/2", "cannot export report to excel!", 300)
        rows, log = ms.rank([a, b], SCORING, AS_OF)
        self.assertEqual(len(rows), 1)
        self.assertEqual(log[0]["reason"], "title_exact")
        self.assertEqual(rows[0]["sources"], "github;ideaexchange")

    def test_near_duplicate_long_title_merges(self):
        t1 = "Allow scheduled flows to run every fifteen minutes instead of daily"
        t2 = "Allow scheduled flows to run every fifteen minutes instead of daily."
        a = rec("github", "https://github.com/o/r/issues/1", t1, 10)
        b = rec("ideaexchange", "https://ideas.example.com/2", t2, 300)
        rows, log = ms.rank([a, b], SCORING, AS_OF)
        self.assertEqual(len(rows), 1)
        self.assertIn(log[0]["reason"], ("title_exact", "title_similar"))

    def test_similar_but_different_titles_do_not_merge(self):
        a = rec("github", "https://github.com/o/r/issues/1", "Allow scheduled flows to run every 15 minutes", 10)
        b = rec("ideaexchange", "https://ideas.example.com/2", "Allow scheduled flows to run every 30 minutes", 300)
        rows, log = ms.rank([a, b], SCORING, AS_OF)
        self.assertEqual(len(rows), 2)
        self.assertEqual(log, [])

    def test_short_titles_never_merge_by_similarity(self):
        a = rec("github", "https://github.com/o/r/issues/1", "Export fails", 10)
        b = rec("ideaexchange", "https://ideas.example.com/2", "Export fail", 300)
        rows, log = ms.rank([a, b], SCORING, AS_OF)
        self.assertEqual(len(rows), 2)
        self.assertEqual(log, [])

    def test_every_row_merged_appears_in_log(self):
        base = gh_spread(5)
        dup = [rec("hackernews", f"https://news.ycombinator.com/item?id={i}", r["title"], 60) for i, r in enumerate(base)]
        rows, log = ms.rank(base + dup, SCORING, AS_OF)
        self.assertEqual(len(rows), 5)
        self.assertEqual(len(log), 5)
        self.assertEqual(len(base) + len(dup), len(rows) + len(log))


class LabelTests(unittest.TestCase):
    def test_top_recent_single_source_is_strong(self):
        rows, _ = ms.rank(gh_spread(10), SCORING, AS_OF)
        top = rows[0]
        self.assertEqual(top["top_normalized_score"], 1.0)
        self.assertEqual(top["evidence_label"], "Strong")

    def test_top_but_stale_single_source_is_not_strong(self):
        recs = gh_spread(10)
        for r in recs:
            r["created_at"] = "2021-01-01T00:00:00+00:00"
        rows, _ = ms.rank(recs, SCORING, AS_OF)
        self.assertEqual(rows[0]["evidence_label"], "Moderate")

    def test_middle_is_moderate_and_bottom_anecdotal(self):
        rows, _ = ms.rank(gh_spread(10), SCORING, AS_OF)
        by_url = {r["best_url"]: r for r in rows}
        self.assertEqual(by_url["https://github.com/o/r/issues/8"]["evidence_label"], "Moderate")
        self.assertEqual(by_url["https://github.com/o/r/issues/3"]["evidence_label"], "Anecdotal")

    def test_hacker_news_only_rows_are_capped_at_moderate(self):
        recs = [rec("hackernews", f"https://news.ycombinator.com/item?id={i}", f"Hacker News story number {i} about salesforce", i) for i in range(1, 11)]
        rows, _ = ms.rank(recs, SCORING, AS_OF)
        self.assertTrue(all(r["evidence_label"] != "Strong" for r in rows))
        self.assertEqual(rows[0]["evidence_label"], "Moderate")

    def test_hacker_news_plus_another_source_can_be_strong(self):
        a = rec("hackernews", "https://news.ycombinator.com/item?id=1", "Salesforce data export is painfully slow", 80)
        b = rec("github", "https://github.com/o/r/issues/9", "Salesforce data export is painfully slow", 10)
        rows, _ = ms.rank([a, b], SCORING, AS_OF)
        self.assertEqual(rows[0]["evidence_label"], "Strong")

    def test_two_sources_is_strong_even_when_low_in_each(self):
        base = gh_spread(10)
        twin = rec("ideaexchange", "https://ideas.example.com/1", base[0]["title"], 1)
        rows, _ = ms.rank(base + [twin], SCORING, AS_OF)
        row = next(r for r in rows if r["source_count"] == 2)
        self.assertEqual(row["evidence_label"], "Strong")


class WeightTests(unittest.TestCase):
    def test_recency_halves_every_twelve_months(self):
        fresh = rec("github", "https://github.com/o/r/issues/1", "A fresh issue with a long title", 10, created="2026-11-24T00:00:00+00:00")
        old = rec("github", "https://github.com/o/r/issues/2", "An old issue with a long title", 10, created="2025-11-24T00:00:00+00:00")
        rows, _ = ms.rank([fresh, old], SCORING, AS_OF)
        by_url = {r["best_url"]: r for r in rows}
        self.assertAlmostEqual(by_url[fresh["url"]]["final_score"], 1.0, places=2)
        self.assertAlmostEqual(by_url[old["url"]]["final_score"], 0.5, places=2)

    def test_last_activity_beats_created_at(self):
        r = rec("github", "https://github.com/o/r/issues/1", "Issue with recent activity", 10,
                created="2020-01-01T00:00:00+00:00", last_activity="2026-11-01T00:00:00+00:00")
        rows, _ = ms.rank([r], SCORING, AS_OF)
        self.assertEqual(rows[0]["latest_activity"], "2026-11-01")

    def test_missing_date_uses_fixed_weight(self):
        r = rec("ideaexchange", "https://ideas.example.com/1", "Idea with no date at all", 10, created="")
        rows, _ = ms.rank([r], SCORING, AS_OF)
        self.assertEqual(rows[0]["latest_activity"], "")
        self.assertAlmostEqual(rows[0]["final_score"], 0.5, places=4)

    def test_comment_bonus_is_small_and_capped(self):
        quiet = rec("github", "https://github.com/o/r/issues/1", "Quiet issue with nobody talking", 10, created="2026-11-24T00:00:00+00:00", comments=0)
        loud = rec("github", "https://github.com/o/r/issues/2", "Loud issue with everyone talking", 10, created="2026-11-24T00:00:00+00:00", comments=100000)
        rows, _ = ms.rank([quiet, loud], SCORING, AS_OF)
        by_url = {r["best_url"]: r for r in rows}
        self.assertAlmostEqual(by_url[loud["url"]]["final_score"] - by_url[quiet["url"]]["final_score"], 0.10, places=4)


class OutputTests(unittest.TestCase):
    def test_rerun_is_deterministic(self):
        recs = gh_spread(20) + [rec("hackernews", f"https://news.ycombinator.com/item?id={i}", f"Story number {i} about salesforce admins", 50 + i) for i in range(5)]
        first, log1 = ms.rank(recs, SCORING, AS_OF)
        second, log2 = ms.rank(list(reversed(recs)), SCORING, AS_OF)
        self.assertEqual(first, second)
        self.assertEqual(sorted(map(str, log1)), sorted(map(str, log2)))

    def test_as_of_comes_from_data_not_clock(self):
        a = rec("github", "https://github.com/o/r/issues/1", "Issue scored relative to fetch time", 10, created="2026-11-24T00:00:00+00:00")
        rows, _ = ms.rank([a], SCORING)  # no as_of: uses fetched_at (2026-11-24)
        self.assertAlmostEqual(rows[0]["final_score"], 1.0, places=2)

    def test_rows_have_all_output_columns(self):
        rows, _ = ms.rank(gh_spread(3), SCORING, AS_OF)
        for row in rows:
            for col in ms.RANKED_COLUMNS:
                self.assertIn(col, row)
        self.assertEqual([r["rank"] for r in rows], [1, 2, 3])

    def test_input_records_are_not_mutated(self):
        recs = gh_spread(3)
        ms.rank(recs, SCORING, AS_OF)
        self.assertEqual(sorted(recs[0].keys()), sorted(FIELDS))


if __name__ == "__main__":
    unittest.main()
