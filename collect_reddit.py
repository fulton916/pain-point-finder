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

Which subreddits a search asks (search mode only; sweep mode always uses reddit.subreddits):
  1. --subreddits, when given.
  2. reddit.topics in config.yaml: a topic key that appears in the question as whole words.
  3. The archive's subreddit name search, GET /api/subreddits/search?subreddit=<word>, once per useful
     question word, ranked by subscribers; public, non-adult, at least reddit.min_subscribers, at most
     reddit.auto_max. It is name-based (checked 2026-11-25: "veterans" finds r/Veterans, not
     r/VeteransBenefits), which is why the hand-kept topic map comes first. Same delay and 422
     handling as posts.
  4. reddit.subreddits, the config default.
  The sentence saying which step chose them goes to the caller's `info` list, so ppf.py can print it
  in the header and meta.note and a wrong guess is obvious.

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
    has_phrase,
    load_config,
    log,
    make_record,
    now_iso,
    setup_logging,
    validate_record,
    words_of,
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
# Subreddits returned per name-search word. The archive matches names, so a word rarely has many.
NAME_SEARCH_LIMIT = 25
# Question words never sent to the name search when config.yaml has no reddit.name_search_skip_words.
DEFAULT_SKIP_WORDS = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "is", "are",
                      "how", "what", "why", "best", "vs", "my", "i"}


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
        "fields": ",".join(dict.fromkeys(list(rd.get("fields") or FIELDS) + REQUIRED_FIELDS)),
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


def get_with_slowdown_retry(session: requests.Session, cfg: dict, url: str, params: dict, label: str) -> dict | None:
    """One archive GET. A 422 'Timeout. Maybe slow down a bit' is retried timeout_retries times, pausing
    timeout_pause_seconds and doubling each time; when it keeps coming, None (the caller skips `label`).
    Any other error is raised."""
    rd = cfg["reddit"]
    tries = int(rd.get("timeout_retries", 3)) + 1
    pause = float(rd.get("timeout_pause_seconds", 5))
    for attempt in range(1, tries + 1):
        try:
            data, _ = get_json(session, url, params=params)
        except requests.HTTPError as e:
            if not timeout_error(e):
                raise
            if attempt == tries:
                log.warning("reddit %s: archive said 'slow down' %d times, skipping it", label, attempt)
                return None
            log.warning("reddit %s: archive said 'slow down', retry %d/%d in %.0fs", label, attempt, tries - 1, pause)
            time.sleep(pause)
            pause = min(pause * 2, 60)
            continue
        return data if isinstance(data, dict) else {"data": data}
    return None


def fetch_subreddit(session: requests.Session, cfg: dict, query: str, since: datetime | None,
                    subreddit: str, limit: int) -> list[dict] | None:
    """Posts from one subreddit, or None when the archive kept timing out on it."""
    data = get_with_slowdown_retry(session, cfg, cfg["reddit"]["endpoint"],
                                   build_params(cfg, query, since, subreddit, limit), f"r/{subreddit}")
    if data is None:
        return None
    posts = data.get("data") or []
    log.info("reddit r/%s %r: %d posts", subreddit, query, len(posts))
    return posts


def fetch_posts(session: requests.Session, cfg: dict, query: str, since: datetime | None,
                subreddits: list[str], limit: int, skipped: list[str] | None = None) -> list[dict]:
    """One request per subreddit. Raises only when every subreddit failed. A subreddit the archive
    kept refusing is left out of the result and appended to `skipped` when the caller passes a list."""
    rd = cfg["reddit"]
    posts: list[dict] = []
    skipped = skipped if skipped is not None else []
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


def topic_subreddits(cfg: dict, query: str) -> tuple[list[str], list[str]]:
    """Subreddits from reddit.topics whose key appears in the question as whole words, ignoring case
    (a key with spaces must appear as that phrase), in config order with no repeats; and the keys that
    matched. ([], []) when nothing matched or there is no topics map. (The Stack Exchange source picks
    its site the same way from stackexchange.sites; the word matching is common.has_phrase.)"""
    words = words_of(query)
    subs: list[str] = []
    matched: list[str] = []
    for key, value in (cfg["reddit"].get("topics") or {}).items():
        if has_phrase(words, str(key)):
            matched.append(str(key))
            for sub in value or []:
                sub = str(sub).strip().removeprefix("r/")
                if sub and sub.lower() not in {x.lower() for x in subs}:
                    subs.append(sub)
    return subs, matched


def name_search_words(cfg: dict, query: str) -> list[str]:
    """Question words worth a name-search request: no repeats, no one-letter words, none from
    reddit.name_search_skip_words (DEFAULT_SKIP_WORDS when config.yaml has no such list), and at most
    reddit.name_search_max_words of them (0 or missing = no cap)."""
    rd = cfg["reddit"]
    skip = rd.get("name_search_skip_words")
    skip = {str(w).lower() for w in (skip if skip is not None else DEFAULT_SKIP_WORDS)}
    words = [w for w in dict.fromkeys(words_of(query)) if len(w) > 1 and w not in skip]
    cap = int(rd.get("name_search_max_words") or 0)
    return words[:cap] if cap > 0 else words


def name_search_subreddits(session: requests.Session, cfg: dict, words: list[str],
                           skipped_words: list[str] | None = None) -> list[dict]:
    """Ask the archive's subreddit name search for each word, delay_seconds apart. Returns the public,
    non-adult subreddits with at least reddit.min_subscribers subscribers, largest first, at most
    reddit.auto_max of them, as {"name", "subscribers", "word"} dicts. [] when no words, or when
    reddit.subreddit_search_endpoint is not configured. A word the archive kept refusing with 422
    'Timeout' is skipped and appended to `skipped_words` when the caller passes a list."""
    rd = cfg["reddit"]
    endpoint = rd.get("subreddit_search_endpoint")
    if not endpoint or not words:
        return []
    skipped = skipped_words if skipped_words is not None else []
    found: dict[str, dict] = {}
    floor = int(rd.get("min_subscribers", 0))
    for i, word in enumerate(words):
        if i:
            time.sleep(float(rd.get("delay_seconds", 1.0)))
        data = get_with_slowdown_retry(session, cfg, endpoint, {"subreddit": word, "limit": NAME_SEARCH_LIMIT},
                                       f"name search {word!r}")
        if data is None:
            skipped.append(word)
            continue
        items = data.get("data") or []
        log.info("reddit name search %r: %d subreddits", word, len(items))
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("display_name") or "").strip()
            if not name or item.get("over18") or (item.get("subreddit_type") or "public") != "public":
                continue
            try:
                subscribers = int(item.get("subscribers") or 0)
            except (TypeError, ValueError):
                continue
            if subscribers < floor:
                continue
            key = name.lower()
            if key not in found or subscribers > found[key]["subscribers"]:
                found[key] = {"name": name, "subscribers": subscribers, "word": word}
    ranked = sorted(found.values(), key=lambda d: (-d["subscribers"], d["name"].lower()))
    return ranked[: max(1, int(rd.get("auto_max", 3)))]


def _listed(subs: list[str]) -> str:
    return ", ".join(f"r/{s}" for s in subs)


def _quoted(words: list[str]) -> str:
    return ", ".join(f"'{w}'" for w in words)


def choose_subreddits(session: requests.Session, cfg: dict, query: str,
                      override: list[str] | None = None) -> tuple[list[str], str, bool]:
    """Which subreddits to search and one sentence saying how they were chosen: --subreddits, the
    topic map, the archive's name search, or the config default. The bool is True when name-search
    requests went to the archive, so the caller can wait delay_seconds before the first posts request."""
    rd = cfg["reddit"]
    if override:
        subs = [str(s).strip().removeprefix("r/") for s in override if s]
        return subs, f"reddit searched {_listed(subs)} (from --subreddits).", False
    subs, matched = topic_subreddits(cfg, query)
    if subs:
        return subs, f"reddit searched {_listed(subs)} (topic map in config.yaml matched {_quoted(matched)}).", False
    words = name_search_words(cfg, query)
    endpoint = rd.get("subreddit_search_endpoint")
    requested = bool(words and endpoint)
    skipped: list[str] = []
    found = name_search_subreddits(session, cfg, words, skipped)
    if found:
        listed = ", ".join(f"r/{d['name']} ({d['subscribers']:,} subscribers)" for d in found)
        why = (f"reddit searched {listed}: no topic in config.yaml matched, so these are the largest public "
               f"subreddits the archive's name search found for {_quoted(words)}")
        if skipped:
            why += f"; the archive refused the name search for {_quoted(skipped)}"
        return [d["name"] for d in found], why + ". Pass --subreddits to choose yourself.", requested
    default = [str(s).strip().removeprefix("r/") for s in (rd.get("subreddits") or []) if s]
    if not endpoint:
        reason = "the archive's name search is not configured (reddit.subreddit_search_endpoint)"
    elif not words:
        reason = "the question had no words worth a name search"
    elif skipped and len(skipped) == len(words):
        reason = f"the archive refused the name search for {_quoted(skipped)} with 'Timeout. Maybe slow down a bit'"
    else:
        reason = (f"the archive's name search found no public subreddit with {int(rd.get('min_subscribers', 0)):,}+ "
                  f"subscribers for {_quoted(words)}")
        if skipped:
            reason += f" (and refused {_quoted(skipped)})"
    return default, (f"reddit searched {_listed(default)} (config default: no topic in config.yaml matched and "
                     f"{reason}). Pass --subreddits to choose yourself."), requested


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
    """One question in, validated records out. Options: subreddits (list[str]; without it the topic
    map, then the archive's name search, then reddit.subreddits decide, see choose_subreddits);
    info (list[str]): when given, the sentence saying which subreddits were searched and why is
    appended to it (ppf.py prints it in the header and meta.note); notes (list[str]): when given, a
    sentence naming any skipped subreddit is appended to it, so the caller can tell the reader that
    part of the results is missing (ppf.py puts it in meta.note and does not cache that answer)."""
    rd = cfg["reddit"]
    subreddits, why, requested = choose_subreddits(session, cfg, query, options.get("subreddits"))
    if not subreddits:
        raise ValueError("reddit: no subreddits chosen (reddit.subreddits or reddit.topics in config.yaml, or --subreddits)")
    if requested:
        time.sleep(float(rd.get("delay_seconds", 1.0)))
    log.info("%s", why)
    if options.get("info") is not None:
        options["info"].append(why)
    skipped: list[str] = []
    posts = fetch_posts(session, cfg, query, since, subreddits, int(rd.get("search_limit", 100)), skipped)
    if skipped and options.get("notes") is not None:
        options["notes"].append(
            f"reddit skipped r/{', r/'.join(skipped)} after repeated 'Timeout. Maybe slow down a bit' answers; "
            "Reddit results cover the other subreddits only.")
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
