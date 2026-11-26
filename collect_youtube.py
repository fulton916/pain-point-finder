# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""YouTube source: the most-liked comments under the top videos for a question, via YouTube Data API v3.

Two ways in:
    search(session, cfg, query, since)   one question, returns records (used by ppf.py search)
    python collect_youtube.py            sweep mode: every config keyword, writes data/raw/youtube.jsonl
    python collect_youtube.py --check-key   test the key and report the quota facts

How a question is answered (two API methods, both read-only):
  1. search.list   GET <endpoint>/search?part=snippet&type=video&order=relevance&q=<words>
                   &publishedAfter=<since>&maxResults=<youtube.videos>    -> the top videos
  2. commentThreads.list   GET <endpoint>/commentThreads?part=snippet&videoId=<id>&order=relevance
                   &maxResults=100&textFormat=plainText                    -> each video's top comments
     Once per video, more pages only when youtube.comments_per_video is above 100.

Quota (https://developers.google.com/youtube/v3/determine_quota_cost, read 2026-11-26): a project gets
10,000 units a day for most methods, commentThreads.list and videos.list cost 1 unit per call (per page),
and search.list has its own bucket: 100 calls a day, 1 unit each (it used to cost 100 units out of the
shared 10,000; youtube.search_list_cost holds the number so the count can follow the docs). Everything
resets at midnight Pacific time. The API does not say how much quota is left in any response or header;
the Google Cloud console's Quotas page does. This module counts what it spends (search.list calls times
search_list_cost, plus one unit per comment page) and stops before youtube.max_units; the total is logged
and goes in the header sentence.

Terms (https://developers.google.com/youtube/terms/developer-policies and the API Services Terms of
Service, read 2026-11-26):
  * III.A.1: an API client must display a link to YouTube's Terms of Service; III.A.2: and to Google's
    Privacy Policy. ATTRIBUTION below carries both and ppf.py prints it with every result that includes
    this source.
  * III.E.4.d: public ("Non-Authorized") API data may be kept for at most 30 calendar days, then deleted or
    refreshed. search() therefore purges YouTube rows older than youtube.store_days (30) from the local
    store on every run, and sweep mode rewrites its raw file each time.
  * Required Minimum Functionality: comment text shown in full or with a way to reach it (every row links
    to the comment under its video), and the video title with it (the body starts with it in brackets).
  * III.D.1: one API project per client, credentials never embedded in open source. The key lives in .env
    as YOUTUBE_API_KEY, goes only in the `key` query parameter, and common.SECRETS redacts it everywhere.

Record mapping: title = the first `title_chars` (120) characters of the comment on one line, body =
"[<video title> (<channel>)] <comment text>", score = likeCount, comments = totalReplyCount, created_at =
the comment's publishedAt, last_activity = its updatedAt when present, url =
https://www.youtube.com/watch?v=<videoId>&lc=<commentId> (opens the comment under its video), status and
views empty (a views value would cost a videos.list call per question; not worth a unit). Likes count the
viewers who liked a comment under one video, not the people who share a problem, so config caps YouTube
at Moderate (scoring.label_caps.youtube).

Without a key the source is skipped with a sentence in the header and meta.note; the other sources still
run. A video with comments disabled (HTTP 403 commentsDisabled) is skipped with a log line.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from common import (
    SECRETS,
    _rate_limit_wait,
    STATS,
    USER_AGENT,
    env,
    load_config,
    load_env,
    log,
    make_record,
    now_iso,
    redact,
    setup_logging,
    validate_record,
    write_jsonl,
)

SOURCE = "youtube"
ATTRIBUTION = (
    "YouTube rows come from YouTube API Services and are subject to YouTube's Terms of Service "
    "(https://www.youtube.com/t/terms) and Google's Privacy Policy (https://policies.google.com/privacy). "
    "Comments belong to their authors; likes count the viewers who liked a comment under one video, not how "
    "many people share the problem. Each row links to the comment under its video."
)
COMMENT_URL = "https://www.youtube.com/watch?v={video_id}&lc={comment_id}"
DAILY_UNITS = 10_000          # the shared pool, per project per day (docs, 2026-11-26)
DAILY_SEARCH_CALLS = 100      # search.list's own bucket (docs, 2026-11-26)
QUOTAS_PAGE = "https://console.cloud.google.com/iam-admin/quotas"
# Error reasons the API puts in its JSON body. A 403 means several different things here, so this module
# reads the reason itself instead of treating every 403 as a rate limit (which common.get_json does).
COMMENTS_DISABLED = "commentsDisabled"
# Reasons that mean one video cannot be read (deleted or private since search.list answered, or its
# comments withheld). The video is skipped, like one with comments disabled, instead of failing the run.
VIDEO_SKIP_REASONS = {COMMENTS_DISABLED, "videoNotFound", "commentThreadNotFound", "forbidden"}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded", "userRateLimitExceeded"}
_WARNED_NO_KEY = False


class CommentsDisabled(Exception):
    """This one video cannot be read: comments turned off (HTTP 403 commentsDisabled), the video gone
    (404 videoNotFound) or its threads withheld (403 forbidden). `reason` says which."""

    def __init__(self, reason: str, message: str = ""):
        super().__init__(message or reason)
        self.reason = reason


class QuotaExceeded(RuntimeError):
    """The project's daily quota is used up (HTTP 403, reason quotaExceeded)."""


def api_key() -> str:
    """The key from .env, redacted everywhere from now on. Empty when not set (the caller decides)."""
    key = env("YOUTUBE_API_KEY")
    if key:
        SECRETS.add(key)
    return key


def since_param(since: datetime | None) -> str:
    """publishedAfter: RFC 3339 UTC to the second with a Z, as the API requires. Empty when None."""
    if since is None:
        return ""
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    return since.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def search_params(cfg: dict, query: str, since: datetime | None, key: str) -> dict:
    """One search.list call: the top videos for the words since the cut-off."""
    yt = cfg["youtube"]
    params: dict = {
        "part": "snippet",
        "type": "video",
        "order": "relevance",
        "q": " ".join(query.split()),
        "maxResults": max(1, min(int(yt.get("videos", 5)), 50)),
        "key": key,
    }
    value = since_param(since)
    if value:
        params["publishedAfter"] = value
    return params


def comments_params(cfg: dict, video_id: str, key: str, page_token: str = "") -> dict:
    """One commentThreads.list page: the video's top-level comments, most relevant first, as plain text."""
    yt = cfg["youtube"]
    params: dict = {
        "part": "snippet",
        "videoId": video_id,
        "order": "relevance",
        "maxResults": max(1, min(int(yt.get("comments_per_video", 100)), 100)),
        "textFormat": "plainText",
        "key": key,
    }
    if page_token:
        params["pageToken"] = page_token
    return params


def error_detail(resp: requests.Response) -> tuple[str, str]:
    """(reason, message) from the API's JSON error body, or ("", "") when the body is not that shape.
    Neither field carries the URL, so neither carries the key."""
    try:
        err = resp.json().get("error") or {}
        errors = err.get("errors") or [{}]
        reason = str(errors[0].get("reason") or err.get("status") or "")
        return reason, str(err.get("message") or "")
    except (ValueError, AttributeError, IndexError, TypeError):
        return "", ""


def api_get(session: requests.Session, url: str, params: dict, max_tries: int = 4, timeout: int = 30) -> dict:
    """GET one API URL and return its JSON. Retries connection errors, 5xx and 429 with backoff. A 403 is
    read for its reason: commentsDisabled raises CommentsDisabled, a quota reason raises QuotaExceeded, any
    other 4xx raises RuntimeError with the API's own message (never the URL, which holds the key)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    delay = 2.0
    last_err = "no attempts made"
    for attempt in range(1, max_tries + 1):
        last_try = attempt == max_tries
        try:
            STATS["requests"] += 1
            resp = session.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_err = redact(str(e))
            if not last_try:
                log.warning("youtube: request failed (%s), retry %d/%d in %.0fs", last_err, attempt, max_tries, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            continue
        if resp.status_code >= 500 or resp.status_code == 429:
            last_err = f"HTTP {resp.status_code}"
            if not last_try:
                wait = (_rate_limit_wait(resp) if resp.status_code == 429 else None) or delay
                log.warning("youtube: %s, retry %d/%d in %.0fs", last_err, attempt, max_tries, wait)
                time.sleep(wait)
                delay = min(delay * 2, 60)
            continue
        if resp.status_code >= 400:
            reason, message = error_detail(resp)
            if reason in VIDEO_SKIP_REASONS:
                raise CommentsDisabled(reason, redact(message))
            if reason in QUOTA_REASONS:
                raise QuotaExceeded(redact(f"youtube API quota: {reason}: {message}".rstrip(": ")))
            raise RuntimeError(redact(f"youtube API error HTTP {resp.status_code} {reason}: {message}".rstrip(": ")))
        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("youtube API error: the answer was not JSON") from None
        return data if isinstance(data, dict) else {}
    raise RuntimeError(f"youtube: gave up after {max_tries} tries: {last_err}")


class Budget:
    """Units spent in one run, and the stop before youtube.max_units. search.list calls are counted
    separately because they have their own daily bucket."""

    def __init__(self, max_units: int, search_list_cost: int):
        self.max_units = int(max_units)
        self.search_list_cost = int(search_list_cost)
        self.units = 0
        self.search_calls = 0
        self.comment_pages = 0

    def can_spend(self, cost: int) -> bool:
        return self.units + cost <= self.max_units

    def spend_search(self) -> None:
        self.search_calls += 1
        self.units += self.search_list_cost

    def spend_comment_page(self) -> None:
        self.comment_pages += 1
        self.units += 1

    def summary(self) -> str:
        return (f"{self.units} quota units ({self.search_calls} search.list call{'s' if self.search_calls != 1 else ''} "
                f"at {self.search_list_cost} each, {self.comment_pages} commentThreads page{'s' if self.comment_pages != 1 else ''} "
                f"at 1 each; max_units {self.max_units})")


def fetch_videos(session: requests.Session, cfg: dict, query: str, since: datetime | None, key: str,
                 budget: Budget) -> list[dict]:
    """The top videos for the words: [{video_id, title, channel}]. One search.list call, or none when it
    would pass max_units."""
    yt = cfg["youtube"]
    if not budget.can_spend(budget.search_list_cost):
        log.warning("youtube: search.list would pass max_units (%d), nothing fetched", budget.max_units)
        return []
    budget.spend_search()
    data = api_get(session, f"{yt['endpoint'].rstrip('/')}/search", search_params(cfg, query, since, key))
    videos = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        video_id = str((item.get("id") or {}).get("videoId") or "").strip()
        snippet = item.get("snippet") or {}
        if video_id:
            videos.append({"video_id": video_id, "title": str(snippet.get("title") or ""),
                           "channel": str(snippet.get("channelTitle") or "")})
    log.info("youtube %r: %d videos", query, len(videos))
    return videos


def fetch_comments(session: requests.Session, cfg: dict, video: dict, key: str, budget: Budget) -> list[dict]:
    """Up to youtube.comments_per_video top-level comment threads for one video, one page of at most 100
    per unit. Stops before max_units. Raises CommentsDisabled when the video has comments off, is gone or is withheld."""
    yt = cfg["youtube"]
    wanted = max(1, int(yt.get("comments_per_video", 100)))
    threads: list[dict] = []
    token = ""
    while len(threads) < wanted:
        if not budget.can_spend(1):
            log.warning("youtube: stopping before max_units (%d) with %d comments for video %s",
                        budget.max_units, len(threads), video["video_id"])
            break
        budget.spend_comment_page()
        data = api_get(session, f"{yt['endpoint'].rstrip('/')}/commentThreads",
                       comments_params(cfg, video["video_id"], key, token))
        batch = [t for t in (data.get("items") or []) if isinstance(t, dict)]
        threads.extend(batch)
        token = str(data.get("nextPageToken") or "")
        if not token or not batch:
            break
        time.sleep(float(yt.get("delay_seconds", 0.5)))
    return threads[:wanted]


def comment_url(video_id: str, comment_id: str) -> str:
    if not video_id or not comment_id:
        return ""
    return COMMENT_URL.format(video_id=video_id, comment_id=comment_id)


def to_record(thread: dict, video: dict, fetched_at: str, body_max_chars: int = 2000, title_chars: int = 120) -> dict | None:
    """One commentThreads item to the 11-field record. None when the comment has no text or no id."""
    snippet = thread.get("snippet") or {}
    top = snippet.get("topLevelComment") or {}
    comment = top.get("snippet") or {}
    text = str(comment.get("textDisplay") or comment.get("textOriginal") or "").strip()
    comment_id = str(top.get("id") or thread.get("id") or "").strip()
    video_id = str(comment.get("videoId") or snippet.get("videoId") or video.get("video_id") or "").strip()
    if not text or not comment_id:
        return None
    label = video.get("title") or ""
    if video.get("channel"):
        label = f"{label} ({video['channel']})" if label else video["channel"]
    body = f"[{label}] {text}" if label else text
    return make_record(
        source=SOURCE,
        url=comment_url(video_id, comment_id),
        title=" ".join(text.split())[:title_chars],
        body=body[:body_max_chars],
        score=comment.get("likeCount"),
        comments=snippet.get("totalReplyCount"),
        views="",
        created_at=str(comment.get("publishedAt") or ""),
        last_activity=str(comment.get("updatedAt") or ""),
        status="",
        fetched_at=fetched_at,
    )


def threads_to_records(threads: list[tuple[dict, dict]], fetched_at: str, body_max_chars: int = 2000,
                       title_chars: int = 120, min_likes: int = 0) -> list[dict]:
    """(thread, video) pairs to validated records, deduped by URL, most liked first."""
    records = []
    bad = skipped = 0
    for thread, video in threads:
        rec = to_record(thread, video, fetched_at, body_max_chars, title_chars)
        if rec is None or (min_likes and (rec["score"] == "" or int(rec["score"]) < min_likes)):
            skipped += 1
            continue
        problems = validate_record(rec)
        if problems:
            bad += 1
            log.warning("skipping invalid record %s: %s", rec.get("url"), problems)
            continue
        records.append(rec)
    by_url = {r["url"]: r for r in records}
    if skipped:
        log.info("youtube: %d comments skipped (no text, or under the like floor)", skipped)
    if bad:
        log.warning("youtube: %d invalid comments skipped", bad)
    return sorted(by_url.values(), key=lambda r: (-(r["score"] if r["score"] != "" else float("-inf")), r["url"]))


def collect(session: requests.Session, cfg: dict, query: str, since: datetime | None, key: str,
            budget: Budget, min_likes: int = 0) -> tuple[list[dict], int, int]:
    """Records for one question plus (videos read, videos skipped for disabled comments)."""
    yt = cfg["youtube"]
    videos = fetch_videos(session, cfg, query, since, key, budget)
    pairs: list[tuple[dict, dict]] = []
    read = disabled = 0
    for i, video in enumerate(videos):
        if not budget.can_spend(1):
            log.warning("youtube: stopping before max_units (%d): %d of %d videos not read", budget.max_units, len(videos) - i, len(videos))
            break
        if i:
            time.sleep(float(yt.get("delay_seconds", 0.5)))
        try:
            threads = fetch_comments(session, cfg, video, key, budget)
        except CommentsDisabled as e:
            disabled += 1
            what = "comments are disabled on" if e.reason == COMMENTS_DISABLED else f"{e.reason}: cannot read"
            log.info("youtube: %s video %s (%s), skipped", what, video["video_id"], video["title"][:60])
            continue
        read += 1
        pairs.extend((t, video) for t in threads)
        log.info("youtube: %d comments from video %s (%s)", len(threads), video["video_id"], video["title"][:60])
    records = threads_to_records(pairs, now_iso(), int(yt.get("body_max_chars", 2000)), int(yt.get("title_chars", 120)), min_likes)
    return records, read, disabled


def purge_old_rows(store, cfg: dict, now: datetime | None = None) -> int:
    """Delete YouTube rows fetched more than youtube.store_days (30) days ago from the evidence store, as
    the Developer Policies require (III.E.4.d). Returns how many went. `store` may be None (nothing to do)."""
    if store is None:
        return 0
    days = int(cfg["youtube"].get("store_days", 30))
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    gone = store.purge(SOURCE, cutoff)
    if gone:
        log.info("youtube: %d stored rows older than %d days deleted (YouTube API policy III.E.4.d)", gone, days)
    return gone


def search(session: requests.Session, cfg: dict, query: str, since: datetime, **options) -> list[dict]:
    """One question in, validated records out. Options: info (list[str]): a sentence saying how many
    videos were read and what the run spent; notes (list[str]): when the key is missing, the sentence saying
    YouTube was skipped (ppf.py prints both in the header and meta.note and does not cache a partial
    answer); store (Store): where old YouTube rows are purged. Other options belong to other sources."""
    global _WARNED_NO_KEY
    yt = cfg["youtube"]
    if not " ".join(query.split()):
        raise ValueError("youtube: the question has no words")
    key = api_key()
    if not key:
        why = "youtube skipped: YOUTUBE_API_KEY is empty in .env (see README, Getting a YouTube API key)."
        if not _WARNED_NO_KEY:
            log.warning("%s", why)
            _WARNED_NO_KEY = True
        if options.get("notes") is not None:
            options["notes"].append(why)
        return []
    budget = Budget(int(yt.get("max_units", 600)), int(yt.get("search_list_cost", 1)))
    records, read, disabled = collect(session, cfg, query, since, key, budget, int(yt.get("search_min_likes", 0)))
    skipped = f", {disabled} skipped (comments disabled or video unavailable)" if disabled else ""
    why = f"youtube read the top comments of {read} video{'s' if read != 1 else ''}{skipped}; spent {budget.summary()}."
    log.info("youtube %r: %d records (since %s); spent %s", query, len(records), since.date() if since else "any date",
             budget.summary())
    if options.get("info") is not None:
        options["info"].append(why)
    purge_old_rows(options.get("store"), cfg)
    return records


def check_key(cfg: dict) -> int:
    """One videos.list call (1 unit, not one of the 100 daily search.list calls): does the key work? The
    API never says how much quota is left, so the daily allowance and this run's spend are reported instead."""
    yt = cfg["youtube"]
    key = api_key()
    if not key:
        print("FAILED: YOUTUBE_API_KEY is empty in .env. See README, Getting a YouTube API key.")
        return 1
    session = requests.Session()
    params = {"part": "id", "chart": "mostPopular", "maxResults": 1, "key": key}
    try:
        api_get(session, f"{yt['endpoint'].rstrip('/')}/videos", params, max_tries=1)
    except (QuotaExceeded, RuntimeError, CommentsDisabled) as e:
        print(f"FAILED: {redact(str(e))}")
        print("'API key not valid' means the key is wrong; 'has not been used in project' or 'is disabled' means "
              "YouTube Data API v3 is not enabled for the project that owns the key. Check YOUTUBE_API_KEY in .env.")
        return 1
    print("OK: key accepted (one videos.list call answered)")
    print(f"  units left today: not reported by the API. The default allowance is {DAILY_UNITS:,} units a day plus "
          f"{DAILY_SEARCH_CALLS} search.list calls a day, resetting at midnight Pacific time; this check spent 1 unit.")
    print(f"  one question spends about 1 search.list call plus {int(yt.get('videos', 5))} units "
          f"({int(yt.get('videos', 5))} videos, one comment page each); max_units is {int(yt.get('max_units', 600))}.")
    print(f"  usage so far today: {QUOTAS_PAGE} (sign in, filter on YouTube Data API v3)")
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    load_env()
    cfg = load_config()
    if argv and "--check-key" in argv:
        return check_key(cfg)
    yt = cfg["youtube"]
    key = api_key()
    if not key:
        log.warning("youtube skipped: YOUTUBE_API_KEY is empty in .env (see README, Getting a YouTube API key)")
        return 0
    since = datetime.now(timezone.utc) - timedelta(days=30.4375 * cfg["months_back"])
    session = requests.Session()
    budget = Budget(int(yt.get("sweep_max_units", 2000)), int(yt.get("search_list_cost", 1)))
    records: list[dict] = []
    # Sweep: every topic keyword, the top videos' top comments. One budget for the whole sweep.
    for i, keyword in enumerate(cfg["keywords"]):
        if i:
            time.sleep(float(yt.get("delay_seconds", 0.5)))
        try:
            found, _, _ = collect(session, cfg, keyword, since, key, budget, int(yt.get("min_likes", 0)))
            records.extend(found)
        except QuotaExceeded as e:
            log.error("youtube %r: %s; stopping the sweep", keyword, e)
            break
        except (requests.HTTPError, RuntimeError) as e:
            log.error("youtube %r: %s", keyword, e)
    by_url = {r["url"]: r for r in records}
    records = sorted(by_url.values(), key=lambda r: (-(r["score"] if r["score"] != "" else float("-inf")), r["url"]))
    out = f"{cfg['paths']['raw_dir']}/youtube.jsonl"
    n = write_jsonl(out, records)
    log.info("wrote %d records to %s (since %s); spent %s", n, out, since.date(), budget.summary())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
