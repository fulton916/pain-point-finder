"""Collect Hacker News stories about the topic from the Algolia HN Search API.

No API key needed. Docs: https://hn.algolia.com/api
Keeps stories with more than `more_than_points` points (default 50) in the last `months_back` months.
Writes data/raw/hackernews.jsonl in the common record format.

Run:  python collect_hn.py
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


def search(session: requests.Session, cfg: dict, query: str, since_ts: int) -> list[dict]:
    """Return all hits for one query, walking every page."""
    hn = cfg["hacker_news"]
    hits: list[dict] = []
    page = 0
    while True:
        params = {
            "query": query,
            "tags": hn["tags"],
            "numericFilters": f"points>{hn['more_than_points']},created_at_i>={since_ts}",
            "hitsPerPage": hn["hits_per_page"],
            "page": page,
        }
        data, _ = get_json(session, hn["endpoint"], params=params)
        page_hits = data.get("hits", [])
        hits.extend(page_hits)
        nb_pages = data.get("nbPages", 0)
        log.info("query %r page %d/%d: %d hits", query, page + 1, nb_pages, len(page_hits))
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


def main() -> None:
    setup_logging()
    cfg = load_config()
    hn = cfg["hacker_news"]
    since = datetime.now(timezone.utc) - timedelta(days=30.4375 * cfg["months_back"])
    since_ts = int(since.timestamp())
    fetched_at = now_iso()
    queries = cfg["keywords"]

    session = requests.Session()
    by_id: dict[str, dict] = {}
    for q in queries:
        for hit in search(session, cfg, q, since_ts):
            by_id.setdefault(str(hit.get("objectID")), hit)
        time.sleep(hn["delay_seconds"])

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

    out = f"{cfg['paths']['raw_dir']}/hackernews.jsonl"
    n = write_jsonl(out, records)
    log.info("wrote %d records to %s (%d skipped, %d queries, since %s)", n, out, bad, len(queries), since.date())


if __name__ == "__main__":
    main()
