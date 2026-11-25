"""Hacker News source, via the Algolia HN Search API. No API key needed.

Two ways in:
    search(session, cfg, query, since)   one question, returns records (used by ppf.py search)
    python collect_hn.py                 sweep mode: every config keyword, writes data/raw/hackernews.jsonl

Keeps stories with more than `more_than_points` points. When `title_only` is on (default), the
query must match the story title, not just its text. Verified 2026-11-24: the API's
restrictSearchableAttributes=title parameter works (180 hits for "salesforce" became 102, all
with the word in the title).

Docs: https://hn.algolia.com/api
"""

from __future__ import annotations

import html
import re
import time
from datetime import datetime, timedelta, timezone

import requests

from common import (
    get_json,
    load_config,
    log,
    make_record,
    now_iso,
    setup_logging,
    validate_record,
    write_jsonl,
)

SOURCE = "hackernews"


def hn_item_url(object_id: str) -> str:
    return f"https://news.ycombinator.com/item?id={object_id}"


_TAG = re.compile(r"<[^>]+>")


def clean_html(text: str) -> str:
    """Algolia returns story text as HTML. Keep the words, drop the tags."""
    text = text.replace("<p>", "\n\n").replace("<br>", "\n")
    return html.unescape(_TAG.sub("", text)).strip()


def build_params(cfg: dict, query: str, since_ts: int, page: int, title_only: bool, more_than_points: int | None = None) -> dict:
    hn = cfg["hacker_news"]
    if more_than_points is None:
        more_than_points = hn["more_than_points"]
    params = {
        "query": query,
        "tags": hn["tags"],
        "numericFilters": f"points>{more_than_points},created_at_i>={since_ts}",
        "hitsPerPage": hn["hits_per_page"],
        "page": page,
    }
    if title_only:
        params["restrictSearchableAttributes"] = "title"
    return params


def fetch_hits(session: requests.Session, cfg: dict, query: str, since_ts: int, title_only: bool,
               more_than_points: int | None = None) -> list[dict]:
    """Return all raw hits for one query, walking every page."""
    hn = cfg["hacker_news"]
    hits: list[dict] = []
    page = 0
    while True:
        params = build_params(cfg, query, since_ts, page, title_only, more_than_points)
        data, _ = get_json(session, hn["endpoint"], params=params)
        page_hits = data.get("hits", [])
        hits.extend(page_hits)
        nb_pages = data.get("nbPages", 0)
        log.info("hackernews %r page %d/%d: %d hits", query, page + 1, nb_pages, len(page_hits))
        page += 1
        if page >= nb_pages or not page_hits:
            break
        time.sleep(hn["delay_seconds"])
    return hits


def to_record(hit: dict, fetched_at: str) -> dict:
    object_id = str(hit.get("objectID", ""))
    # The record URL is the HN discussion page. The external link (if any) goes in the body
    # so nothing is lost, and the HN page is where the votes and comments live.
    external = hit.get("url") or ""
    text = clean_html(hit.get("story_text") or "")
    body_parts = [p for p in (external, text) if p]
    return make_record(
        source=SOURCE,
        url=hn_item_url(object_id),
        title=hit.get("title") or "",
        body="\n\n".join(body_parts),
        score=hit.get("points"),
        comments=hit.get("num_comments"),
        views="",  # HN does not publish view counts
        created_at=hit.get("created_at") or "",
        last_activity="",  # not provided by the search API; do not guess
        status="",  # HN stories have no status
        fetched_at=fetched_at,
    )


def hits_to_records(hits: list[dict], fetched_at: str) -> list[dict]:
    """Dedupe by story id, validate, sort best first."""
    by_id: dict[str, dict] = {}
    no_id = 0
    for hit in hits:
        object_id = hit.get("objectID")
        if not object_id:
            no_id += 1  # no id means no URL; never guess one
            continue
        by_id.setdefault(str(object_id), hit)
    if no_id:
        log.warning("hackernews: %d hits without objectID skipped", no_id)
    records = []
    bad = 0
    for hit in by_id.values():
        rec = to_record(hit, fetched_at)
        problems = validate_record(rec)
        if problems:
            bad += 1
            log.warning("skipping invalid record %s: %s", rec.get("url"), problems)
            continue
        records.append(rec)
    records.sort(key=lambda r: (-(r["score"] or 0), r["url"]))
    if bad:
        log.warning("hackernews: %d invalid hits skipped", bad)
    return records


def search(session: requests.Session, cfg: dict, query: str, since: datetime, **options) -> list[dict]:
    """One question in, validated records out. Options: title_only (bool, default from config)."""
    hn = cfg["hacker_news"]
    title_only = options.get("title_only")
    if title_only is None:
        title_only = bool(hn.get("title_only", True))
    # A single question deserves a lower bar than the topic sweep: config search_more_than_points.
    more_than = int(hn.get("search_more_than_points", hn["more_than_points"]))
    hits = fetch_hits(session, cfg, query, int(since.timestamp()), title_only, more_than)
    records = hits_to_records(hits, now_iso())
    log.info("hackernews %r: %d records (title_only=%s, points>%d, since %s)", query, len(records), title_only, more_than, since.date())
    return records


def main() -> None:
    setup_logging()
    cfg = load_config()
    hn = cfg["hacker_news"]
    since = datetime.now(timezone.utc) - timedelta(days=30.4375 * cfg["months_back"])
    queries = cfg["keywords"]

    session = requests.Session()
    fetched_at = now_iso()
    title_only = bool(hn.get("title_only", True))
    by_url: dict[str, dict] = {}
    for q in queries:
        # Sweep mode uses the topic bar (more_than_points), not the per-question one.
        hits = fetch_hits(session, cfg, q, int(since.timestamp()), title_only)
        for rec in hits_to_records(hits, fetched_at):
            by_url.setdefault(rec["url"], rec)
        time.sleep(hn["delay_seconds"])
    records = sorted(by_url.values(), key=lambda r: (-(r["score"] or 0), r["url"]))

    out = f"{cfg['paths']['raw_dir']}/hackernews.jsonl"
    n = write_jsonl(out, records)
    log.info("wrote %d records to %s (%d queries, since %s)", n, out, len(queries), since.date())


if __name__ == "__main__":
    main()
