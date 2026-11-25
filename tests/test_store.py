"""Tests for the SQLite evidence store, using an in-memory database."""

import unittest
from datetime import datetime, timedelta, timezone

from common import make_record
from store import Store, query_key

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=timezone.utc)
FETCHED = NOW.isoformat()


def rec(url: str, score=10, comments="", **extra):
    return make_record(
        source="github", url=url, title=f"Issue {url[-1]}", body="body", score=score, comments=comments,
        created_at="2026-01-01T00:00:00Z", fetched_at=FETCHED, **extra,
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.close()

    def test_upsert_inserts_then_updates(self):
        self.assertEqual(self.store.upsert([rec("u/1"), rec("u/2")]), (2, 0))
        self.assertEqual(self.store.upsert([rec("u/1", score=99), rec("u/3")]), (1, 1))
        self.assertEqual(self.store.counts(), {"github": 3})

    def test_query_key_normalizes_case_and_spaces(self):
        self.assertEqual(query_key("  Flow   Limits "), "flow limits")

    def test_cache_miss_before_any_query(self):
        self.assertIsNone(self.store.cached("flow", "github", "24m", 24, NOW))

    def test_cache_hit_returns_the_same_records_with_empty_strings_restored(self):
        records = [rec("u/1", score=50, comments=""), rec("u/2", score=5, comments=3)]
        self.store.upsert(records)
        self.store.log_query("Flow  Limits", "github", "24m", FETCHED, records)
        got = self.store.cached("flow limits", "github", "24m", 24, NOW + timedelta(hours=1))
        self.assertIsNotNone(got)
        self.assertEqual([g["url"] for g in got], ["u/1", "u/2"])
        self.assertEqual(got[0]["comments"], "")
        self.assertEqual(got[0]["views"], "")
        self.assertEqual(got[1]["comments"], 3)
        self.assertEqual(got[0]["score"], 50)

    def test_cache_expires(self):
        records = [rec("u/1")]
        self.store.upsert(records)
        self.store.log_query("flow", "github", "24m", FETCHED, records)
        self.assertIsNotNone(self.store.cached("flow", "github", "24m", 24, NOW + timedelta(hours=23)))
        self.assertIsNone(self.store.cached("flow", "github", "24m", 24, NOW + timedelta(hours=25)))

    def test_cache_is_per_source_and_per_window(self):
        records = [rec("u/1")]
        self.store.upsert(records)
        self.store.log_query("flow", "github", "24m", FETCHED, records)
        self.assertIsNone(self.store.cached("flow", "hackernews", "24m", 24, NOW))
        self.assertIsNone(self.store.cached("flow", "github", "12m", 24, NOW))

    def test_latest_run_wins(self):
        first = [rec("u/1")]
        second = [rec("u/2")]
        self.store.upsert(first + second)
        self.store.log_query("flow", "github", "24m", FETCHED, first)
        self.store.log_query("flow", "github", "24m", (NOW + timedelta(minutes=5)).isoformat(), second)
        got = self.store.cached("flow", "github", "24m", 24, NOW + timedelta(hours=1))
        self.assertEqual([g["url"] for g in got], ["u/2"])


if __name__ == "__main__":
    unittest.main()
