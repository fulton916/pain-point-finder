"""Collect GitHub issues from the configured repos, sorted by reactions.

Uses the GitHub REST search API (https://docs.github.com/rest/search/search#search-issues-and-pull-requests).
Needs GITHUB_TOKEN in .env (a read-only fine-grained token). Works without one at a much
lower rate limit (10 searches/minute instead of 30).

Writes data/raw/github.jsonl in the common record format.

Run:  python collect_gh.py
Test the token first:  python collect_gh.py --check-token
"""

from __future__ import annotations

import sys
import time

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


def search_repo(session: requests.Session, cfg: dict, repo: str) -> list[dict]:
    gh = cfg["github"]
    items: list[dict] = []
    data: dict = {}
    q = build_query(repo, gh["min_reactions"], gh["state"])
    for page in range(1, gh["max_pages"] + 1):
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
        log.info("%s page %d: %d issues (total reported %d)", repo, page, len(page_items), total)
        if len(items) >= total or not page_items:
            break
        time.sleep(gh["delay_seconds"])
    if data.get("incomplete_results"):
        log.warning("%s: GitHub reported incomplete results (search timed out server side)", repo)
    return items


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
    records = []
    bad = 0
    for repo in gh["repos"]:
        for item in search_repo(session, cfg, repo):
            rec = to_record(item, fetched_at, gh["body_max_chars"])
            if not rec:
                continue
            problems = validate_record(rec)
            if problems:
                bad += 1
                log.warning("skipping invalid record %s: %s", rec.get("url"), problems)
                continue
            records.append(rec)
        time.sleep(gh["delay_seconds"])

    # Dedupe by URL in case a repo is listed twice, then sort for a stable file.
    by_url = {r["url"]: r for r in records}
    records = sorted(by_url.values(), key=lambda r: (-(r["score"] or 0), r["url"]))

    out = f"{cfg['paths']['raw_dir']}/github.jsonl"
    n = write_jsonl(out, records)
    log.info("wrote %d records to %s (%d skipped, %d repos)", n, out, bad, len(gh["repos"]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
