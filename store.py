# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Local evidence store: one SQLite file that accumulates every record ever fetched.

Why: a question asked twice should not refetch, and records should pile up over time so
later work (percentiles against a real population, trend checks) has something to stand on.

Tables
    records     one row per (source, url); the 11 common fields plus first_seen
    queries     one row per search run per source: what was asked, when, how many hits
    query_hits  which records a query run returned, so a cached answer can be rebuilt

Standard library only. The file lives at paths.store in config.yaml (gitignored).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Iterable

from common import FIELDS, ROOT, log
from merge_and_score import parse_date

NUMERIC = ("score", "comments", "views")

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    source        TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL DEFAULT '',
    body          TEXT NOT NULL DEFAULT '',
    score         INTEGER,
    comments      INTEGER,
    views         INTEGER,
    created_at    TEXT NOT NULL DEFAULT '',
    last_activity TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT '',
    fetched_at    TEXT NOT NULL,
    first_seen    TEXT NOT NULL,
    PRIMARY KEY (source, url)
);
CREATE TABLE IF NOT EXISTS queries (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    query_key  TEXT NOT NULL,
    source     TEXT NOT NULL,
    scope      TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    hit_count  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS queries_lookup ON queries (query_key, source, scope, fetched_at);
CREATE TABLE IF NOT EXISTS query_hits (
    query_id INTEGER NOT NULL REFERENCES queries(id),
    source   TEXT NOT NULL,
    url      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS query_hits_by_query ON query_hits (query_id);
"""

_UPSERT_SET = ", ".join(f"{f} = :{f}" for f in FIELDS if f not in ("source", "url"))
_INSERT_COLS = ", ".join(FIELDS + ["first_seen"])
_INSERT_VALS = ", ".join(f":{f}" for f in FIELDS) + ", :fetched_at"


def query_key(text: str) -> str:
    """Lowercase, single spaces. 'Flow  Limits' and 'flow limits' are the same question."""
    return " ".join(text.lower().split())


class Store:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            full = ROOT / path
            full.parent.mkdir(parents=True, exist_ok=True)
            path = str(full)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # ---------- records ----------

    def upsert(self, records: Iterable[dict]) -> tuple[int, int]:
        """Insert new records, update existing ones (same source and url). Returns (inserted, updated)."""
        inserted = updated = 0
        for rec in records:
            row = {f: rec.get(f, "") for f in FIELDS}
            for f in NUMERIC:
                if row[f] == "":
                    row[f] = None
            exists = self.conn.execute(
                "SELECT 1 FROM records WHERE source = ? AND url = ?", (row["source"], row["url"])
            ).fetchone()
            if exists:
                self.conn.execute(f"UPDATE records SET {_UPSERT_SET} WHERE source = :source AND url = :url", row)
                updated += 1
            else:
                self.conn.execute(f"INSERT INTO records ({_INSERT_COLS}) VALUES ({_INSERT_VALS})", row)
                inserted += 1
        self.conn.commit()
        return inserted, updated

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT source, COUNT(*) AS n FROM records GROUP BY source ORDER BY source").fetchall()
        return {r["source"]: r["n"] for r in rows}

    # ---------- queries and cache ----------

    def log_query(self, query: str, source: str, scope: str, fetched_at: str, records: list[dict]) -> int:
        """`scope` is everything besides the words that changes the answer: the time window plus
        per-source options such as a repo list. Two runs share a cache only when it matches."""
        cur = self.conn.execute(
            "INSERT INTO queries (query_key, source, scope, fetched_at, hit_count) VALUES (?, ?, ?, ?, ?)",
            (query_key(query), source, scope, fetched_at, len(records)),
        )
        qid = cur.lastrowid
        self.conn.executemany(
            "INSERT INTO query_hits (query_id, source, url) VALUES (?, ?, ?)",
            [(qid, r["source"], r["url"]) for r in records],
        )
        self.conn.commit()
        return qid

    def cached(self, query: str, source: str, scope: str, max_age_hours: float, now: datetime) -> list[dict] | None:
        """Records from the latest run of this exact question and scope, if fresh enough. None otherwise."""
        row = self.conn.execute(
            "SELECT id, fetched_at FROM queries WHERE query_key = ? AND source = ? AND scope = ? "
            "ORDER BY fetched_at DESC, id DESC LIMIT 1",
            (query_key(query), source, scope),
        ).fetchone()
        if row is None:
            return None
        fetched = parse_date(row["fetched_at"])
        if fetched is None or now - fetched > timedelta(hours=max_age_hours):
            return None
        hits = self.conn.execute(
            "SELECT r.* FROM query_hits h JOIN records r ON r.source = h.source AND r.url = h.url "
            "WHERE h.query_id = ? ORDER BY r.score DESC, r.url",
            (row["id"],),
        ).fetchall()
        log.info("%s: %d records from cache (fetched %s)", source, len(hits), row["fetched_at"])
        return [_to_record(h) for h in hits]


def _to_record(row: sqlite3.Row) -> dict:
    rec = {f: row[f] for f in FIELDS}
    for f in FIELDS:
        if rec[f] is None:
            rec[f] = ""
    return rec
