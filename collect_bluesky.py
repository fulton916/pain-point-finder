# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Bluesky source, via the public Bluesky AppView API (app.bsky.feed.searchPosts). No key.

Two ways in:
    search(session, cfg, query, since)   one question, returns records (used by ppf.py search)
    python collect_bluesky.py            sweep mode: every config keyword, writes data/raw/bluesky.jsonl

What the API can and cannot do (lexicon app.bsky.feed.searchPosts and the live service, checked 2026-11-26):
  * GET <endpoint>?q=<words>&sort=top&limit=100&since=<ISO datetime>&cursor=<token>. `q` is the only
    required parameter; `sort` is `top` or `latest` (default latest); `since` is inclusive and may be a
    datetime or a YYYY-MM-DD date; `limit` is 1 to 100 (default 25); the answer carries `posts`, a
    `cursor` for the next page when there is one, and `hitsTotal`.
  * The documented host for unauthenticated reads is https://public.api.bsky.app (a cached copy of
    https://api.bsky.app). On 2026-11-26 the public host answered every request from this machine with
    an HTML 403 from its CDN, with any User-Agent, while api.bsky.app answered normally. So the first
    page is asked of `bluesky.endpoint` once, and when that host refuses (403, 429 or a connection
    failure) the run moves to `bluesky.fallback_endpoint` and says so in the header. A refused host is
    not tried again in the same process.
  * Rate limits: the docs publish no number for these hosts ("generous rate-limits", HTTP 429 when
    crossed, contact them if it happens). The tool waits `delay_seconds` between pages and common.get_json
    backs off on 429. Bluesky's Developer Guidelines forbid automated interactions (likes, follows,
    replies); this collector only reads.
  * The lexicon warns that the cursor "may not necessarily allow scrolling through entire result set",
    and on 2026-11-26 api.bsky.app answered every unauthenticated request that carried a cursor with
    HTTP 403 "Request forbidden by administrative rules", whatever the sort or `since` form. So pages
    after the first are best effort: one try each, and the first refusal ends paging with a header
    sentence saying how many posts were kept. Paging also stops at `max_pages`, when no cursor comes
    back, or when a page is empty. In practice one question gets the top 100 posts.

Record mapping: title = the first `title_chars` (120) characters of the post text on one line, body = the
full text, score = likeCount, comments = replyCount, created_at = record.createdAt, url =
https://bsky.app/profile/<author.handle>/post/<rkey> where rkey is the last part of the post `uri`
(at://<did>/app.bsky.feed.post/<rkey>). last_activity, views and status are empty: the API has no
activity date, view count or state. A post with no text (image only) has no title and is skipped.
Likes measure how far a short post spread, not how many people share a problem, so config caps Bluesky
at Moderate (scoring.label_caps.bluesky).
"""

from __future__ import annotations

import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from common import (
    RateLimited,
    get_json,
    load_config,
    log,
    make_record,
    now_iso,
    setup_logging,
    validate_record,
    write_jsonl,
)

SOURCE = "bluesky"
ATTRIBUTION = (
    "Bluesky rows come from the Bluesky public API (app.bsky.feed.searchPosts). Posts belong to their "
    "authors; likes count how far a short post spread, not how many people share the problem. Each row "
    "links to the original post on bsky.app."
)
POST_URL = "https://bsky.app/profile/{actor}/post/{rkey}"
# Endpoints that refused in this process (403, 429 or no connection), so sweep mode does not probe the
# public host once per keyword. Tests clear it.
REFUSED: set[str] = set()


def since_param(since: datetime | None) -> str:
    """The `since` query value: an ISO 8601 UTC datetime to the second with a Z, as the lexicon allows.
    Empty when no cut-off was given. A naive datetime is taken as UTC."""
    if since is None:
        return ""
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return since.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_params(cfg: dict, query: str, since: datetime | None, cursor: str = "") -> dict:
    """Parameters for one searchPosts call: the words, best first, at most `limit` posts, the cut-off
    and the paging cursor when there is one."""
    bs = cfg["bluesky"]
    params: dict = {
        "q": " ".join(query.split()),
        "sort": str(bs.get("sort", "top")),
        "limit": max(1, min(int(bs.get("limit", 100)), 100)),
    }
    value = since_param(since)
    if value:
        params["since"] = value
    if cursor:
        params["cursor"] = cursor
    return params


def rkey_of(uri: str) -> str:
    """The record key: the last path segment of at://<did>/app.bsky.feed.post/<rkey>. Empty when the
    uri is not shaped like that."""
    parts = [p for p in (uri or "").strip().split("/") if p]
    if len(parts) < 4 or parts[0].lower() != "at:" or parts[2] != "app.bsky.feed.post":
        return ""
    return parts[3]


def post_url(post: dict) -> str:
    """https://bsky.app/profile/<handle>/post/<rkey>. The author's DID stands in when the handle is
    missing (bsky.app resolves both). Empty when the uri has no record key."""
    rkey = rkey_of(post.get("uri") or "")
    author = post.get("author") or {}
    actor = (author.get("handle") or author.get("did") or "").strip()
    if not rkey or not actor:
        return ""
    return POST_URL.format(actor=actor, rkey=rkey)


def to_record(post: dict, fetched_at: str, body_max_chars: int, min_likes: int = 0,
              title_chars: int = 120) -> dict | None:
    """One post view to the 11-field record. None when the post has no text, or fewer likes than
    min_likes."""
    record = post.get("record") or {}
    text = str(record.get("text") or "").strip()
    if not text:
        return None
    likes = post.get("likeCount")
    if likes is not None and min_likes and int(likes) < min_likes:
        return None
    return make_record(
        source=SOURCE,
        url=post_url(post),
        title=" ".join(text.split())[:title_chars],
        body=text[:body_max_chars],
        score=likes,
        comments=post.get("replyCount"),
        views="",
        created_at=str(record.get("createdAt") or ""),
        last_activity="",  # the API has no activity date; do not guess
        status="",
        fetched_at=fetched_at,
    )


def posts_to_records(posts: list[dict], fetched_at: str, body_max_chars: int, min_likes: int = 0,
                     title_chars: int = 120) -> list[dict]:
    """Validate, dedupe by URL, sort most liked first."""
    records = []
    bad = 0
    skipped = 0
    for post in posts:
        rec = to_record(post, fetched_at, body_max_chars, min_likes, title_chars)
        if rec is None:
            skipped += 1
            continue
        problems = validate_record(rec)
        if problems:
            bad += 1
            log.warning("skipping invalid record %s: %s", rec.get("url") or post.get("uri"), problems)
            continue
        records.append(rec)
    by_url = {r["url"]: r for r in records}
    if skipped:
        log.info("bluesky: %d posts skipped (no text, or under the like floor)", skipped)
    if bad:
        log.warning("bluesky: %d invalid posts skipped", bad)
    return sorted(by_url.values(), key=lambda r: (-(r["score"] if r["score"] != "" else float("-inf")), r["url"]))


def host_of(url: str) -> str:
    return url.split("//", 1)[-1].split("/", 1)[0]


_STATUS = re.compile(r"\b(\d{3})\b")


def refusal(e: Exception) -> str:
    """A short, printable reason for a failed request: 'HTTP 403' rather than the HTML page the CDN sent
    (common.get_json keeps the first 200 characters of the body in its message), or 'no answer'."""
    if isinstance(e, RateLimited):
        m = _STATUS.search(str(e))
        return f"HTTP {m.group(1)}" if m else "a rate limit"
    if isinstance(e, requests.HTTPError):
        resp = getattr(e, "response", None)
        return f"HTTP {resp.status_code}" if resp is not None else "an HTTP error"
    return "no answer"


def first_page(session: requests.Session, cfg: dict, params: dict, info: list[str] | None = None) -> tuple[dict, str]:
    """The first searchPosts page from the configured endpoint, or from the fallback when it refuses.
    Returns (data, endpoint used). The endpoint is probed once (one try, no backoff): a 403 or 429, or a
    failed connection, moves the run to `fallback_endpoint`, which gets the normal retries, and a header
    sentence says so. When there is no fallback the first error is raised as usual."""
    bs = cfg["bluesky"]
    endpoint = str(bs["endpoint"])
    fallback = str(bs.get("fallback_endpoint") or "")
    if not fallback:
        data, _ = get_json(session, endpoint, params=params)
        return data, endpoint
    if endpoint not in REFUSED:
        try:
            data, _ = get_json(session, endpoint, params=params, max_tries=1)
            return data, endpoint
        except (RateLimited, RuntimeError) as e:
            REFUSED.add(endpoint)
            why = f"bluesky searched {host_of(fallback)} ({host_of(endpoint)} refused with {refusal(e)})."
            log.warning("%s", why)
            if info is not None:
                info.append(why)
    data, _ = get_json(session, fallback, params=params)
    return data, fallback


def fetch_posts(session: requests.Session, cfg: dict, query: str, since: datetime | None, max_pages: int,
                info: list[str] | None = None) -> list[dict]:
    """Up to max_pages pages of posts, best first, `delay_seconds` apart. The first page gets the host
    fallback and the normal retries. Later pages are best effort: one try each on the host that answered
    the first page, and the first refusal ends paging with a sentence in `info` (the header) saying how
    many posts were kept. Paging also stops when a page has no cursor or no posts."""
    bs = cfg["bluesky"]
    posts: list[dict] = []
    cursor = ""
    endpoint = ""
    for page in range(1, max(1, int(max_pages)) + 1):
        params = build_params(cfg, query, since, cursor)
        if page == 1:
            data, endpoint = first_page(session, cfg, params, info)
        else:
            time.sleep(float(bs.get("delay_seconds", 1.0)))
            try:
                data, _ = get_json(session, endpoint, params=params, max_tries=1)
            except (RateLimited, RuntimeError, requests.HTTPError) as e:
                why = (f"bluesky kept page 1 only ({len(posts)} posts): {host_of(endpoint)} refused page {page} "
                       f"with {refusal(e)}.")
                log.warning("%s", why)
                if info is not None:
                    info.append(why)
                break
        batch = (data or {}).get("posts") or []
        posts.extend(p for p in batch if isinstance(p, dict))
        log.info("bluesky %r page %d: %d posts (%s)", query, page, len(batch), host_of(endpoint))
        cursor = str((data or {}).get("cursor") or "")
        if not cursor or not batch:
            break
    return posts


def search(session: requests.Session, cfg: dict, query: str, since: datetime, **options) -> list[dict]:
    """One question in, validated records out. Options: info (list[str]): when given, a sentence is
    appended when the configured host refused and the fallback host answered, so ppf.py can print it in
    the header and meta.note. Other options are ignored (they belong to other sources)."""
    bs = cfg["bluesky"]
    if not " ".join(query.split()):
        raise ValueError("bluesky: the question has no words")
    posts = fetch_posts(session, cfg, query, since, int(bs.get("max_pages", 3)), options.get("info"))
    records = posts_to_records(posts, now_iso(), int(bs.get("body_max_chars", 2000)),
                               int(bs.get("search_min_likes", 0)), int(bs.get("title_chars", 120)))
    log.info("bluesky %r: %d records (since %s)", query, len(records), since.date() if since else "any date")
    return records


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    cfg = load_config()
    bs = cfg["bluesky"]
    since = datetime.now(timezone.utc) - timedelta(days=30.4375 * cfg["months_back"])
    session = requests.Session()
    fetched_at = now_iso()
    posts: list[dict] = []
    # Sweep: every topic keyword, best posts first.
    for i, keyword in enumerate(cfg["keywords"]):
        if i:
            time.sleep(float(bs.get("delay_seconds", 1.0)))
        try:
            posts.extend(fetch_posts(session, cfg, keyword, since, int(bs.get("max_pages", 3))))
        except (requests.HTTPError, RateLimited, RuntimeError) as e:
            log.error("bluesky %r: %s", keyword, e)
    records = posts_to_records(posts, fetched_at, int(bs.get("body_max_chars", 2000)),
                               int(bs.get("min_likes", 0)), int(bs.get("title_chars", 120)))

    out = f"{cfg['paths']['raw_dir']}/bluesky.jsonl"
    n = write_jsonl(out, records)
    log.info("wrote %d records to %s (since %s)", n, out, since.date())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
