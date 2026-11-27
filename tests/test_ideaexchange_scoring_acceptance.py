# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Acceptance tests for issue #31 (IdeaExchange scoring), one class per "Done when" line. No network:
the ie source is a local file, so each test writes its own JSONL or CSV into a temp folder and points
cfg["paths"]["raw_dir"] at it. Tests on the checked-in CSV need no guard (it is tracked); the test on the
real data/raw/ideaexchange.jsonl is skipped where that file is missing (CI).  Run:  python -m unittest -v"""

from __future__ import annotations

import csv
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import import_ideaexchange as ie  # noqa: E402
import merge_and_score as ms  # noqa: E402
import ppf  # noqa: E402
from common import ROOT, load_config, make_record, write_jsonl  # noqa: E402
from store import Store  # noqa: E402

AS_OF = datetime(2026, 11, 27, tzinfo=timezone.utc)
FETCHED = "2026-11-27T00:00:00+00:00"
RAW_JSONL = ROOT / "data" / "raw" / "ideaexchange.jsonl"

# Ideas the way the importer produces them: posted date only, no activity date, votes as the score.
IDEAS = [
    make_record(source="ideaexchange", url="https://ideas.example.com/1", title="Increase the limit on the number of reports",
                body="points: 95040; sub-status: Building", score=9608, comments="", views="", created_at="2015-03-05",
                last_activity="", status="In Development", fetched_at=FETCHED),
    make_record(source="ideaexchange", url="https://ideas.example.com/2", title="Schedule Reports as Attachments",
                body="points: 93080; sub-status: Complete", score=9376, comments="", views="", created_at="2009-01-08",
                last_activity="", status="Delivered", fetched_at=FETCHED),
    make_record(source="ideaexchange", url="https://ideas.example.com/3", title="Reports on Notes and Attachments",
                body="points: 51500; sub-status: Never", score=5208, comments="", views="", created_at="2006-10-04",
                last_activity="", status="Archived", fetched_at=FETCHED),
    make_record(source="ideaexchange", url="https://ideas.example.com/4", title="Export reports to Excel with formatting",
                body="points: 8000", score=800, comments="", views="", created_at="2007-04-19",
                last_activity="", status="Open", fetched_at=FETCHED),
]


def idea(url, title, votes, created="2007-04-19", status="Open", source="ideaexchange", last_activity="", body=""):
    return make_record(source=source, url=url, title=title, body=body, score=votes, comments="", views="",
                       created_at=created, last_activity=last_activity, status=status, fetched_at=FETCHED)


def run_ie_search(cfg, query="salesforce reports", since_text=None):
    """Drive ppf.run_search the way `python ppf.py search <query> --sources ie` does, with an in-memory store
    and no session (the ie source never makes a request). since_text None = the config default window."""
    since_text = since_text or str(cfg["search"]["default_since"])
    store = Store(":memory:")
    try:
        return ppf.run_search(cfg=cfg, query=query, sources=["ie"], since_text=since_text, refresh=True,
                              limit=30, repos=None, title_only=None, store=store, session=object())
    finally:
        store.close()


def sources_line(md: str) -> str:
    return next(line for line in md.splitlines() if line.startswith("Sources:"))


class DefaultWindowReturnsRowsAndHeaderSaysNotDateFiltered(unittest.TestCase):
    """Done when: `python ppf.py search "salesforce reports" --sources ie` returns IdeaExchange rows with the
    default window, and the header says they are not date-filtered."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["paths"]["raw_dir"] = self.tmp.name
        write_jsonl(str(Path(self.tmp.name) / "ideaexchange.jsonl"), IDEAS)

    def tearDown(self):
        self.tmp.cleanup()

    def test_search_salesforce_reports_sources_ie_returns_rows_with_the_default_window(self):
        self.assertEqual(str(self.cfg["search"]["default_since"]), "24m")
        result = run_ie_search(self.cfg)
        self.assertEqual(result["meta"]["since"], "24m")
        urls = {r["best_url"] for r in result["rows"]}
        self.assertEqual(urls, {r["url"] for r in IDEAS})  # every idea whose title has "reports"
        # Every returned row is older than the window, so the window was not applied.
        since_date = result["meta"]["since_date"]
        self.assertTrue(all(r["latest_activity"] < since_date for r in result["rows"]), result["rows"])
        self.assertEqual(result["meta"]["per_source"]["ideaexchange"]["records"], len(IDEAS))

    def test_header_says_rows_are_not_date_filtered(self):
        md = ppf.format_md(run_ie_search(self.cfg))
        line = sources_line(md)
        self.assertIn("ideaexchange (4 records, fetched; IdeaExchange rows are not date-filtered (the CSV has posted dates only))", line)

    def test_header_sentence_comes_from_the_source_module(self):
        info: list[str] = []
        ie.search(None, self.cfg, "salesforce reports", ms.parse_date("2024-11-27"), info=info, notes=[])
        self.assertEqual(info, [ie.NOT_DATE_FILTERED])
        self.assertIn("not date-filtered", ie.NOT_DATE_FILTERED)
        self.assertIn("posted dates only", ie.NOT_DATE_FILTERED)

    def test_a_narrow_since_changes_nothing_for_ie(self):
        wide = run_ie_search(self.cfg, since_text="24m")
        narrow = run_ie_search(self.cfg, since_text="30d")
        self.assertEqual([r["best_url"] for r in narrow["rows"]], [r["best_url"] for r in wide["rows"]])
        self.assertEqual(len(narrow["rows"]), len(IDEAS))

    def test_command_line_passes_sources_ie_and_the_default_window(self):
        """`python ppf.py search "salesforce reports" --sources ie`: argparse to run_search, no network."""
        fake_result = {"meta": {"per_source": {}}, "rows": [{"rank": 1}]}
        with mock.patch.object(ppf, "run_search", return_value=fake_result) as run, \
                mock.patch.object(ppf, "format_md", return_value="x") as fmt, \
                mock.patch.object(ppf.common, "load_config", return_value=self.cfg):
            rc = ppf.main(["search", "salesforce reports", "--sources", "ie"])
        self.assertEqual(rc, 0)
        self.assertTrue(fmt.called)
        args, kwargs = run.call_args
        self.assertEqual(args[1], "salesforce reports")
        self.assertEqual(args[2], ["ie"])
        self.assertEqual(args[3], "24m")  # no --since given: the config default window
        self.assertEqual(kwargs, {"site": None, "subreddits": None, "apps": None})

    @unittest.skipUnless(RAW_JSONL.exists(), "data/raw/ideaexchange.jsonl is not imported here (run import_ideaexchange.py)")
    def test_real_imported_file_answers_salesforce_reports_with_rows_and_the_header_sentence(self):
        cfg = load_config()  # the real raw_dir
        result = run_ie_search(cfg)
        self.assertGreater(len(result["rows"]), 0)
        self.assertTrue(all("reports" in r["title"].lower() for r in result["rows"]))
        self.assertIn("not date-filtered", sources_line(ppf.format_md(result)))


class OpenIdeaAtStrongIsStrongDeliveredOrArchivedIsNot(unittest.TestCase):
    """Done when: an Open idea at or above the `strong` threshold is labeled Strong; a Delivered or Archived
    one at the same votes is not."""

    def setUp(self):
        self.scoring = load_config()["scoring"]
        self.strong = int(self.scoring["source_thresholds"]["ideaexchange"]["strong"])

    def labels(self, recs):
        rows, _ = ms.rank(recs, self.scoring, AS_OF, mode="search")
        return {r["best_url"]: r["evidence_label"] for r in rows}

    def test_open_idea_at_the_strong_threshold_is_strong_without_any_activity_date(self):
        labels = self.labels([idea("https://ideas.example.com/open", "Open idea with many votes", self.strong, status="Open")])
        self.assertEqual(labels["https://ideas.example.com/open"], "Strong")

    def test_open_idea_above_the_threshold_posted_in_2007_is_strong(self):
        labels = self.labels([idea("https://ideas.example.com/open", "Open idea with many votes", 6683, created="2007-04-19")])
        self.assertEqual(labels["https://ideas.example.com/open"], "Strong")

    def test_in_development_idea_at_the_threshold_is_strong_too(self):
        labels = self.labels([idea("https://ideas.example.com/dev", "Idea being built", self.strong, status="In Development")])
        self.assertEqual(labels["https://ideas.example.com/dev"], "Strong")

    def test_delivered_idea_at_the_same_votes_is_not_strong(self):
        labels = self.labels([idea("https://ideas.example.com/done", "Delivered idea with many votes", self.strong, status="Delivered")])
        self.assertNotEqual(labels["https://ideas.example.com/done"], "Strong")
        self.assertEqual(labels["https://ideas.example.com/done"], "Moderate")

    def test_archived_idea_at_the_same_votes_is_not_strong(self):
        labels = self.labels([idea("https://ideas.example.com/dead", "Archived idea with many votes", self.strong, status="Archived")])
        self.assertNotEqual(labels["https://ideas.example.com/dead"], "Strong")
        self.assertEqual(labels["https://ideas.example.com/dead"], "Moderate")

    def test_all_three_statuses_side_by_side_at_the_same_votes(self):
        recs = [
            idea("https://ideas.example.com/o", "Open idea alpha", self.strong, status="Open"),
            idea("https://ideas.example.com/d", "Delivered idea beta", self.strong, status="Delivered"),
            idea("https://ideas.example.com/a", "Archived idea gamma", self.strong, status="Archived"),
        ]
        labels = self.labels(recs)
        self.assertEqual(labels["https://ideas.example.com/o"], "Strong")
        self.assertEqual(labels["https://ideas.example.com/d"], "Moderate")
        self.assertEqual(labels["https://ideas.example.com/a"], "Moderate")

    def test_open_idea_below_the_strong_threshold_is_not_strong(self):
        labels = self.labels([idea("https://ideas.example.com/mid", "Open idea with fewer votes", self.strong - 1, status="Open")])
        self.assertEqual(labels["https://ideas.example.com/mid"], "Moderate")

    def test_the_rule_is_a_config_list_naming_ideaexchange(self):
        self.assertEqual(self.scoring["strong_without_activity"], ["ideaexchange"])

    def test_other_sources_keep_the_24_month_rule(self):
        old_gh = idea("https://github.com/o/r/issues/1", "Old GitHub issue with many reactions", 500,
                      created="2021-01-01T00:00:00+00:00", status="open", source="github")
        self.assertEqual(self.labels([old_gh])["https://github.com/o/r/issues/1"], "Moderate")

    def test_without_the_config_list_an_open_idea_is_not_strong(self):
        scoring = dict(self.scoring, strong_without_activity=[])
        rows, _ = ms.rank([idea("https://ideas.example.com/open", "Open idea with many votes", self.strong)], scoring, AS_OF, mode="search")
        self.assertEqual(rows[0]["evidence_label"], "Moderate")


class ArchivedRanksBelowOpenAtTheSameVotes(unittest.TestCase):
    """Done when: an Archived idea ranks below an Open idea with the same votes."""

    def setUp(self):
        self.scoring = load_config()["scoring"]

    def test_archived_idea_ranks_below_open_idea_with_the_same_votes_and_date(self):
        archived = idea("https://ideas.example.com/a", "Reporting on notes and attachments", 5208, created="2006-10-04", status="Archived")
        open_ = idea("https://ideas.example.com/o", "Set accounts and contacts as inactive", 5208, created="2006-10-04", status="Open")
        rows, _ = ms.rank([archived, open_], self.scoring, AS_OF, mode="search")  # archived listed first on purpose
        self.assertEqual([r["best_url"] for r in rows], [open_["url"], archived["url"]])
        self.assertEqual(rows[0]["rank"], 1)
        self.assertEqual(rows[1]["status"], "Archived")  # shown unchanged

    def test_archived_scores_a_quarter_of_open_when_dates_are_equal(self):
        # No date: both get missing_date_weight, so the ratio is the status weight alone.
        archived = idea("https://ideas.example.com/a", "Archived idea with no date", 5208, created="", status="Archived")
        open_ = idea("https://ideas.example.com/o", "Open idea with no date", 5208, created="", status="Open")
        rows, _ = ms.rank([archived, open_], self.scoring, AS_OF, mode="search")
        by_url = {r["best_url"]: r["final_score"] for r in rows}
        self.assertAlmostEqual(by_url[archived["url"]], 0.25 * by_url[open_["url"]], places=4)

    def test_archived_and_not_planned_weights_are_in_config(self):
        self.assertEqual(ms.status_weight("Archived", self.scoring), 0.25)
        self.assertEqual(ms.status_weight("archived", self.scoring), 0.25)
        self.assertEqual(ms.status_weight("Not Planned", self.scoring), 0.25)
        self.assertEqual(ms.status_weight("Open", self.scoring), 1.0)
        self.assertEqual(ms.status_weight("In Development", self.scoring), 1.0)

    def test_archived_weight_applies_in_sweep_mode_too(self):
        archived = idea("https://ideas.example.com/a", "Archived idea with no date", 5208, created="", status="Archived")
        open_ = idea("https://ideas.example.com/o", "Open idea with no date", 5208, created="", status="Open")
        rows, _ = ms.rank([archived, open_], self.scoring, AS_OF, mode="sweep")
        self.assertEqual(rows[0]["best_url"], open_["url"])


class SubStatusPrintsOnTheRow(unittest.TestCase):
    """Done when: the sub-status prints on the row when the CSV has one."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = load_config()
        self.cfg["paths"]["raw_dir"] = self.tmp.name
        self.csv_path = Path(self.tmp.name) / "ideaexchange.csv"
        with open(self.csv_path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(ie.REQUIRED_COLUMNS + ie.OPTIONAL_COLUMNS)
            w.writerow(["Ability to Set Accounts as Inactive", "https://ideas.example.com/15", "65910", "6683", "Open", "2007-04-19", "", "Not Planned"])
            w.writerow(["Dependent page layouts for accounts", "https://ideas.example.com/1", "125570", "12619", "Delivered", "2006-11-16", "", "Partial"])
            w.writerow(["Inline editing on accounts related lists", "https://ideas.example.com/4", "90380", "9060", "Open", "2007-10-05", "", ""])
        ie.import_csv(self.csv_path, str(Path(self.tmp.name) / "ideaexchange.jsonl"))

    def tearDown(self):
        self.tmp.cleanup()

    def row_lines(self, md: str, title: str) -> list[str]:
        """The three printed lines of one row: title, facts, snippet (or two when there is no snippet)."""
        lines = md.splitlines()
        i = next(k for k, line in enumerate(lines) if title in line)
        out = [lines[i]]
        for line in lines[i + 1:]:
            if not line.startswith("   "):
                break
            out.append(line)
        return out

    def test_sub_status_prints_under_the_row_that_has_one(self):
        md = ppf.format_md(run_ie_search(self.cfg, query="accounts"))
        not_planned = self.row_lines(md, "Ability to Set Accounts as Inactive")
        self.assertEqual(not_planned[2], "   points: 65910; sub-status: Not Planned")
        self.assertIn("· Open", not_planned[1])  # the main status still drives the facts line
        partial = self.row_lines(md, "Dependent page layouts for accounts")
        self.assertEqual(partial[2], "   points: 125570; sub-status: Partial")
        self.assertIn("· Delivered", partial[1])

    def test_row_without_a_sub_status_prints_none(self):
        md = ppf.format_md(run_ie_search(self.cfg, query="accounts"))
        plain = self.row_lines(md, "Inline editing on accounts related lists")
        self.assertEqual(plain[2], "   points: 90380")
        self.assertNotIn("sub-status", "\n".join(plain))

    def test_sub_status_does_not_change_the_weight(self):
        scoring = self.cfg["scoring"]
        with_sub = idea("https://ideas.example.com/15", "Open idea with no date", 6683, created="", status="Open", body="points: 65910; sub-status: Not Planned")
        without = idea("https://ideas.example.com/16", "Open idea with no date too", 6683, created="", status="Open", body="points: 65910")
        rows, _ = ms.rank([with_sub, without], scoring, AS_OF, mode="search")
        scores = {r["best_url"]: r["final_score"] for r in rows}
        self.assertEqual(scores[with_sub["url"]], scores[without["url"]])

    def test_json_output_carries_the_sub_status_in_the_snippet(self):
        result = run_ie_search(self.cfg, query="accounts")
        row = next(r for r in result["rows"] if r["best_url"] == "https://ideas.example.com/15")
        self.assertEqual(row["snippet"], "points: 65910; sub-status: Not Planned")
        self.assertEqual(row["status"], "Open")

    def test_checked_in_csv_has_sub_statuses_that_print(self):
        """The real data/ideaexchange.csv (tracked) imported into the temp folder: Partial and Not Planned print."""
        ie.import_csv(ROOT / "data" / "ideaexchange.csv", str(Path(self.tmp.name) / "ideaexchange.jsonl"))
        md = ppf.format_md(run_ie_search(self.cfg, query="accounts contacts inactive"))
        lines = self.row_lines(md, "Ability to Set Accounts & Contacts as Inactive")
        self.assertIn("sub-status: Not Planned", lines[2])
        self.assertIn("· Open", lines[1])
        md = ppf.format_md(run_ie_search(self.cfg, query="dependent page layouts"))
        lines = self.row_lines(md, "Dependent page layouts")
        self.assertIn("sub-status: Partial", lines[2])
        self.assertIn("· Delivered", lines[1])


if __name__ == "__main__":
    unittest.main()
