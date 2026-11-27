# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Tests for import_ideaexchange.py search and the substatus column (issue #31).  Run:  python -m unittest -v"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import import_ideaexchange as ie  # noqa: E402
import ppf  # noqa: E402
from common import ROOT, make_record, write_jsonl  # noqa: E402

SINCE_2024 = datetime(2024, 11, 27, tzinfo=timezone.utc)


def idea(url, title, votes, created="2007-04-19", status="Open", body=""):
    return make_record(
        source="ideaexchange", url=url, title=title, body=body, score=votes, comments="", views="",
        created_at=created, last_activity="", status=status, fetched_at="2026-11-27T00:00:00+00:00",
    )


class SearchTests(unittest.TestCase):
    """search() reads data/raw/ideaexchange.jsonl under cfg paths.raw_dir; an absolute raw_dir keeps the test
    away from the real file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {"paths": {"raw_dir": self.tmp.name}, "topic": "Salesforce"}
        write_jsonl(str(Path(self.tmp.name) / "ideaexchange.jsonl"), [
            idea("https://ideas.example.com/1", "Schedule Reports as Attachments", 9376, created="2009-01-08", status="Delivered"),
            idea("https://ideas.example.com/2", "Increase the limit on the number of reports", 9608, created="2015-03-05",
                 status="In Development"),
            idea("https://ideas.example.com/3", "Inline Editing for Related Lists", 9060, created="2007-10-05"),
        ])

    def tearDown(self):
        self.tmp.cleanup()

    def test_since_is_ignored_rows_posted_before_the_window_are_returned(self):
        rows = ie.search(None, self.cfg, "reports", SINCE_2024)
        self.assertEqual([r["score"] for r in rows], [9608, 9376])

    def test_info_sentence_says_rows_are_not_date_filtered(self):
        info: list[str] = []
        ie.search(None, self.cfg, "reports", SINCE_2024, info=info, notes=[])
        self.assertEqual(info, ["IdeaExchange rows are not date-filtered (the CSV has posted dates only)."])

    def test_topic_word_is_skipped_so_salesforce_reports_means_reports(self):
        rows = ie.search(None, self.cfg, "salesforce reports", SINCE_2024)
        self.assertEqual(len(rows), 2)
        self.assertEqual(ie.search(None, dict(self.cfg, topic="Other"), "salesforce reports", SINCE_2024), [])

    def test_every_other_word_must_still_match(self):
        self.assertEqual(ie.search(None, self.cfg, "reports attachments", SINCE_2024)[0]["score"], 9376)
        self.assertEqual(ie.search(None, self.cfg, "reports nowhere", SINCE_2024), [])

    def test_missing_file_returns_nothing_and_says_why_in_notes_not_info(self):
        info: list[str] = []
        notes: list[str] = []
        cfg = {"paths": {"raw_dir": str(Path(self.tmp.name) / "none")}, "topic": "Salesforce"}
        self.assertEqual(ie.search(None, cfg, "reports", SINCE_2024, info=info, notes=notes), [])
        self.assertEqual(info, [])
        self.assertEqual(notes, ["ideaexchange skipped: ideaexchange.jsonl not found; run import_ideaexchange.py first."])


class SubstatusTests(unittest.TestCase):
    def row(self, **extra):
        base = {"title": "Dupe Alerts", "url": "https://ideas.example.com/13", "points": "67200", "votes": "6818",
                "status": "Delivered", "posted_date": "2006-10-05", "comments": ""}
        base.update(extra)
        return base

    def test_substatus_goes_into_the_body_after_points(self):
        rec = ie.row_to_record(self.row(substatus="Partial"), "2026-11-27T00:00:00+00:00", 2)
        self.assertEqual(rec["body"], "points: 67200; sub-status: Partial")
        self.assertEqual(rec["status"], "Delivered")

    def test_substatus_alone_when_points_are_missing(self):
        rec = ie.row_to_record(self.row(points="", substatus="Not Planned"), "2026-11-27T00:00:00+00:00", 2)
        self.assertEqual(rec["body"], "sub-status: Not Planned")

    def test_no_substatus_column_or_empty_cell_leaves_the_body_as_before(self):
        self.assertEqual(ie.row_to_record(self.row(), "2026-11-27T00:00:00+00:00", 2)["body"], "points: 67200")
        self.assertEqual(ie.row_to_record(self.row(substatus="  "), "2026-11-27T00:00:00+00:00", 2)["body"], "points: 67200")

    def test_substatus_is_optional_not_required(self):
        self.assertNotIn("substatus", ie.REQUIRED_COLUMNS)
        self.assertIn("substatus", ie.OPTIONAL_COLUMNS)

    def test_substatus_prints_under_the_row_in_markdown(self):
        result = {
            "meta": {"query": "dupe", "since": "24m", "since_date": "2024-11-27", "records": 1, "rows": 1, "merges": 0,
                     "dropped": 0, "shown": 1, "requests_sent": 0, "generated": "2026-11-27T00:00:00+00:00",
                     "note": "", "per_source": {"ideaexchange": {"records": 1, "from_cache": False}}},
            "rows": [{"rank": 1, "title": "Dupe Alerts", "best_url": "https://ideas.example.com/13",
                      "best_source": "ideaexchange", "source_count": 1, "sources": "ideaexchange",
                      "top_normalized_score": 1.0, "raw_score": 6818, "comments": "", "latest_activity": "2006-10-05",
                      "status": "Delivered", "evidence_label": "Moderate", "final_score": 0.1,
                      "snippet": "points: 67200; sub-status: Partial"}],
        }
        md = ppf.format_md(result)
        self.assertIn("**Moderate** · ideaexchange · 6818 votes · date 2006-10-05 · Delivered", md)
        self.assertIn("   points: 67200; sub-status: Partial", md)


class RealCsvTests(unittest.TestCase):
    """The checked-in CSV: 30 hand-read rows, a substatus column, no new ideas (issue #31)."""

    def setUp(self):
        with open(ROOT / "data" / "ideaexchange.csv", encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            self.fieldnames = reader.fieldnames
            self.rows = list(reader)

    def test_header_is_the_documented_columns_plus_substatus(self):
        self.assertEqual(self.fieldnames, ie.REQUIRED_COLUMNS + ie.OPTIONAL_COLUMNS)

    def test_still_thirty_rows_with_the_main_status_in_status(self):
        self.assertEqual(len(self.rows), 30)
        self.assertTrue({r["status"] for r in self.rows} <= {"Open", "In Development", "Delivered", "Archived"})
        self.assertTrue(all(r["url"].startswith("https://ideas.salesforce.com/") for r in self.rows))

    def test_substatus_values_are_the_sites_second_status_line(self):
        allowed = {"Partial", "Complete", "Building", "Submitted", "Not Planned", "Never", "On Roadmap", "Beta Testing", ""}
        self.assertTrue({r["substatus"] for r in self.rows} <= allowed)
        self.assertIn("Partial", {r["substatus"] for r in self.rows})
        self.assertIn("Not Planned", {r["substatus"] for r in self.rows})

    def test_the_whole_file_imports_with_substatus_in_every_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "ie.jsonl")
            self.assertEqual(ie.import_csv(ROOT / "data" / "ideaexchange.csv", out), 30)
            from common import read_jsonl
            bodies = [r["body"] for r in read_jsonl(out)]
        self.assertTrue(all("sub-status: " in b for b in bodies))


if __name__ == "__main__":
    unittest.main()
