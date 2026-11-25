# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

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
    "source_thresholds": {
        "github": {"moderate": 5, "strong": 20},
        "hackernews": {"moderate": 50, "strong": 150},
    },
    "status_weights": {"default": 1.0, "closed": 0.5, "Delivered": 0.25},
    "drop_title_patterns": ["^Show HN:", r"who is hiring\?"],
}
AS_OF = datetime(2026, 11, 24, tzinfo=timezone.utc)


def rec(source, url, title, score, created="2026-06-01T00:00:00+00:00", comments="", last_activity="", status=""):
    return make_record(
        source=source, url=url, title=title, score=score, comments=comments, status=status,
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


class SearchModeLabelTests(unittest.TestCase):
    """Search mode labels come from scoring.source_thresholds, not from percentiles."""

    def labels(self, recs):
        rows, _ = ms.rank(recs, SCORING, AS_OF, mode="search")
        return {r["best_url"]: r["evidence_label"] for r in rows}

    def test_threshold_labels_strong_moderate_anecdotal(self):
        recs = [
            rec("github", "https://github.com/o/r/issues/1", "Issue with twenty reactions", 20),   # at strong
            rec("github", "https://github.com/o/r/issues/2", "Issue with five reactions", 5),      # at moderate
            rec("github", "https://github.com/o/r/issues/3", "Issue with four reactions", 4),      # below
        ]
        labels = self.labels(recs)
        self.assertEqual(labels["https://github.com/o/r/issues/1"], "Strong")
        self.assertEqual(labels["https://github.com/o/r/issues/2"], "Moderate")
        self.assertEqual(labels["https://github.com/o/r/issues/3"], "Anecdotal")

    def test_percentile_top_is_not_strong_when_below_threshold(self):
        # In sweep mode the top of three rows is Strong (percentile 1.0). In search mode a score
        # of 3 reactions is below the moderate threshold, so it is only Anecdotal.
        recs = [rec("github", f"https://github.com/o/r/issues/{i}", f"Low-vote issue number {i}", i) for i in (1, 2, 3)]
        sweep, _ = ms.rank(recs, SCORING, AS_OF)
        self.assertEqual(sweep[0]["evidence_label"], "Strong")
        self.assertEqual(self.labels(recs)["https://github.com/o/r/issues/3"], "Anecdotal")

    def test_strong_threshold_needs_recent_activity(self):
        old = rec("github", "https://github.com/o/r/issues/1", "Old issue with many reactions", 500, created="2021-01-01T00:00:00+00:00")
        self.assertEqual(self.labels([old])["https://github.com/o/r/issues/1"], "Moderate")

    def test_two_sources_is_strong_even_below_thresholds(self):
        a = rec("github", "https://github.com/o/r/issues/1", "Salesforce data export is painfully slow", 1)
        b = rec("ideaexchange", "https://ideas.example.com/2", "Salesforce data export is painfully slow", 1)
        rows, _ = ms.rank([a, b], SCORING, AS_OF, mode="search")
        self.assertEqual(rows[0]["evidence_label"], "Strong")

    def test_hacker_news_cap_still_applies(self):
        hn = rec("hackernews", "https://news.ycombinator.com/item?id=1", "Salesforce outage takes down everything", 900)
        self.assertEqual(self.labels([hn])["https://news.ycombinator.com/item?id=1"], "Moderate")

    def test_source_without_thresholds_is_anecdotal(self):
        ie = rec("ideaexchange", "https://ideas.example.com/1", "Idea with a huge vote count", 99999)
        self.assertEqual(self.labels([ie])["https://ideas.example.com/1"], "Anecdotal")

    def test_unknown_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            ms.rank(gh_spread(2), SCORING, AS_OF, mode="turbo")


class StatusWeightTests(unittest.TestCase):
    def test_closed_issue_scores_half_of_open_twin(self):
        open_ = rec("github", "https://github.com/o/r/issues/1", "Open issue about flow limits", 10, created="2026-11-24T00:00:00+00:00", status="open")
        closed = rec("github", "https://github.com/o/r/issues/2", "Closed issue about flow limits", 10, created="2026-11-24T00:00:00+00:00", status="closed")
        rows, _ = ms.rank([open_, closed], SCORING, AS_OF)
        by_url = {r["best_url"]: r for r in rows}
        self.assertAlmostEqual(by_url[open_["url"]]["final_score"], 1.0, places=4)
        self.assertAlmostEqual(by_url[closed["url"]]["final_score"], 0.5, places=4)
        self.assertEqual(rows[0]["best_url"], open_["url"])
        self.assertEqual(by_url[closed["url"]]["status"], "closed")  # shown unchanged

    def test_status_match_ignores_case_and_unknown_uses_default(self):
        self.assertEqual(ms.status_weight("CLOSED", SCORING), 0.5)
        self.assertEqual(ms.status_weight("delivered", SCORING), 0.25)
        self.assertEqual(ms.status_weight("Under Consideration", SCORING), 1.0)
        self.assertEqual(ms.status_weight("", SCORING), 1.0)
        self.assertEqual(ms.status_weight("closed", {}), 1.0)

    def test_status_weight_applies_in_search_mode_too(self):
        closed = rec("github", "https://github.com/o/r/issues/2", "Closed issue about flow limits", 10, created="2026-11-24T00:00:00+00:00", status="closed")
        rows, _ = ms.rank([closed], SCORING, AS_OF, mode="search")
        self.assertAlmostEqual(rows[0]["final_score"], 0.5, places=4)


class DropPatternTests(unittest.TestCase):
    def test_matching_title_is_dropped_and_logged(self):
        show = rec("hackernews", "https://news.ycombinator.com/item?id=1", "Show HN: A new CRM that is not Salesforce", 300)
        keep = rec("hackernews", "https://news.ycombinator.com/item?id=2", "Salesforce Flow limits are driving me nuts", 80)
        rows, log = ms.rank([show, keep], SCORING, AS_OF)
        self.assertEqual([r["best_url"] for r in rows], [keep["url"]])
        self.assertEqual(len(log), 1)
        entry = log[0]
        self.assertEqual(entry["reason"], "dropped_title_pattern")
        self.assertEqual(entry["merged_url"], show["url"])
        self.assertEqual(entry["merged_title"], show["title"])
        self.assertEqual(entry["merged_source"], "hackernews")
        self.assertEqual(entry["detail"], "^Show HN:")
        self.assertEqual(entry["kept_url"], "")
        self.assertEqual(sorted(entry), sorted(ms.MERGE_LOG_COLUMNS))

    def test_match_ignores_case_and_is_searched_anywhere(self):
        r = rec("hackernews", "https://news.ycombinator.com/item?id=1", "Ask HN: WHO IS HIRING? (November 2026)", 300)
        rows, log = ms.rank([r], SCORING, AS_OF)
        self.assertEqual(rows, [])
        self.assertEqual(log[0]["detail"], r"who is hiring\?")

    def test_non_matching_title_is_kept_and_not_logged(self):
        r = rec("hackernews", "https://news.ycombinator.com/item?id=1", "Salesforce shows HN users its new pricing", 80)
        rows, log = ms.rank([r], SCORING, AS_OF)
        self.assertEqual(len(rows), 1)
        self.assertEqual(log, [])

    def test_drop_happens_before_percentiles_and_dedupe(self):
        # Without the drop, the Show HN row (score 300) would be the 1.0 percentile and the other
        # row 0.5. With it gone, the remaining row is the top of its source.
        show = rec("hackernews", "https://news.ycombinator.com/item?id=1", "Show HN: Something shiny", 300)
        keep = rec("hackernews", "https://news.ycombinator.com/item?id=2", "Salesforce Flow limits are driving me nuts", 80)
        rows, _ = ms.rank([show, keep], SCORING, AS_OF)
        self.assertEqual(rows[0]["top_normalized_score"], 1.0)

    def test_drop_applies_in_search_mode(self):
        show = rec("hackernews", "https://news.ycombinator.com/item?id=1", "Show HN: Something shiny", 300)
        rows, log = ms.rank([show], SCORING, AS_OF, mode="search")
        self.assertEqual(rows, [])
        self.assertEqual(log[0]["reason"], "dropped_title_pattern")

    def test_no_patterns_means_nothing_dropped(self):
        scoring = {k: v for k, v in SCORING.items() if k != "drop_title_patterns"}
        rows, log = ms.rank(gh_spread(3), scoring, AS_OF)
        self.assertEqual(len(rows), 3)
        self.assertEqual(log, [])

    def test_bad_regex_stops_with_a_message_naming_it(self):
        scoring = dict(SCORING, drop_title_patterns=["^Show HN:", "(unclosed"])
        with self.assertRaises(SystemExit) as cm:
            ms.rank(gh_spread(2), scoring, AS_OF)
        msg = str(cm.exception)
        self.assertIn("drop_title_patterns", msg)
        self.assertIn("entry 2", msg)
        self.assertIn("(unclosed", msg)


    def test_blank_or_empty_pattern_is_rejected(self):
        for bad in (None, "", "   ", 42):
            scoring = dict(SCORING, drop_title_patterns=["^Show HN:", bad])
            with self.assertRaises(SystemExit, msg=repr(bad)) as cm:
                ms.rank(gh_spread(2), scoring, AS_OF)
            self.assertIn("entry 2", str(cm.exception))

    def test_patterns_must_be_a_list(self):
        scoring = dict(SCORING, drop_title_patterns="^Show HN:")
        with self.assertRaises(SystemExit) as cm:
            ms.rank(gh_spread(2), scoring, AS_OF)
        self.assertIn("must be a list", str(cm.exception))

    def test_dropped_row_does_not_set_the_as_of_date(self):
        # The only row fetched later is dropped, so "today" comes from the surviving row and its
        # recency weight is a full 1.0.
        keep = rec("github", "https://github.com/o/r/issues/1", "Issue fetched on the earlier day", 10, created="2026-11-24T00:00:00+00:00")
        show = rec("hackernews", "https://news.ycombinator.com/item?id=1", "Show HN: Fetched a year later", 300)
        show["fetched_at"] = "2027-11-24T00:00:00+00:00"
        rows, _ = ms.rank([keep, show], SCORING)  # no as_of: taken from the data
        self.assertAlmostEqual(rows[0]["final_score"], 1.0, places=4)


class ConfigValidationTests(unittest.TestCase):
    def test_threshold_must_have_both_numbers(self):
        scoring = dict(SCORING, source_thresholds={"github": {"moderate": 5}})
        with self.assertRaises(SystemExit) as cm:
            ms.rank(gh_spread(2), scoring, AS_OF)
        self.assertIn("source_thresholds.github", str(cm.exception))

    def test_strong_below_moderate_is_rejected(self):
        scoring = dict(SCORING, source_thresholds={"github": {"moderate": 50, "strong": 5}})
        with self.assertRaises(SystemExit):
            ms.rank(gh_spread(2), scoring, AS_OF)

    def test_negative_status_weight_is_rejected(self):
        scoring = dict(SCORING, status_weights={"closed": -1})
        with self.assertRaises(SystemExit) as cm:
            ms.rank(gh_spread(2), scoring, AS_OF)
        self.assertIn("status_weights.closed", str(cm.exception))

    def test_real_config_validates(self):
        from common import load_config
        patterns = ms.validate_scoring(load_config()["scoring"])
        self.assertTrue(patterns)


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
