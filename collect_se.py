# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Stack Exchange source, via API v2.3 `/search/advanced` on one site (default salesforce).

Two ways in:
    search(session, cfg, query, since)   one question, returns records (used by ppf.py search)
    python collect_se.py                 sweep mode: the site's top-voted questions since months_back,
                                         writes data/raw/stackexchange.jsonl
    python collect_se.py --check-key     test the key and show the daily quota

Record mapping: score = question score (votes), comments = answer_count, views = view_count,
status = "answered" when the API says is_answered, url = the question link. Titles and bodies come
back HTML-encoded, so they are unescaped and the body tags are stripped.

Terms honored (pasted 2026-11-25, https://api.stackexchange.com/docs/throttle and the API Terms):
  * `backoff` in a response means wait that many seconds before calling the same method again.
    The wait is remembered across calls in this process.
  * 10,000 requests/day per key (300 without a key). quota_remaining is logged on every page.
  * Never more than 30 requests/second per IP: delay_seconds between pages keeps us far under.
  * No identical request more than once a minute: ppf.py's 24-hour cache covers this.
  * Attribution: content is CC BY-SA from the Stack Exchange network. ATTRIBUTION below is printed
    with every result that includes this source, and every row links back to the question.

The key goes only in the `key` query parameter, read from .env (STACKEXCHANGE_KEY). It is added to
common.SECRETS so it never appears in a log line or an error message.

Which site a search asks (search mode only; sweep mode and --check-key always use stackexchange.site):
  1. --site, when given.
  2. stackexchange.sites in config.yaml: a topic word that appears in the question as whole words,
     ignoring case ("how to keep bread from going stale" matches `bread` -> cooking). The API searches
     one site per request, so the first matching key in config order wins.
  3. stackexchange.site, the config default (salesforce).
  The sentence saying which step chose it goes to the caller's `info` list, so ppf.py prints it in the
  header and meta.note and a wrong guess is obvious. The same shape as collect_reddit.choose_subreddits.

Docs: https://api.stackexchange.com/docs/advanced-search
"""

from __future__ import annotations

import html
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

from common import (
    RateLimited,
    SECRETS,
    clean_html,
    env,
    get_json,
    has_phrase,
    load_config,
    load_env,
    log,
    make_record,
    now_iso,
    setup_logging,
    validate_record,
    words_of,
    write_jsonl,
)

SOURCE = "stackexchange"
ATTRIBUTION = (
    "Stack Exchange content is from the Stack Exchange Network (https://stackexchange.com), "
    "written by its users and licensed CC BY-SA 4.0. Each row links to the original question."
)
# `filter=withbody` is a built-in filter: the default fields plus the question body.
FILTER = "withbody"

# The API asks us to wait `backoff` seconds before hitting the same method again. Keyed by method
# so a backoff from /search/advanced does not stall an unrelated call.
BACKOFF_UNTIL: dict[str, float] = {}
_WARNED_NO_KEY = False
# Sites whose web address is not <api_site_parameter>.stackexchange.com (the founding sites). Every
# other api_site_parameter follows that pattern, including meta sites (meta.cooking.stackexchange.com
# is `meta.cooking`). Used only for the header sentence; the API gets the bare parameter.
SITE_HOSTS = {
    "stackoverflow": "stackoverflow.com",
    "serverfault": "serverfault.com",
    "superuser": "superuser.com",
    "askubuntu": "askubuntu.com",
    "mathoverflow": "mathoverflow.net",
    "meta.stackoverflow": "meta.stackoverflow.com",
    "meta.serverfault": "meta.serverfault.com",
    "meta.superuser": "meta.superuser.com",
    "meta.askubuntu": "meta.askubuntu.com",
    "meta.mathoverflow": "meta.mathoverflow.net",
}


def site_host(site: str) -> str:
    """The site's web address for a human: cooking -> cooking.stackexchange.com, stackoverflow -> stackoverflow.com."""
    return SITE_HOSTS.get(site, f"{site}.stackexchange.com")


def topic_site(cfg: dict, query: str) -> tuple[str, str]:
    """(site, key) for the first key of stackexchange.sites that appears in the question as whole words,
    ignoring case (a key with spaces must appear as that phrase), in config order. ("", "") when nothing
    matched or there is no sites map. One site per search is all the API allows, so order matters."""
    words = words_of(query)
    for key, value in (cfg["stackexchange"].get("sites") or {}).items():
        if has_phrase(words, str(key)):
            site = str(value or "").strip()
            if site:
                return site, str(key)
    return "", ""


def choose_site(cfg: dict, query: str, override: str | None = None) -> tuple[str, str]:
    """Which site to search and one sentence saying how it was chosen: --site, the sites map in
    config.yaml, or the config default."""
    se = cfg["stackexchange"]
    if override:
        site = str(override).strip()
        return site, f"stackexchange searched {site_host(site)} (from --site)."
    site, key = topic_site(cfg, query)
    if site:
        return site, f"stackexchange searched {site_host(site)} (site map in config.yaml matched '{key}')."
    site = str(se["site"]).strip()
    return site, (f"stackexchange searched {site_host(site)} (config default: no word in stackexchange.sites "
                  f"matched). Pass --site to choose yourself.")


def key_param() -> dict[str, str]:
    """The API key as a query parameter, or nothing. Logged never, redacted everywhere."""
    global _WARNED_NO_KEY
    key = env("STACKEXCHANGE_KEY")
    if not key:
        if not _WARNED_NO_KEY:
            log.warning("STACKEXCHANGE_KEY is empty: running without a key (300 requests/day instead of 10,000)")
            _WARNED_NO_KEY = True
        return {}
    SECRETS.add(key)
    return {"key": key}


def build_params(cfg: dict, query: str, since: datetime | None, page: int, site: str, min_score: int,
                 search_in: str = "title", tagged: str = "") -> dict:
    """Parameters for one page of /search/advanced, sorted by votes, best first.
    `search_in` is "title" (the words must appear in the title) or "q" (free text, any field).
    `fromdate` filters on the question's creation date (the API offers no activity-date filter here)."""
    se = cfg["stackexchange"]
    params: dict = {
        "site": site,
        "sort": "votes",
        "order": "desc",
        "min": min_score,  # with sort=votes, min applies to the score
        "pagesize": se["pagesize"],
        "page": page,
        "filter": FILTER,
    }
    words = " ".join(query.split())
    if words:
        params["q" if search_in == "q" else "title"] = words
    if tagged:
        params["tagged"] = tagged
    if since is not None:
        params["fromdate"] = int(since.timestamp())
    return params


def wait_for_backoff(method: str) -> None:
    until = BACKOFF_UNTIL.get(method, 0.0)
    remaining = until - time.time()
    if remaining > 0:
        log.info("stackexchange: honoring backoff, waiting %.0fs", remaining)
        time.sleep(remaining)


def note_response(method: str, data: dict) -> None:
    """Record a backoff and log the quota. Called after every response."""
    backoff = data.get("backoff")
    if backoff:
        BACKOFF_UNTIL[method] = time.time() + float(backoff)
        log.warning("stackexchange: API asked for a %ss backoff on %s", backoff, method)
    remaining = data.get("quota_remaining")
    if remaining is not None:
        if remaining <= 100:
            log.warning("stackexchange: only %s of %s daily requests left", remaining, data.get("quota_max"))
        else:
            log.info("stackexchange: quota %s/%s left today", remaining, data.get("quota_max"))


def api_error(e: requests.HTTPError) -> RuntimeError:
    """The API explains 4xx errors in the body (error_id, error_name, error_message). Use that text,
    which has no URL and so no key. 502 = throttle_violation (quota or backoff ignored)."""
    detail = ""
    try:
        body = e.response.json()
        detail = f"{body.get('error_name', '')} {body.get('error_id', '')}: {body.get('error_message', '')}".strip()
    except (ValueError, AttributeError):
        pass
    return RuntimeError(f"stackexchange API error: {detail or e}")


def fetch_items(session: requests.Session, cfg: dict, query: str, since: datetime | None, site: str,
                min_score: int, max_pages: int, search_in: str = "title", tagged: str = "") -> list[dict]:
    """Return raw question items for one query, walking up to max_pages pages."""
    se = cfg["stackexchange"]
    endpoint = se["endpoint"]
    method = endpoint.rsplit("/2.3/", 1)[-1]
    key = key_param()
    items: list[dict] = []
    label = repr(query) if query else f"tagged={tagged!r}" if tagged else "whole site"
    for page in range(1, max_pages + 1):
        params = dict(build_params(cfg, query, since, page, site, min_score, search_in, tagged), **key)
        wait_for_backoff(method)
        try:
            data, _ = get_json(session, endpoint, params=params)
        except requests.HTTPError as e:
            raise api_error(e) from None
        note_response(method, data)
        page_items = data.get("items", [])
        items.extend(page_items)
        log.info("stackexchange %s %s page %d: %d questions", site, label, page, len(page_items))
        if not data.get("has_more") or not page_items:
            break
        if data.get("quota_remaining") == 0:
            log.warning("stackexchange: daily quota used up, stopping after page %d", page)
            break
        time.sleep(se["delay_seconds"])
    return items


def epoch_iso(value) -> str:
    """The API gives dates as Unix seconds. Empty when missing; never guessed."""
    if value in (None, ""):
        return ""
    return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat()


def to_record(item: dict, fetched_at: str, body_max_chars: int) -> dict:
    return make_record(
        source=SOURCE,
        url=item.get("link") or "",
        title=html.unescape(item.get("title") or ""),
        body=clean_html(item.get("body") or "")[:body_max_chars],
        score=item.get("score"),
        comments=item.get("answer_count"),
        views=item.get("view_count"),
        created_at=epoch_iso(item.get("creation_date")),
        last_activity=epoch_iso(item.get("last_activity_date")),
        status="answered" if item.get("is_answered") else "",
        fetched_at=fetched_at,
    )


def items_to_records(items: list[dict], fetched_at: str, body_max_chars: int) -> list[dict]:
    """Validate, dedupe by URL, sort best first."""
    records = []
    bad = 0
    for item in items:
        rec = to_record(item, fetched_at, body_max_chars)
        problems = validate_record(rec)
        if problems:
            bad += 1
            log.warning("skipping invalid record %s: %s", rec.get("url"), problems)
            continue
        records.append(rec)
    by_url = {r["url"]: r for r in records}
    if bad:
        log.warning("stackexchange: %d invalid items skipped", bad)
    return sorted(by_url.values(), key=lambda r: (-(r["score"] if r["score"] != "" else -1), r["url"]))


def search(session: requests.Session, cfg: dict, query: str, since: datetime, **options) -> list[dict]:
    """One question in, validated records out. Options: site (str; without it the stackexchange.sites
    map, then stackexchange.site decide, see choose_site); info (list[str]): when given, the sentence
    saying which site was searched and why is appended to it (ppf.py prints it in the header and
    meta.note)."""
    se = cfg["stackexchange"]
    site, why = choose_site(cfg, query, options.get("site"))
    log.info("%s", why)
    if options.get("info") is not None:
        options["info"].append(why)
    min_score = int(se.get("search_min_score", 0))
    items = fetch_items(session, cfg, query, since, site, min_score, int(se.get("search_max_pages", 2)),
                        se.get("search_in", "title"))
    records = items_to_records(items, now_iso(), se["body_max_chars"])
    log.info("stackexchange %r: %d records (site=%s, score>=%d, since %s)", query, len(records), site, min_score, since.date())
    return records


def check_key(cfg: dict) -> int:
    """One tiny request: does the key work, and how much of today's quota is left?"""
    se = cfg["stackexchange"]
    session = requests.Session()
    params = dict(build_params(cfg, "", None, 1, se["site"], 0), pagesize=1, **key_param())
    wait_for_backoff(se["endpoint"].rsplit("/2.3/", 1)[-1])
    try:
        data, _ = get_json(session, se["endpoint"], params=params, max_tries=1)
    except requests.HTTPError as e:
        print(f"FAILED: {api_error(e)}")
        print("A 400 with error 400/bad_parameter means the key is wrong. Check STACKEXCHANGE_KEY in .env.")
        return 1
    except (RateLimited, RuntimeError) as e:
        print(f"FAILED: {e}")
        return 1
    print(f"OK: {'key accepted' if env('STACKEXCHANGE_KEY') else 'no key (300 requests/day)'}")
    print(f"  site: {se['site']}")
    print(f"  daily quota: {data.get('quota_remaining')} of {data.get('quota_max')} requests left")
    return 0


def main(argv: list[str]) -> int:
    setup_logging()
    load_env()
    cfg = load_config()
    if "--check-key" in argv:
        return check_key(cfg)

    se = cfg["stackexchange"]
    since = datetime.now(timezone.utc) - timedelta(days=30.4375 * cfg["months_back"])
    session = requests.Session()
    fetched_at = now_iso()
    items: list[dict] = []
    # Sweep: the site's top-voted questions, whole site or one query per tag in sweep_tags.
    for tag in se.get("sweep_tags") or [""]:
        items.extend(fetch_items(session, cfg, "", since, se["site"], int(se["min_score"]), int(se["max_pages"]), tagged=tag))
        time.sleep(se["delay_seconds"])
    records = items_to_records(items, fetched_at, se["body_max_chars"])

    out = f"{cfg['paths']['raw_dir']}/stackexchange.jsonl"
    n = write_jsonl(out, records)
    log.info("wrote %d records to %s (site %s, since %s)", n, out, se["site"], since.date())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
