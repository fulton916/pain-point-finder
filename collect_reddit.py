# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Reddit source, via the Arctic Shift archive API (https://arctic-shift.photon-reddit.com). No key.

Two ways in:
    search(session, cfg, query, since)   one question, returns records (used by ppf.py search)
    python collect_reddit.py             sweep mode: every config keyword in every configured
                                         subreddit since months_back, writes data/raw/reddit.jsonl

Why an archive and not Reddit itself: Reddit stopped taking new Data API access requests on
2026-10-31 and closes the public Data API in March 2027 (issue #9). Arctic Shift is a third-party
archive run by one person (github.com/ArthurHeitmann/arctic_shift). It publishes no terms of use
and promises no uptime; rate limits are dynamic ("a couple requests per second" is fine). Treat
it as research-grade data that may stop working after March 2027.

What the API can and cannot do (checked live 2026-11-25):
  * Keyword search (`query` over title and selftext) only works together with a `subreddit`, so a
    question is asked once per configured subreddit. There is no all-of-Reddit keyword search.
  * `fields` must be sent, or the server answers 422 {"error": "Timeout. Maybe slow down a bit"}.
    The same answer comes back, within a second, when requests arrive too fast: one request of
    100 posts with text uses up a budget that refills over tens of seconds. So it is a throttle,
    whatever it calls itself. The collector waits `delay_seconds` between subreddits and, on a
    422, pauses `timeout_pause_seconds` (doubling each time) before retrying, then skips that
    subreddit with a warning. The source only fails when every subreddit was refused.
  * Results are sorted by date only (newest first with sort=desc), at most 100 per request, so a
    popular older post can be cut off when a subreddit has more than `limit` matches since `since`.
  * `score` and `num_comments` are what the archiver saw. `retrieved_on` is usually seconds after
    `created_utc`, so most scores are the score at posting time, not today's. Records keep them
    as-is; config caps Reddit at Moderate because of this.

Record mapping: score = score, comments = num_comments, url = the post's permalink built from the
subreddit and id (the archive's `url` field is the link target, which for a link post is an
external site; that link is kept at the top of the body). created_at = created_utc. last_activity,
views and status are empty: the archive has no activity date, view count or state.
"""

from __future__ import annotations

import html
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

SOURCE = "reddit"
ATTRIBUTION = (
    "Reddit rows come from the Arctic Shift archive (https://github.com/ArthurHeitmann/arctic_shift), "
    "not from Reddit's API. Posts belong to their Reddit authors; scores are as archived, usually at "
    "posting time, not current. Each row links to the original post."
)
# Fields asked for on every request. The server times out without a fields list.
FIELDS = ["id", "subreddit", "title", "selftext", "url", "score", "num_comments", "created_utc", "retrieved_on"]
# The ones to_record cannot do without; always requested even if config.yaml trims the list.
REQUIRED_FIELDS = ["id", "subreddit", "title", "score", "num_comments", "created_utc"]
# Bodies Reddit replaced; they carry no words, so the record body is left empty.
EMPTY_BODIES = {"[removed]", "[deleted]", ""}


def permalink(post: dict) -> str:
    """https://www.reddit.com/r/<subreddit>/comments/<id>/ . Empty when either part is missing."""
    sub = (post.get("subreddit") or "").strip()
    pid = (post.get("id") or "").strip()
    if pid.startswith("t3_"):
        pid = pid[3:]
    if not sub or not pid:
        return ""
    return f"https://www.reddit.com/r/{sub}/comments/{pid}/"


def is_self_link(url: str, link: str) -> bool:
    """True when the archive's url is this post itself (a text post). A link to another Reddit
    thread is an external link like any other and is kept in the body."""
    u = (url or "").rstrip("/").lower()
    own = link.rstrip("/").lower()
    return not u or bool(own) and u.startswith(own)


def build_params(cfg: dict, query: str, since: datetime | None, subreddit: str, limit: int) -> dict:
    """Parameters for one /api/posts/search call: one subreddit, the words, newest first."""
    rd = cfg["reddit"]
    params: dict = {
        "subreddit": subreddit.removeprefix("r/"),
        "limit": max(1, min(int(limit), 100)),
        "sort": "desc",
        "fields": ",".join(dict.fromkeys(REQUIRED_FIELDS + list(rd.get("fields") or FIELDS))),
    }
    words = " ".join(query.split())
    if words:
        params["query"] = words
    if since is not None:
        params["after"] = int(since.timestamp())
    return params


def timeout_error(e: requests.HTTPError) -> bool:
    """The archive answers 422 with {"error": "Timeout. Maybe slow down a bit"} when it wants us to
    wait (it arrives in under a second, so it is a throttle). Worth a retry; other 4xx are our mistake."""
    resp = getattr(e, "response", None)
    if resp is None or resp.status_code != 422:
        return False
    try:
        return "timeout" in str(resp.json().get("error", "")).lower()
    except (ValueError, AttributeError):
        return False


def fetch_subreddit(session: requests.Session, cfg: dict, query: str, since: datetime | None,
                    subreddit: str, limit: int) -> list[dict] | None:
    """Posts from one subreddit, or None when the archive kept timing out on it."""
    rd = cfg["reddit"]
    params = build_params(cfg, query, since, subreddit, limit)
    tries = int(rd.get("timeout_retries", 3)) + 1
    pause = float(rd.get("timeout_pause_seconds", 5))
    for attempt in range(1, tries + 1):
        try:
            data, _ = get_json(session, rd["endpoint"], params=params)
        except requests.HTTPError as e:
            if not timeout_error(e):
                raise
            if attempt == tries:
                log.warning("reddit r/%s: archive said 'slow down' %d times, skipping it", subreddit, attempt)
                return None
            log.warning("reddit r/%s: archive said 'slow down', retry %d/%d in %.0fs", subreddit, attempt, tries - 1, pause)
            time.sleep(pause)
            pause = min(pause * 2, 60)
            continue
        posts = data.get("data") or []
        log.info("reddit r/%s %r: %d posts", subreddit, query, len(posts))
        return posts
    return None


def fetch_posts(session: requests.Session, cfg: dict, query: str, since: datetime | None,
                subreddits: list[str], limit: int) -> list[dict]:
    """One request per subreddit. Raises only when every subreddit failed."""
    rd = cfg["reddit"]
    posts: list[dict] = []
    skipped: list[str] = []
    for i, sub in enumerate(subreddits):
        if i:
            time.sleep(float(rd.get("delay_seconds", 1.0)))
        got = fetch_subreddit(session, cfg, query, since, sub, limit)
        if got is None:
            skipped.append(sub)
            continue
        posts.extend(got)
    if skipped and len(skipped) == len(subreddits):
        raise RuntimeError(f"reddit: the Arctic Shift archive refused every subreddit with 'Timeout. Maybe slow down a bit' ({', '.join(skipped)}); try again in a minute")
    if skipped:
        log.warning("reddit: skipped %s after repeated 'slow down' answers; results cover the other subreddits only", ", ".join(skipped))
    return posts


def epoch_iso(value) -> str:
    """The archive gives dates as Unix seconds. Empty when missing; never guessed."""
    if value in (None, ""):
        return ""
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()


def to_record(post: dict, fetched_at: str, body_max_chars: int, min_score: int = 0) -> dict | None:
    """One archive post to the 11-field record, or None when it is below min_score."""
    score = post.get("score")
    if score is not None and min_score and int(score) < min_score:
        return None
    link = permalink(post)
    text = html.unescape(post.get("selftext") or "").strip()
    if text in EMPTY_BODIES:
        text = ""
    url = (post.get("url") or "").strip()
    if url and not is_self_link(url, link):
        text = f"Link: {url}\n\n{text}".strip()
    return make_record(
        source=SOURCE,
        url=link,
        title=html.unescape(post.get("title") or "").strip(),
        body=text[:body_max_chars],
        score=score,
        comments=post.get("num_comments"),
        views="",
        created_at=epoch_iso(post.get("created_utc")),
        last_activity="",  # the archive has no activity date; do not guess
        status="",
        fetched_at=fetched_at,
    )


def posts_to_records(posts: list[dict], fetched_at: str, body_max_chars: int, min_score: int = 0) -> list[dict]:
    """Validate, dedupe by URL, sort best first."""
    records = []
    bad = 0
    for post in posts:
        rec = to_record(post, fetched_at, body_max_chars, min_score)
        if rec is None:
            continue
        problems = validate_record(rec)
        if problems:
            bad += 1
            log.warning("skipping invalid record %s: %s", rec.get("url"), problems)
            continue
        records.append(rec)
    by_url = {r["url"]: r for r in records}
    if bad:
        log.warning("reddit: %d invalid posts skipped", bad)
    return sorted(by_url.values(), key=lambda r: (-(r["score"] if r["score"] != "" else float("-inf")), r["url"]))


def search(session: requests.Session, cfg: dict, query: str, since: datetime, **options) -> list[dict]:
    """One question in, validated records out. Options: subreddits (list[str], default from config)."""
    rd = cfg["reddit"]
    subreddits = [s for s in (options.get("subreddits") or rd["subreddits"]) if s]
    if not subreddits:
        raise ValueError("reddit: no subreddits configured (reddit.subreddits in config.yaml, or --subreddits)")
    posts = fetch_posts(session, cfg, query, since, subreddits, int(rd.get("search_limit", 100)))
    records = posts_to_records(posts, now_iso(), rd["body_max_chars"], int(rd.get("search_min_score", 0)))
    log.info("reddit %r: %d records (r/%s, since %s)", query, len(records), ", r/".join(subreddits), since.date())
    return records


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    cfg = load_config()
    rd = cfg["reddit"]
    since = datetime.now(timezone.utc) - timedelta(days=30.4375 * cfg["months_back"])
    session = requests.Session()
    fetched_at = now_iso()
    posts: list[dict] = []
    # Sweep: every topic keyword in every configured subreddit, newest first.
    for keyword in cfg["keywords"]:
        try:
            posts.extend(fetch_posts(session, cfg, keyword, since, rd["subreddits"], int(rd.get("sweep_limit", 100))))
        except (requests.HTTPError, RateLimited, RuntimeError) as e:
            log.error("reddit %r: %s", keyword, e)
        time.sleep(float(rd.get("delay_seconds", 1.0)))
    records = posts_to_records(posts, fetched_at, rd["body_max_chars"], int(rd.get("min_score", 0)))

    out = f"{cfg['paths']['raw_dir']}/reddit.jsonl"
    n = write_jsonl(out, records)
    log.info("wrote %d records to %s (r/%s, since %s)", n, out, ", r/".join(rd["subreddits"]), since.date())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
