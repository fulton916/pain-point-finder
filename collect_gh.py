"""GitHub issues source, via the REST search API, sorted by reactions.

Two ways in:
    search(session, cfg, query, since)   one question across all of GitHub (or a repo list),
                                         returns records (used by ppf.py search)
    python collect_gh.py                 sweep mode: the configured repos, writes data/raw/github.jsonl
    python collect_gh.py --check-token   test the token

Docs: https://docs.github.com/rest/search/search#search-issues-and-pull-requests
Needs GITHUB_TOKEN in .env (a read-only fine-grained token). Works without one at a much
lower rate limit (10 searches/minute instead of 30). Queries must stay under 256 characters.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime

import requests

from common import (
    RateLimited,
    env,
    get_json,
    load_config,
    load_env,
    log,
    make_record,
    now_iso,
    setup_logging,
    validate_record,
    write_jsonl,
)

SOURCE = "github"
API_VERSION = "2022-11-28"


def headers() -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": API_VERSION}
    token = env("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    else:
        log.warning("GITHUB_TOKEN is empty: running unauthenticated (10 searches/minute)")
    return h


def build_query(repo: str, min_reactions: int, state: str) -> str:
    # One repo per query keeps every query far below the 256-character and
    # five-operator limits of the search API.
    q = f"repo:{repo} is:issue reactions:>={min_reactions}"
    if state in ("open", "closed"):
        q += f" state:{state}"
    return q


def check_token(cfg: dict) -> int:
    """Run one tiny search and report whether the token works and what limits apply."""
    gh = cfg["github"]
    session = requests.Session()
    params = {"q": build_query(gh["repos"][0], gh["min_reactions"], gh["state"]), "per_page": 1}
    try:
        data, resp = get_json(session, gh["endpoint"], params=params, headers=headers(), max_tries=1)
    except (requests.HTTPError, RateLimited) as e:
        print(f"FAILED: {e}")
        print("401 means the token is wrong or expired. 403 means it lacks access or you are rate limited. Check .env.")
        return 1
    auth = "token accepted" if env("GITHUB_TOKEN") else "no token (unauthenticated)"
    print(f"OK: {auth}")
    print(f"  search results available: {data.get('total_count')}")
    print(f"  search rate limit: {resp.headers.get('X-RateLimit-Limit')} per minute, "
          f"{resp.headers.get('X-RateLimit-Remaining')} remaining")
    return 0


MAX_QUERY_CHARS = 256


def build_search_query(query: str, min_reactions: int, since: datetime | None, repos: list[str],
                       search_in: str = "title") -> str:
    """Free-text words (all must match, in the title by default) plus qualifiers.
    Raises when over GitHub's 256-character limit. Without in:title, GitHub also matches
    comments, which returned unrelated repos in the first live run (2026-11-24)."""
    words = " ".join(query.replace('"', " ").split())
    q = f"{words} in:{search_in} is:issue reactions:>={min_reactions}"
    if since is not None:
        q += f" updated:>={since.date().isoformat()}"
    for repo in repos:
        q += f" repo:{repo}"  # several repo: qualifiers are OR-ed by GitHub
    if len(q) > MAX_QUERY_CHARS:
        raise ValueError(
            f"GitHub query is {len(q)} characters, limit is {MAX_QUERY_CHARS}. Shorten the question or the repo list."
        )
    return q


def fetch_items(session: requests.Session, cfg: dict, q: str, max_pages: int, label: str) -> list[dict]:
    gh = cfg["github"]
    items: list[dict] = []
    data: dict = {}
    for page in range(1, max_pages + 1):
        params = {
            "q": q,
            "sort": "reactions",
            "order": "desc",
            "per_page": gh["per_page"],
            "page": page,
        }
        data, _ = get_json(session, gh["endpoint"], params=params, headers=headers())
        page_items = data.get("items", [])
        items.extend(page_items)
        total = data.get("total_count", 0)
        log.info("github %s page %d: %d issues (total reported %d)", label, page, len(page_items), total)
        if len(items) >= total or not page_items:
            break
        time.sleep(gh["delay_seconds"])
    if data.get("incomplete_results"):
        log.warning("github %s: incomplete results reported (search timed out server side)", label)
    return items


def search_repo(session: requests.Session, cfg: dict, repo: str) -> list[dict]:
    gh = cfg["github"]
    return fetch_items(session, cfg, build_query(repo, gh["min_reactions"], gh["state"]), gh["max_pages"], repo)


def items_to_records(items: list[dict], fetched_at: str, body_max_chars: int) -> list[dict]:
    """Drop pull requests, validate, dedupe by URL, sort best first."""
    records = []
    bad = 0
    for item in items:
        rec = to_record(item, fetched_at, body_max_chars)
        if not rec:
            continue
        problems = validate_record(rec)
        if problems:
            bad += 1
            log.warning("skipping invalid record %s: %s", rec.get("url"), problems)
            continue
        records.append(rec)
    by_url = {r["url"]: r for r in records}
    if bad:
        log.warning("github: %d invalid items skipped", bad)
    return sorted(by_url.values(), key=lambda r: (-(r["score"] or 0), r["url"]))


def search(session: requests.Session, cfg: dict, query: str, since: datetime, **options) -> list[dict]:
    """One question in, validated records out. Options: repos (list; empty means all of GitHub)."""
    gh = cfg["github"]
    repos = options.get("repos")
    if repos is None:
        repos = gh.get("search_repos") or []
    q = build_search_query(query, int(gh.get("search_min_reactions", 1)), since, list(repos), gh.get("search_in", "title"))
    items = fetch_items(session, cfg, q, int(gh.get("search_max_pages", 3)), repr(query))
    records = items_to_records(items, now_iso(), gh["body_max_chars"])
    log.info("github %r: %d records (repos=%s, since %s)", query, len(records), repos or "all", since.date())
    return records


def to_record(item: dict, fetched_at: str, body_max_chars: int) -> dict:
    if "pull_request" in item:
        return {}  # safety net; is:issue should already exclude pull requests
    reactions = item.get("reactions") or {}
    body = (item.get("body") or "")[:body_max_chars]
    return make_record(
        source=SOURCE,
        url=item.get("html_url") or "",
        title=item.get("title") or "",
        body=body,
        score=reactions.get("total_count"),
        comments=item.get("comments"),
        views="",  # GitHub does not expose issue view counts
        created_at=item.get("created_at") or "",
        last_activity=item.get("updated_at") or "",
        status=item.get("state") or "",
        fetched_at=fetched_at,
    )


def main(argv: list[str]) -> int:
    setup_logging()
    load_env()
    cfg = load_config()
    if "--check-token" in argv:
        return check_token(cfg)

    gh = cfg["github"]
    fetched_at = now_iso()
    session = requests.Session()
    items: list[dict] = []
    for repo in gh["repos"]:
        items.extend(search_repo(session, cfg, repo))
        time.sleep(gh["delay_seconds"])
    records = items_to_records(items, fetched_at, gh["body_max_chars"])

    out = f"{cfg['paths']['raw_dir']}/github.jsonl"
    n = write_jsonl(out, records)
    log.info("wrote %d records to %s (%d repos)", n, out, len(gh["repos"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
