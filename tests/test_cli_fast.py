# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Issue #32: sources fetched side by side, --format compact, --trend and the history command. No network."""

import io
import threading
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import ppf
from common import load_config, make_record
from store import Store

FETCHED = "2026-11-27T00:00:00+00:00"


def rec(source, n, created, score=10, **extra):
    return make_record(source=source, url=f"https://{source}.example/{n}", title=f"{source} post {n} about flow limits",
                       body=f"Body of {source} {n}", score=score, comments=1, created_at=created, fetched_at=FETCHED, **extra)


class SlowSource:
    """A collector that takes `delay` seconds and remembers which thread ran it."""

    def __init__(self, name, records, delay=0.0, fail=False):
        self.SOURCE, self.records, self.delay, self.fail = name, records, delay, fail
        self.threads: list[str] = []
        self.store_seen = []

    def search(self, session, cfg, query, since, **options):
        self.threads.append(threading.current_thread().name)
        self.store_seen.append(options.get("store"))
        time.sleep(self.delay)
        if self.fail:
            raise RuntimeError(f"{self.SOURCE} is down")
        if options.get("info") is not None:
            options["info"].append(f"{self.SOURCE} searched somewhere.")
        return list(self.records)


class ParallelSourcesTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()
        self.store = Store(":memory:")
        self.slow = SlowSource("slowsrc", [rec("slowsrc", 1, "2026-03-01T00:00:00Z", 50)], delay=0.6)
        self.quick = SlowSource("quicksrc", [rec("quicksrc", 1, "2026-05-01T00:00:00Z", 5),
                                             rec("quicksrc", 2, "2025-09-01T00:00:00Z", 7)], delay=0.6)
        for short, mod in (("slow", self.slow), ("quick", self.quick)):
            ppf.SOURCES[short] = mod
            ppf.UNITS[mod.SOURCE] = "votes"

    def tearDown(self):
        for short, name in (("slow", "slowsrc"), ("quick", "quicksrc")):
            ppf.SOURCES.pop(short, None)
            ppf.UNITS.pop(name, None)
        self.store.close()

    def run_it(self, sources=("slow", "quick"), **kw):
        args = dict(cfg=self.cfg, query="flow limits", sources=list(sources), since_text="24m", refresh=False,
                    limit=10, repos=None, title_only=None, store=self.store, session=object())
        args.update(kw)
        return ppf.run_search(**args)

    def test_two_slow_sources_take_about_the_time_of_one(self):
        start = time.perf_counter()
        result = self.run_it()
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 1.0, f"took {elapsed:.2f}s: the sources ran one after the other")
        self.assertEqual(result["meta"]["records"], 3)
        self.assertNotEqual(self.slow.threads, ["MainThread"])
        self.assertNotEqual(self.quick.threads, ["MainThread"])

    def test_header_keeps_the_fixed_source_order_and_records_match_a_sequential_run(self):
        result = self.run_it()
        self.assertEqual(list(result["meta"]["per_source"]), ["slowsrc", "quicksrc"])
        self.assertEqual(result["meta"]["sources"], ["slowsrc", "quicksrc"])
        urls = sorted(r["best_url"] for r in result["rows"])
        self.assertEqual(urls, ["https://quicksrc.example/1", "https://quicksrc.example/2", "https://slowsrc.example/1"])
        # The notes come through from the worker, in source order.
        self.assertEqual(result["meta"]["per_source"]["slowsrc"]["note"], "slowsrc searched somewhere.")
        self.assertTrue(result["meta"]["note"].startswith("slowsrc searched somewhere. quicksrc searched somewhere."))
        # The quick source finished first; the header still lists the slow one first.
        md = ppf.format_md(result)
        self.assertLess(md.index("slowsrc (1 records, fetched"), md.index("quicksrc (2 records, fetched"))

    def test_store_is_written_from_the_main_thread_only_and_holds_every_record(self):
        self.run_it()
        self.assertEqual(self.store.counts(), {"quicksrc": 2, "slowsrc": 1})
        # Workers never see the store: a SQLite connection belongs to the thread that opened it.
        self.assertEqual(self.slow.store_seen, [None])
        self.assertEqual(self.quick.store_seen, [None])

    def test_one_failing_source_does_not_kill_the_others(self):
        self.slow.fail = True
        result = self.run_it()
        self.assertEqual(result["meta"]["per_source"]["slowsrc"], {"records": 0, "from_cache": False, "error": "slowsrc is down"})
        self.assertEqual(result["meta"]["per_source"]["quicksrc"]["records"], 2)
        self.assertEqual(len(result["rows"]), 2)
        self.assertIn("slowsrc (failed: slowsrc is down)", ppf.format_md(result))

    def test_cached_source_sends_no_worker_and_the_rest_still_fetch(self):
        self.run_it()
        self.quick.records.append(rec("quicksrc", 3, "2026-06-01T00:00:00Z"))
        self.slow.fail = True  # would fail if asked again
        second = self.run_it(query="Flow  LIMITS")
        self.assertTrue(second["meta"]["per_source"]["slowsrc"]["from_cache"])
        self.assertTrue(second["meta"]["per_source"]["quicksrc"]["from_cache"])
        self.assertEqual(len(self.slow.threads), 1)

    def test_a_source_purge_hook_runs_in_the_main_thread_with_the_store(self):
        seen = []
        self.quick.purge_old_rows = lambda store, cfg, now: seen.append((threading.current_thread().name, store))
        self.run_it()
        self.assertEqual(seen, [("MainThread", self.store)])


class CompactFormatTests(unittest.TestCase):
    def result(self):
        return {
            "meta": {
                "query": "flow limits", "sources": ["hackernews", "github"], "since": "24m", "since_date": "2024-11-27",
                "generated": FETCHED, "requests_sent": 3,
                "per_source": {"hackernews": {"records": 2, "from_cache": False}, "github": {"records": 5, "from_cache": True}},
                "records": 7, "rows": 6, "shown": 2, "merges": 1, "dropped": 0, "store_totals": {},
                "note": "note text", "attribution": ["Credit line."],
            },
            "rows": [
                {"rank": 1, "title": "Flow | limits [beta]", "best_url": "https://github.com/a/b/issues/1", "best_source": "github",
                 "source_count": 2, "sources": "github;hackernews", "top_normalized_score": 1.0, "raw_score": 42, "comments": 7,
                 "latest_activity": "2026-03-04", "status": "closed", "evidence_label": "Strong", "final_score": 1.2,
                 "snippet": "Something went wrong in the snippet..."},
                {"rank": 2, "title": "No numbers here", "best_url": "https://news.ycombinator.com/item?id=2", "best_source": "hackernews",
                 "source_count": 1, "sources": "hackernews", "top_normalized_score": 0.5, "raw_score": "", "comments": "",
                 "latest_activity": "", "status": "", "evidence_label": "Anecdotal", "final_score": 0.3, "snippet": "Another snippet"},
            ],
        }

    def test_one_line_per_row_with_the_seven_columns_and_no_snippet(self):
        text = ppf.format_compact(self.result())
        lines = text.split("\n")
        self.assertIn("Sources: hackernews (2 records, fetched); github (5 records, cache). Since 2024-11-27 (24m).", lines[1])
        self.assertIn("note text", lines)
        self.assertIn("Credit line.", lines)
        self.assertIn(ppf.COMPACT_COLUMNS, lines)
        rows = lines[lines.index(ppf.COMPACT_COLUMNS) + 1:]
        self.assertEqual(rows, [
            "Strong | github + hackernews | 42 reactions | 2026-03-04 | closed | Flow / limits [beta] | https://github.com/a/b/issues/1",
            "Anecdotal | hackernews | - | - | - | No numbers here | https://news.ycombinator.com/item?id=2",
        ])
        self.assertNotIn("snippet", text)
        self.assertNotIn("Something went wrong", text)
        for row in rows:
            self.assertEqual(row.count(" | "), 6)

    def test_compact_with_no_rows(self):
        r = self.result()
        r["rows"] = []
        self.assertTrue(ppf.format_compact(r).endswith("No results."))

    def test_compact_is_a_format_choice_and_md_stays_the_default(self):
        parser = ppf.build_parser()
        self.assertEqual(parser.parse_args(["search", "x"]).format, "md")
        self.assertEqual(parser.parse_args(["search", "x", "--format", "compact"]).format, "compact")
        fake = {"meta": {"per_source": {}}, "rows": []}
        with mock.patch.object(ppf, "run_search", return_value=fake), \
             mock.patch.object(ppf, "format_compact", return_value="compact!") as fmt, redirect_stdout(io.StringIO()) as out:
            ppf.main(["search", "x", "--format", "compact"])
        self.assertTrue(fmt.called)
        self.assertEqual(out.getvalue(), "compact!\n")


class TrendTests(unittest.TestCase):
    SINCE = datetime(2024, 11, 27, tzinfo=timezone.utc)
    NOW = datetime(2026, 11, 27, tzinfo=timezone.utc)

    def test_half_year_labels_cover_the_window(self):
        self.assertEqual(ppf.half_year(datetime(2025, 6, 30, tzinfo=timezone.utc)), "2025H1")
        self.assertEqual(ppf.half_year(datetime(2025, 7, 1, tzinfo=timezone.utc)), "2025H2")
        self.assertEqual(ppf.half_years(self.SINCE, self.NOW), ["2024H2", "2025H1", "2025H2", "2026H1", "2026H2"])
        self.assertEqual(ppf.half_years(datetime(2026, 2, 1, tzinfo=timezone.utc), datetime(2026, 3, 1, tzinfo=timezone.utc)), ["2026H1"])

    def test_buckets_count_records_per_source_by_posting_date_inside_the_window(self):
        records = [
            rec("reddit", 1, "2024-12-01T00:00:00Z"), rec("reddit", 2, "2025-02-01T00:00:00Z"), rec("reddit", 3, "2025-03-01T00:00:00Z"),
            rec("reddit", 4, "2026-10-01T00:00:00Z"),
            rec("reddit", 5, "2024-08-01T00:00:00Z"),           # same half-year as `since`, but before it: outside
            rec("github", 1, "", last_activity="2026-01-15"),  # no posting date: last activity counts
            rec("github", 2, ""),                              # no date at all
        ]
        buckets, outside = ppf.trend_buckets(records, ["reddit", "github", "hackernews"], self.SINCE, self.NOW)
        self.assertEqual(buckets["reddit"], {"2024H2": 1, "2025H1": 2, "2025H2": 0, "2026H1": 0, "2026H2": 1})
        self.assertEqual(buckets["github"], {"2024H2": 0, "2025H1": 0, "2025H2": 0, "2026H1": 1, "2026H2": 0})
        self.assertEqual(buckets["hackernews"], {"2024H2": 0, "2025H1": 0, "2025H2": 0, "2026H1": 0, "2026H2": 0})
        self.assertEqual(outside, {"reddit": 1, "github": 1, "hackernews": 0})
        meta = {"trend": buckets, "trend_outside": outside}
        self.assertEqual(ppf.trend_lines(meta), [
            "reddit: 2024H2 1, 2025H1 2, 2025H2 0, 2026H1 0, 2026H2 1, outside the window or undated 1",
            "github: 2024H2 0, 2025H1 0, 2025H2 0, 2026H1 1, 2026H2 0, outside the window or undated 1",
            "hackernews: 2024H2 0, 2025H1 0, 2025H2 0, 2026H1 0, 2026H2 0",
        ])

    def test_run_search_adds_meta_trend_only_when_asked_and_every_format_prints_it(self):
        cfg = load_config()
        store = Store(":memory:")
        src = SlowSource("trendsrc", [rec("trendsrc", 1, "2026-02-01T00:00:00Z"), rec("trendsrc", 2, "2026-03-01T00:00:00Z")])
        ppf.SOURCES["trend"] = src
        ppf.UNITS["trendsrc"] = "votes"
        try:
            plain = ppf.run_search(cfg, "flow", ["trend"], "12m", False, 10, None, None, store=store, session=object())
            self.assertNotIn("trend", plain["meta"])
            self.assertNotIn("Trend", ppf.format_md(plain))
            result = ppf.run_search(cfg, "flow", ["trend"], "12m", False, 10, None, None, store=store, session=object(), trend=True)
        finally:
            ppf.SOURCES.pop("trend"); ppf.UNITS.pop("trendsrc"); store.close()
        self.assertTrue(result["meta"]["per_source"]["trendsrc"]["from_cache"])  # trend works on cached records too
        counts = result["meta"]["trend"]["trendsrc"]
        self.assertEqual(sum(counts.values()), 2)
        self.assertEqual(counts["2026H1"], 2)
        line = ppf.trend_lines(result["meta"])[0]
        self.assertTrue(line.startswith("trendsrc: "))
        self.assertIn("2026H1 2", line)
        self.assertIn(line, ppf.format_md(result))
        self.assertIn(ppf.TREND_HEADING, ppf.format_md(result))
        self.assertIn(line, ppf.format_compact(result).split("\n"))
        self.assertIn('"trend": {', ppf.format_json(result))

    def test_trend_flag_reaches_run_search(self):
        fake = {"meta": {"per_source": {}}, "rows": []}
        with mock.patch.object(ppf, "run_search", return_value=fake) as run, mock.patch.object(ppf, "format_md", return_value="x"), \
             redirect_stdout(io.StringIO()):
            ppf.main(["search", "x", "--trend"])
            ppf.main(["search", "x"])
        self.assertTrue(run.call_args_list[0].kwargs["trend"])
        self.assertFalse(run.call_args_list[1].kwargs["trend"])


class HistoryTests(unittest.TestCase):
    def seed(self, store):
        scope = "since=24m|repos=|title_only=True|site=auto|subreddits=auto|apps=auto"
        a = [rec("hackernews", 1, "2026-01-01T00:00:00Z"), rec("hackernews", 2, "2026-01-01T00:00:00Z")]
        b = [rec("github", 1, "2026-01-01T00:00:00Z")]
        store.upsert(a + b)
        store.log_query("Flow Limits", "hackernews", scope, "2026-11-25T10:00:00+00:00", a)
        store.log_query("Flow Limits", "github", scope, "2026-11-25T10:00:00+00:00", b)
        store.log_query("agentforce credits", "reddit", scope.replace("since=24m", "since=6m"), "2026-11-26T09:00:00+00:00", [])
        store.log_query("flow limits", "hackernews", scope, "2026-11-27T08:00:00+00:00", a)

    def test_history_groups_a_run_by_time_question_and_scope_newest_first(self):
        store = Store(":memory:")
        try:
            self.seed(store)
            runs = store.history()
        finally:
            store.close()
        self.assertEqual([(r["fetched_at"], r["query"], r["sources"], r["since"], r["records"]) for r in runs], [
            ("2026-11-27T08:00:00+00:00", "flow limits", ["hackernews"], "24m", 2),
            ("2026-11-26T09:00:00+00:00", "agentforce credits", ["reddit"], "6m", 0),
            ("2026-11-25T10:00:00+00:00", "flow limits", ["hackernews", "github"], "24m", 3),
        ])

    def test_limit_counts_runs_not_rows(self):
        store = Store(":memory:")
        try:
            self.seed(store)
            self.assertEqual(len(store.history(limit=2)), 2)
            self.assertEqual(store.history(limit=1)[0]["query"], "flow limits")
            self.assertEqual(Store(":memory:").history(), [])
        finally:
            store.close()

    def test_old_database_without_new_columns_still_works(self):
        """history() reads only columns the queries table has had since the store was made."""
        store = Store(":memory:")
        try:
            cols = [r["name"] for r in store.conn.execute("PRAGMA table_info(queries)")]
            self.assertEqual(cols, ["id", "query_key", "source", "scope", "fetched_at", "hit_count"])
        finally:
            store.close()

    def test_format_history_and_the_command(self):
        runs = [{"fetched_at": "2026-11-27T08:00:00+00:00", "query": "flow limits", "scope": "s", "since": "24m",
                 "sources": ["hackernews", "github"], "records": 3}]
        text = ppf.format_history(runs)
        self.assertEqual(text.split("\n"), ["date | query | sources | since | records",
                                            "2026-11-27T08:00:00+00:00 | flow limits | hackernews,github | 24m | 3"])
        self.assertEqual(ppf.format_history([]), "No searches stored yet.")
        parser = ppf.build_parser()
        self.assertEqual(parser.parse_args(["history"]).limit, 20)
        self.assertEqual(parser.parse_args(["history", "--limit", "5"]).limit, 5)
        with mock.patch.object(ppf.Store, "history", return_value=runs) as hist, \
             mock.patch.object(ppf.Store, "__init__", return_value=None), mock.patch.object(ppf.Store, "close"), \
             redirect_stdout(io.StringIO()) as out:
            rc = ppf.main(["history", "--limit", "5"])
        self.assertEqual(rc, 0)
        hist.assert_called_once_with(5)
        self.assertIn("flow limits | hackernews,github | 24m | 3", out.getvalue())


if __name__ == "__main__":
    unittest.main()
