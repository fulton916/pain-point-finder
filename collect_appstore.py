# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""App Store customer reviews source, via two public Apple endpoints. No key.

Two ways in:
    search(session, cfg, query, since)   one question, returns records (used by ppf.py search)
    python collect_appstore.py           sweep mode: every config keyword, writes data/raw/appstore.jsonl

How a question becomes reviews:
  1. The iTunes Search API, GET https://itunes.apple.com/search?term=<words>&entity=software&limit=5&country=us,
     finds the apps whose names and descriptions match the words. The first `appstore.apps` (3) results are
     kept. Apple matches loosely (nonsense words still return five apps), so the header names the apps read
     and `--apps <id,id>` names them outright (one GET https://itunes.apple.com/lookup?id=<ids> call then
     fetches their names; an id the lookup does not know is labeled "app <id>").
  2. Each app's customer-review feed, GET https://itunes.apple.com/<country>/rss/customerreviews/page=<n>/
     id=<appId>/sortBy=mostHelpful/json, 50 reviews a page, `appstore.pages` (2) pages. Apple serves at most
     10 pages and answers HTTP 400 past the last one; an empty page or that error ends paging.

Terms (read 2026-11-26): the Search API page (performance-partners.apple.com/search-api) says it "is
limited to approximately 20 calls per minute (subject to change)" and asks large sites to cache; its only
attribution and no-caching rules concern music and video previews, not app data. The review feed is an
Apple-published RSS/JSON feed with no terms page of its own. Apple's website terms forbid robots and
page-scraping of www.apple.com pages; this collector reads only the API and the feed, never an App Store
web page. The tool waits `delay_seconds` (1) between requests and common.get_json backs off on 429.

Record mapping: title = the review title with the app name in brackets ("Battery drain [Strava: Run,
Bike, Walk]"), body = "(2/5 stars) " and the review text (low stars are the complaint signal, so the rating
leads), score = im:voteCount (readers who marked the review helpful), created_at = updated (the review's
date, or its last edit), url = https://apps.apple.com/<country>/app/id<appId>?see-all=reviews&review=<id>
(the app's reviews page; Apple publishes no page per review, so the review id rides along as a query
parameter Apple ignores, which keeps each review a distinct row: the deduper merges rows whose URLs
match once the fragment is dropped). comments, views, last_activity and status are empty: the feed has
none of them. Reviews dated before `since` are skipped. One app's reviews describe one product, so config
caps App Store rows at Moderate (scoring.label_caps.appstore).
"""

from __future__ import annotations

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

SOURCE = "appstore"
ATTRIBUTION = (
    "App Store rows come from Apple's public iTunes Search API and customer-review feeds. Reviews belong to "
    "their authors; helpful votes count the readers of one app's reviews who agreed, not how many people share "
    "the problem. Each row links to the app's reviews page on apps.apple.com."
)
REVIEWS_URL = "https://apps.apple.com/{country}/app/id{app_id}?see-all=reviews&review={review_id}"


def _label(value) -> str:
    """The feed wraps every value as {"label": "..."}. Returns the text, or "" when absent."""
    if isinstance(value, dict):
        value = value.get("label")
    return str(value).strip() if value is not None else ""


def _int(text: str):
    """An integer from feed text, or "" when it is not one (never guess a count)."""
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return ""


def _delay(cfg: dict) -> None:
    time.sleep(float(cfg["appstore"].get("delay_seconds", 1.0)))


def search_params(cfg: dict, query: str) -> dict:
    """Parameters for one iTunes Search API call: the words, software only, the store country."""
    a = cfg["appstore"]
    return {
        "term": " ".join(query.split()),
        "entity": "software",
        "limit": max(1, min(int(a.get("lookup_limit", 5)), 200)),
        "country": str(a.get("country", "us")),
    }


def _apps_from(data, limit: int) -> list[dict]:
    """[{"id": "426826309", "name": "Strava: Run, Bike, Walk"}, ...] from a Search or Lookup answer."""
    apps: list[dict] = []
    results = data.get("results") if isinstance(data, dict) else None
    for item in results or []:
        if not isinstance(item, dict) or item.get("trackId") is None:
            continue
        name = str(item.get("trackName") or "").strip() or f"app {item['trackId']}"
        apps.append({"id": str(item["trackId"]), "name": name})
        if len(apps) >= limit:
            break
    return apps


def find_apps(session: requests.Session, cfg: dict, query: str) -> list[dict]:
    """The first appstore.apps apps the iTunes Search API returns for the words."""
    a = cfg["appstore"]
    data, _ = get_json(session, str(a["search_endpoint"]), params=search_params(cfg, query))
    apps = _apps_from(data, max(1, int(a.get("apps", 3))))
    log.info("appstore %r: %d apps (%s)", query, len(apps), ", ".join(x["name"] for x in apps) or "none")
    return apps


def lookup_apps(session: requests.Session, cfg: dict, ids: list[str]) -> list[dict]:
    """Names for the ids given with --apps, in the order given, from one Lookup API call. An id the
    lookup does not return (or a failed lookup) keeps the id as its name, so the reviews are still read.
    An id that is not all digits cannot be an App Store id and is refused before any request."""
    a = cfg["appstore"]
    wanted = [str(i).strip() for i in ids if str(i).strip()]
    bad = [i for i in wanted if not i.isdigit()]
    if bad:
        raise ValueError(f"appstore: --apps takes numeric app ids (the number after 'id' in an apps.apple.com link), "
                         f"not {', '.join(bad)}")
    names: dict[str, str] = {}
    try:
        data, _ = get_json(session, str(a["lookup_endpoint"]),
                           params={"id": ",".join(wanted), "country": str(a.get("country", "us"))})
        names = {x["id"]: x["name"] for x in _apps_from(data, len(wanted))}
    except (RateLimited, RuntimeError, requests.HTTPError) as e:
        log.warning("appstore: lookup of %s failed (%s); using the ids as names", ",".join(wanted), e)
    return [{"id": i, "name": names.get(i, f"app {i}")} for i in wanted]


def choose_apps(session: requests.Session, cfg: dict, query: str, override: list[str] | None = None) -> tuple[list[dict], str]:
    """The apps to read and the header sentence saying which and why: --apps when given, else the iTunes
    Search API's matches for the question."""
    country = str(cfg["appstore"].get("country", "us"))
    if override:
        apps = lookup_apps(session, cfg, override)
        why = f"appstore read the most-helpful reviews of {_listed(apps)} (from --apps, {country} store)."
    else:
        apps = find_apps(session, cfg, query)
        if apps:
            why = (f"appstore read the most-helpful reviews of {_listed(apps)} (iTunes Search API matches for "
                   f"{query!r}, {country} store).")
        else:
            why = f"appstore found no app for {query!r} in the {country} store (pass --apps to name one)."
    return apps, why


def _listed(apps: list[dict]) -> str:
    return ", ".join(f"{x['name']} (id {x['id']})" for x in apps)


def feed_url(cfg: dict, app_id: str, page: int) -> str:
    a = cfg["appstore"]
    return str(a["reviews_endpoint"]).format(country=str(a.get("country", "us")), app_id=app_id, page=page,
                                            sort=str(a.get("sort", "mostHelpful")))


def _entries(data) -> list[dict]:
    """The feed's reviews: `feed.entry` is a list, a single object when there is one review, or absent."""
    feed = data.get("feed") if isinstance(data, dict) else None
    if not isinstance(feed, dict):
        return []
    entries = feed.get("entry")
    if isinstance(entries, dict):
        entries = [entries]
    return [e for e in entries or [] if isinstance(e, dict)]


def fetch_reviews(session: requests.Session, cfg: dict, app: dict, pages: int) -> list[dict]:
    """Up to `pages` pages of one app's most-helpful reviews, each tagged with the app. Paging ends at an
    empty page or at the HTTP 400 Apple sends past its last page (10). An error on the first page skips
    the app with a warning rather than failing the source."""
    reviews: list[dict] = []
    for page in range(1, max(1, int(pages)) + 1):
        if page > 1:
            _delay(cfg)
        try:
            data, _ = get_json(session, feed_url(cfg, app["id"], page))
        except requests.HTTPError as e:
            if page == 1:
                log.warning("appstore: app %s (%s) skipped: %s", app["id"], app["name"], e)
            else:
                log.info("appstore: app %s page %d is past the last page (%s)", app["id"], page, e)
            break
        batch = _entries(data)
        log.info("appstore %s (%s) page %d: %d reviews", app["id"], app["name"][:40], page, len(batch))
        if not batch:
            break
        for entry in batch:
            reviews.append(dict(entry, _app_id=app["id"], _app_name=app["name"]))
    return reviews


def parse_date(text: str) -> datetime | None:
    """The feed's `updated` ("2020-12-26T17:03:25-07:00") as an aware datetime, or None."""
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def to_record(entry: dict, fetched_at: str, body_max_chars: int, country: str, since: datetime | None = None,
              min_votes: int = 0) -> dict | None:
    """One feed entry to the 11-field record. None when the review has no id or text, is older than
    `since`, or has fewer helpful votes than min_votes."""
    review_id = _label(entry.get("id"))
    text = _label(entry.get("content"))
    if not review_id or not text:
        return None
    created = _label(entry.get("updated"))
    if since is not None:
        when = parse_date(created)
        if when is not None and when < (since if since.tzinfo else since.replace(tzinfo=timezone.utc)):
            return None
    votes = _int(_label(entry.get("im:voteCount")))
    if min_votes and (votes == "" or votes < min_votes):
        return None
    rating = _label(entry.get("im:rating"))
    stars = f"({rating}/5 stars) " if rating else ""
    title = _label(entry.get("title")) or text.split("\n", 1)[0][:120]
    return make_record(
        source=SOURCE,
        url=REVIEWS_URL.format(country=country, app_id=entry.get("_app_id", ""), review_id=review_id),
        title=f"{title} [{entry.get('_app_name', '')}]".strip(),
        body=(stars + text)[:body_max_chars],
        score=votes,
        comments="",  # the feed has no reply count
        views="",
        created_at=created,
        last_activity="",  # the feed has no activity date beyond the review's own; do not guess
        status="",
        fetched_at=fetched_at,
    )


def reviews_to_records(reviews: list[dict], fetched_at: str, cfg: dict, since: datetime | None = None,
                       min_votes: int = 0) -> list[dict]:
    """Validate, dedupe by URL, sort most helpful first."""
    a = cfg["appstore"]
    country = str(a.get("country", "us"))
    body_max = int(a.get("body_max_chars", 2000))
    records = []
    bad = skipped = 0
    for entry in reviews:
        rec = to_record(entry, fetched_at, body_max, country, since, min_votes)
        if rec is None:
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
        log.info("appstore: %d reviews skipped (no text, before the since date, or under the vote floor)", skipped)
    if bad:
        log.warning("appstore: %d invalid reviews skipped", bad)
    return sorted(by_url.values(), key=lambda r: (-(r["score"] if r["score"] != "" else float("-inf")), r["url"]))


def fetch_all(session: requests.Session, cfg: dict, apps: list[dict], pages: int) -> list[dict]:
    reviews: list[dict] = []
    for app in apps:
        _delay(cfg)  # one request already went out for the app list
        reviews.extend(fetch_reviews(session, cfg, app, pages))
    return reviews


def search(session: requests.Session, cfg: dict, query: str, since: datetime, **options) -> list[dict]:
    """One question in, validated records out. Options: apps (list[str]): app ids to read instead of
    looking them up (the --apps flag); info (list[str]): when given, the sentence naming the apps read
    and why is appended, so ppf.py prints it in the header and meta.note. Other options are ignored."""
    a = cfg["appstore"]
    if not " ".join(query.split()) and not options.get("apps"):
        raise ValueError("appstore: the question has no words")
    apps, why = choose_apps(session, cfg, query, options.get("apps"))
    log.info("%s", why)
    if options.get("info") is not None:
        options["info"].append(why)
    reviews = fetch_all(session, cfg, apps, int(a.get("pages", 2)))
    records = reviews_to_records(reviews, now_iso(), cfg, since, int(a.get("search_min_votes", 0)))
    log.info("appstore %r: %d records from %d reviews (since %s)", query, len(records), len(reviews),
             since.date() if since else "any date")
    return records


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    cfg = load_config()
    a = cfg["appstore"]
    since = datetime.now(timezone.utc) - timedelta(days=30.4375 * cfg["months_back"])
    session = requests.Session()
    fetched_at = now_iso()
    reviews: list[dict] = []
    seen: set[str] = set()
    # Sweep: every topic keyword's apps, each read once.
    for i, keyword in enumerate(cfg["keywords"]):
        if i:
            _delay(cfg)
        try:
            apps = [x for x in find_apps(session, cfg, keyword) if x["id"] not in seen]
            seen.update(x["id"] for x in apps)
            reviews.extend(fetch_all(session, cfg, apps, int(a.get("pages", 2))))
        except (requests.HTTPError, RateLimited, RuntimeError) as e:
            log.error("appstore %r: %s", keyword, e)
    records = reviews_to_records(reviews, fetched_at, cfg, since, int(a.get("min_votes", 0)))

    out = f"{cfg['paths']['raw_dir']}/appstore.jsonl"
    n = write_jsonl(out, records)
    log.info("wrote %d records to %s (since %s)", n, out, since.date())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
