# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Acceptance tests for issue #32 (parallel sources, --format compact, --trend, history), written by the
tester agent from the issue's "Done when" lines. No network: the four real short names hn, gh, se and rd
are pointed at fake modules for the length of each test, and the CLI's SQLite store is an in-memory one."""

import io
import json
import re
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import ppf
from common import load_config, make_record
from store import Store

FETCHED = "2026-11-27T00:00:00+00:00"
DELAY = 0.5           # seconds each fake source takes; four in a row would be 2.0 s
SLOWEST_PLUS = 1.2    # what "about the time of the slowest source" is allowed to be, with slack for a busy machine


def rec(source, n, created, score=10, **extra):
    return make_record(source=source, url=f"https://{source}.example/{n}", title=f"{source} post {n} on agentforce credits",
                       body=f"SNIPPET-{source}-{n} the body text of {source} {n}", score=score, comments=1,
                       created_at=created, fetched_at=FETCHED, **extra)


class FakeModule:
    """Stands in for a collector: SOURCE and search(session, cfg, query, since, **options)."""

    def __init__(self, name, records, delay=0.0):
        self.SOURCE, self.records, self.delay = name, records, delay
        self.calls = 0
        self.threads = []

    def search(self, session, cfg, query, since, **options):
        self.calls += 1
        self.threads.append(threading.current_thread().name)
        time.sleep(self.delay)
        return list(self.records)


class SerialExecutor:
    """A drop-in for ThreadPoolExecutor that runs each job on the calling thread, for a sequential baseline."""

    def __init__(self, max_workers=None, thread_name_prefix=""):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args, **kwargs):
        value = fn(*args, **kwargs)
        return mock.Mock(result=lambda: value)


def four_fakes(delay=0.0):
    """hn, gh, se, rd as fakes with the real source names, so UNITS and the label caps apply as in a live run."""
    return {
        "hn": FakeModule("hackernews", [rec("hackernews", 1, "2025-02-01T00:00:00Z", 40),
                                        rec("hackernews", 2, "2025-08-01T00:00:00Z", 30),
                                        rec("hackernews", 3, "2025-09-01T00:00:00Z", 20)], delay),
        "gh": FakeModule("github", [rec("github", 1, "2026-01-15T00:00:00Z", 12, status="open")], delay),
        "se": FakeModule("stackexchange", [], delay),
        "rd": FakeModule("reddit", [rec("reddit", 1, "2026-03-01T00:00:00Z", 99)], delay),
    }


class SearchHarness(unittest.TestCase):
    """Runs `ppf.main` with the fakes in place of hn, gh, se, rd and an in-memory store, capturing stdout."""

    def setUp(self):
        self.fakes = four_fakes()
        self.patches = [
            mock.patch.dict(ppf.SOURCES, self.fakes),
            mock.patch.object(ppf, "Store", lambda path: Store(":memory:")),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()

    def main(self, *argv):
        with redirect_stdout(io.StringIO()) as out:
            rc = ppf.main(list(argv))
        return rc, out.getvalue()


class ParallelSourcesAcceptance(SearchHarness):
    """Done when: `python ppf.py search "agentforce credits" --sources hn,gh,se,rd` finishes in about the time
    of the slowest source, not the sum, and the header lists the sources in the same order as before."""

    def test_search_finishes_in_about_the_time_of_the_slowest_source_not_the_sum(self):
        for f in self.fakes.values():
            f.delay = DELAY
        start = time.perf_counter()
        rc, out = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd")
        elapsed = time.perf_counter() - start
        self.assertEqual(rc, 0)
        self.assertLess(elapsed, SLOWEST_PLUS, f"took {elapsed:.2f}s; four sources of {DELAY}s each in a row would be {4 * DELAY}s")
        self.assertGreaterEqual(elapsed, DELAY, "the slowest source must still have been waited for")
        for f in self.fakes.values():
            self.assertEqual(f.calls, 1)
            self.assertNotIn("MainThread", f.threads, f"{f.SOURCE} ran on the main thread")

    def test_header_lists_the_sources_in_the_order_asked_as_before(self):
        # Reverse the delays so the source listed first finishes last: the header must not follow finish order.
        self.fakes["hn"].delay, self.fakes["gh"].delay, self.fakes["se"].delay, self.fakes["rd"].delay = 0.3, 0.2, 0.1, 0.0
        rc, out = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd")
        self.assertEqual(rc, 0)
        header = out.split("\n")[2]
        self.assertTrue(header.startswith("Sources: "), header)
        positions = [header.index(f"{name} (") for name in ("hackernews", "github", "stackexchange", "reddit")]
        self.assertEqual(positions, sorted(positions), f"header order is not hn,gh,se,rd: {header}")
        self.assertIn("hackernews (3 records, fetched)", header)
        self.assertIn("github (1 records, fetched)", header)
        self.assertIn("stackexchange (0 records, fetched)", header)
        self.assertIn("reddit (1 records, fetched)", header)

    def test_parallel_run_returns_the_same_records_and_header_as_a_sequential_run(self):
        cfg = load_config()

        def run():
            store = Store(":memory:")
            try:
                return ppf.run_search(cfg, "agentforce credits", ["hn", "gh", "se", "rd"], "24m", True, 30, None, None,
                                      store=store, session=object())
            finally:
                store.close()

        parallel = run()
        with mock.patch.object(ppf, "ThreadPoolExecutor", SerialExecutor):
            sequential = run()
        self.assertEqual(list(parallel["meta"]["per_source"]), ["hackernews", "github", "stackexchange", "reddit"])
        self.assertEqual(list(parallel["meta"]["per_source"]), list(sequential["meta"]["per_source"]))
        self.assertEqual(parallel["meta"]["per_source"], sequential["meta"]["per_source"])
        self.assertEqual(parallel["meta"]["records"], 5)
        keep = ("rank", "title", "best_url", "best_source", "sources", "raw_score", "evidence_label", "status", "snippet")
        self.assertEqual([{k: r[k] for k in keep} for r in parallel["rows"]],
                         [{k: r[k] for k in keep} for r in sequential["rows"]])


class CompactFormatAcceptance(SearchHarness):
    """Done when: `--format compact` prints one line per row with label, source, score, date, status, title
    and link, and no snippet."""

    def test_compact_prints_one_line_per_row_with_the_fields_and_no_snippet(self):
        rc, out = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd", "--format", "compact")
        self.assertEqual(rc, 0)
        lines = out.rstrip("\n").split("\n")
        self.assertIn(ppf.COMPACT_COLUMNS, lines, "no column header line")
        rows = lines[lines.index(ppf.COMPACT_COLUMNS) + 1:]
        self.assertEqual(len(rows), 5, f"expected one line per row for 5 records: {rows}")
        self.assertNotIn("SNIPPET-", out, "a body snippet leaked into compact output")
        seen_urls = set()
        for line in rows:
            fields = line.split(" | ")
            self.assertEqual(len(fields), 7, line)
            label, source, score, date, status, title, url = fields
            self.assertIn(label, ("Strong", "Moderate", "Anecdotal"), line)
            self.assertIn(source, ("hackernews", "github", "stackexchange", "reddit"), line)
            self.assertRegex(score, r"^\d+ (points|reactions|votes|upvotes)$", line)
            self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}$", line)
            self.assertTrue(status == "-" or status == "open", line)
            self.assertRegex(title, r"^\w+ post \d on agentforce credits$", line)
            self.assertRegex(url, r"^https://\w+\.example/\d$", line)
            seen_urls.add(url)
        self.assertEqual(len(seen_urls), 5)
        # The GitHub row carries its status; the others have none and print a dash.
        gh_line = next(l for l in rows if "https://github.example/1" in l)
        self.assertIn(" | open | ", gh_line)

    def test_compact_keeps_the_header_source_line_and_md_stays_the_default(self):
        rc, compact = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd", "--format", "compact")
        self.assertIn("Sources: hackernews (3 records, fetched); github (1 records, fetched)", compact)
        self.assertEqual(ppf.build_parser().parse_args(["search", "x"]).format, "md")
        self.assertEqual(ppf.build_parser().parse_args(["search", "x", "--format", "compact"]).format, "compact")
        rc, md = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd")
        self.assertIn("SNIPPET-", md, "the default md output should still carry the snippet")
        self.assertLess(len(compact), len(md))


class TrendAcceptance(SearchHarness):
    """Done when: `--trend` adds one line per source with record counts per half-year, and `--format json`
    carries them under `meta.trend`."""

    TREND_LINE = re.compile(r"^- (\w+): (\d{4}H[12] \d+)(, \d{4}H[12] \d+)*")

    def test_trend_adds_one_line_per_source_with_counts_per_half_year(self):
        rc, out = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd", "--since", "2025-01-01", "--trend")
        self.assertEqual(rc, 0)
        lines = out.split("\n")
        trend = [l for l in lines if self.TREND_LINE.match(l)]
        self.assertEqual([self.TREND_LINE.match(l).group(1) for l in trend], ["hackernews", "github", "stackexchange", "reddit"],
                         f"expected one trend line per source asked, in order: {trend}")
        by_source = {self.TREND_LINE.match(l).group(1): l for l in trend}
        self.assertIn("2025H1 1, 2025H2 2, 2026H1 0", by_source["hackernews"])
        self.assertIn("2025H1 0, 2025H2 0, 2026H1 1", by_source["github"])
        self.assertIn("2025H1 0, 2025H2 0, 2026H1 0", by_source["stackexchange"])
        self.assertIn("2025H1 0, 2025H2 0, 2026H1 1", by_source["reddit"])
        # Facts only: no judgment word on the trend lines.
        for l in trend:
            for word in ("rising", "falling", "growing", "shrinking", "up", "down", "trend is"):
                self.assertNotRegex(l.lower(), rf"\b{word}\b", l)

    def test_without_trend_no_trend_lines_are_printed(self):
        rc, out = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd", "--since", "2025-01-01")
        self.assertEqual([l for l in out.split("\n") if self.TREND_LINE.match(l)], [])
        self.assertNotIn("Trend", out)

    def test_json_carries_the_counts_under_meta_trend(self):
        rc, out = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd", "--since", "2025-01-01",
                            "--trend", "--format", "json")
        self.assertEqual(rc, 0)
        data = json.loads(out)
        trend = data["meta"]["trend"]
        self.assertEqual(list(trend), ["hackernews", "github", "stackexchange", "reddit"])
        self.assertEqual(trend["hackernews"]["2025H1"], 1)
        self.assertEqual(trend["hackernews"]["2025H2"], 2)
        self.assertEqual(trend["github"]["2026H1"], 1)
        self.assertEqual(trend["reddit"]["2026H1"], 1)
        self.assertEqual(sum(trend["stackexchange"].values()), 0)
        for counts in trend.values():
            for label, n in counts.items():
                self.assertRegex(label, r"^\d{4}H[12]$")
                self.assertIsInstance(n, int)
            self.assertEqual(list(counts)[:3], ["2025H1", "2025H2", "2026H1"], "half-years run from the window start, in order")
        # Compact prints the same lines, without the markdown dash.
        rc, compact = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd", "--since", "2025-01-01",
                                "--trend", "--format", "compact")
        self.assertTrue(any(l.startswith("hackernews: 2025H1 1, 2025H2 2, 2026H1 0") for l in compact.split("\n")), compact)
        # And without --trend, JSON has no trend key.
        rc, plain = self.main("search", "agentforce credits", "--sources", "hn,gh,se,rd", "--since", "2025-01-01", "--format", "json")
        self.assertNotIn("trend", json.loads(plain)["meta"])


class OpenStore(Store):
    """An in-memory store the command cannot close, so one seeded store serves the whole test."""

    def close(self):
        pass


class HistoryAcceptance(unittest.TestCase):
    """Done when: `python ppf.py history` lists past searches with date, query, sources, window and row count.
    The count is `records`, the per-source hits the store keeps for a run (rows after dedupe are not stored)."""

    SCOPE = "since=24m|repos=|title_only=True|site=auto|subreddits=auto|apps=auto"

    def seeded(self):
        store = OpenStore(":memory:")
        hn = [rec("hackernews", 1, "2026-01-01T00:00:00Z"), rec("hackernews", 2, "2026-01-01T00:00:00Z")]
        gh = [rec("github", 1, "2026-01-01T00:00:00Z")]
        store.upsert(hn + gh)
        store.log_query("Flow Limits", "hackernews", self.SCOPE, "2026-11-25T10:00:00+00:00", hn)
        store.log_query("Flow Limits", "github", self.SCOPE, "2026-11-25T10:00:00+00:00", gh)
        store.log_query("agentforce credits", "reddit", self.SCOPE.replace("since=24m", "since=6m"), "2026-11-26T09:00:00+00:00", [])
        return store

    def run_history(self, store, *extra):
        with mock.patch.object(ppf, "Store", lambda path: store), redirect_stdout(io.StringIO()) as out:
            rc = ppf.main(["history", *extra])
        return rc, out.getvalue().rstrip("\n").split("\n")

    def test_history_lists_past_searches_with_date_query_sources_window_and_count(self):
        store = self.seeded()
        try:
            rc, lines = self.run_history(store)
        finally:
            Store.close(store)
        self.assertEqual(rc, 0)
        self.assertEqual(lines[0], "date | query | sources | since | records", "header names the five columns")
        self.assertEqual(lines[1:], [
            "2026-11-26T09:00:00+00:00 | agentforce credits | reddit | 6m | 0",
            "2026-11-25T10:00:00+00:00 | flow limits | hackernews,github | 24m | 3",
        ])

    def test_history_limit_and_empty_store(self):
        store = self.seeded()
        try:
            rc, lines = self.run_history(store, "--limit", "1")
        finally:
            Store.close(store)
        self.assertEqual(len(lines), 2)
        self.assertIn("agentforce credits", lines[1])
        empty = OpenStore(":memory:")
        try:
            rc, lines = self.run_history(empty)
        finally:
            Store.close(empty)
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ["No searches stored yet."])

    def test_a_search_just_run_shows_up_in_history(self):
        """End to end on one store: search through the CLI, then history through the CLI."""
        store = OpenStore(":memory:")
        try:
            with mock.patch.dict(ppf.SOURCES, four_fakes()), mock.patch.object(ppf, "Store", lambda path: store), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(ppf.main(["search", "agentforce credits", "--sources", "hn,gh,se,rd", "--since", "6m"]), 0)
            rc, lines = self.run_history(store)
        finally:
            Store.close(store)
        self.assertEqual(rc, 0)
        self.assertEqual(len(lines), 2, lines)
        date, query, sources, since, records = lines[1].split(" | ")
        self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
        self.assertEqual(query, "agentforce credits")
        self.assertEqual(sources, "hackernews,github,stackexchange,reddit")
        self.assertEqual(since, "6m")
        self.assertEqual(records, "5")


if __name__ == "__main__":
    unittest.main()
