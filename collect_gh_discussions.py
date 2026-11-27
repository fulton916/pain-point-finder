# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""GitHub Discussions source, via the GitHub GraphQL API, upvotes as the score (issue #33).

One way in:
    search(session, cfg, query, since)   one question across all of GitHub (or a repo list),
                                         returns records (used by ppf.py search). No sweep mode.

GitHub Discussions are where feature requests and "does anyone else have this problem" threads live
for many open-source projects, and each one carries an upvote count. The REST search API does not
cover them; the GraphQL API does, through search(type: DISCUSSION). Each page is one POST to
https://api.github.com/graphql with the search string `<words> in:title updated:>=<since>` (plus one
`repo:owner/name` per --repos entry, OR-ed by GitHub), `first: 100` and a cursor, up to
`github_discussions.max_pages` (3) pages.

Record: score = upvoteCount, comments = comments.totalCount, created_at = createdAt, last_activity =
updatedAt, status = `answered` when the discussion has an accepted answer, else empty, body = the first
`body_max_chars` of the discussion body. `answered` already weighs 0.5 in scoring.status_weights.

Needs GITHUB_TOKEN in .env: the GraphQL API refuses anonymous requests, so without one the source is
skipped with a sentence in the header and the other sources still run. The token travels only in the
Authorization header and is in common.SECRETS, so redact() masks it in every message.

Rate limit (docs.github.com/graphql/overview/rate-limits-and-node-limits-for-the-graphql-api, read
2026-11-27): a budget of 5,000 points per hour per token, separate from the REST budget; every answer
reports its cost in the `rateLimit` field (a search page costs 1 point) and the tool logs what is left.
When a page answers with a RATE_LIMITED error the tool waits for X-RateLimit-Reset (or Retry-After,
else a doubling pause, at most MAX_WAIT_SECONDS) and retries; HTTP 403 and 429 (the secondary limit) are
handled the same way, and a longer wait fails the source so the other sources still print.
The search string must stay under 256 characters, as for the REST search.
"""

from __future__ import annotations

import time
from datetime import datetime

import requests

from common import (
    RateLimited,
    STATS,
    USER_AGENT,
    _STATS_LOCK,
    _rate_limit_wait,
    env,
    log,
    make_record,
    now_iso,
    redact,
    validate_record,
)

SOURCE = "github_discussions"
API_VERSION = "2022-11-28"
MAX_QUERY_CHARS = 256
# Longest pause on a rate-limit answer. A spent budget resets within the hour; waiting that long would stall
# the whole parallel search, so past this the source fails with RateLimited and the others still print.
MAX_WAIT_SECONDS = 120.0
DEFAULT_ENDPOINT = "https://api.github.com/graphql"

# One page of discussions plus what the call cost. `$after` is null on the first page.
GRAPHQL_QUERY = """
query($q: String!, $first: Int!, $after: String) {
  rateLimit { limit cost remaining resetAt }
  search(type: DISCUSSION, query: $q, first: $first, after: $after) {
    discussionCount
    pageInfo { hasNextPage endCursor }
    nodes {
      ... on Discussion {
        title
        url
        body
        upvoteCount
        createdAt
        updatedAt
        comments { totalCount }
        answer { id }
      }
    }
  }
}
""".strip()


def headers() -> dict[str, str]:
    """Request headers with the token, or without the Authorization line when there is none."""
    h = {"User-Agent": USER_AGENT, "Accept": "application/json", "X-GitHub-Api-Version": API_VERSION}
    token = env("GITHUB_TOKEN")
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def build_search_query(query: str, since: datetime | None, repos: list[str], search_in: str = "title") -> str:
    """Free-text words (all must match, in the title by default) plus qualifiers, as collect_gh builds
    its issue query. Raises when over GitHub's 256-character limit."""
    words = " ".join(query.replace('"', " ").split())
    if not words:
        raise ValueError("github_discussions: the question has no words")
    q = f"{words} in:{search_in}"
    if since is not None:
        q += f" updated:>={since.date().isoformat()}"
    for repo in repos:
        q += f" repo:{repo}"  # several repo: qualifiers are OR-ed by GitHub
    if len(q) > MAX_QUERY_CHARS:
        raise ValueError(
            f"GitHub Discussions query is {len(q)} characters, limit is {MAX_QUERY_CHARS}. "
            "Shorten the question or the repo list."
        )
    return q


def _rate_limited(data: dict) -> bool:
    return any(isinstance(e, dict) and e.get("type") == "RATE_LIMITED" for e in data.get("errors") or [])


def _wait_or_give_up(resp, delay: float, what: str) -> float:
    """Seconds to pause before retrying a rate-limit answer, or RateLimited when the reset is too far off."""
    wait = _rate_limit_wait(resp) or delay
    if wait > MAX_WAIT_SECONDS:
        raise RateLimited(redact(f"github_discussions: {what}; GitHub asks to wait {wait:.0f}s, more than the "
                                 f"{MAX_WAIT_SECONDS:.0f}s this tool will pause. Try again later."))
    return wait


def post_graphql(session: requests.Session, endpoint: str, variables: dict, max_tries: int = 6,
                 timeout: int = 30) -> dict:
    """POST one GraphQL query and return its `data`. Retries connection errors and 5xx with a doubling
    pause; on HTTP 403/429 or a RATE_LIMITED error waits for X-RateLimit-Reset or Retry-After (else the
    pause) and retries, raising RateLimited when the tries run out. Any other GraphQL error raises
    RuntimeError with the API's message. Messages pass through redact(), so the token never prints."""
    payload = {"query": GRAPHQL_QUERY, "variables": variables}
    delay = 2.0
    last_err = "no attempts made"
    for attempt in range(1, max_tries + 1):
        last_try = attempt == max_tries
        try:
            with _STATS_LOCK:
                STATS["requests"] += 1
            resp = session.post(endpoint, json=payload, headers=headers(), timeout=timeout)
        except requests.RequestException as e:
            last_err = redact(str(e))
            if not last_try:
                log.warning("github_discussions: request failed (%s), retry %d/%d in %.0fs", last_err, attempt,
                            max_tries, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            continue

        if resp.status_code in (403, 429):
            if last_try:
                raise RateLimited(redact(f"github_discussions: gave up after {max_tries} tries: "
                                         f"HTTP {resp.status_code} {resp.text[:200]}"))
            wait = _wait_or_give_up(resp, delay, f"HTTP {resp.status_code} {resp.text[:200]}")
            log.warning("github_discussions: HTTP %s (rate limit remaining=%s). Waiting %.0fs, retry %d/%d",
                        resp.status_code, resp.headers.get("X-RateLimit-Remaining"), wait, attempt, max_tries)
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue

        if resp.status_code >= 500:
            last_err = f"HTTP {resp.status_code}"
            if not last_try:
                log.warning("github_discussions: %s, retry %d/%d in %.0fs", last_err, attempt, max_tries, delay)
                time.sleep(delay)
                delay = min(delay * 2, 60)
            continue

        if resp.status_code >= 400:
            # 401 is a bad or expired token. The message never carries the token (it is a header).
            raise RuntimeError(redact(f"github_discussions: HTTP {resp.status_code} {resp.reason} from the GraphQL "
                                      f"API: {resp.text[:200]}"))

        try:
            data = resp.json()
        except ValueError:
            raise RuntimeError("github_discussions: the GraphQL answer was not JSON") from None
        if not isinstance(data, dict):
            raise RuntimeError("github_discussions: the GraphQL answer was not an object")

        if _rate_limited(data):
            if last_try:
                raise RateLimited(f"github_discussions: gave up after {max_tries} tries: the GraphQL budget is spent "
                                  f"(RATE_LIMITED; it refills within the hour)")
            wait = _wait_or_give_up(resp, delay, "the GraphQL budget is spent (RATE_LIMITED)")
            log.warning("github_discussions: RATE_LIMITED (GraphQL points spent). Waiting %.0fs, retry %d/%d",
                        wait, attempt, max_tries)
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue

        if data.get("errors"):
            messages = "; ".join(str(e.get("message", e)) for e in data["errors"] if isinstance(e, dict)) or "unknown"
            raise RuntimeError(redact(f"github_discussions: GraphQL error: {messages}"))
        return data.get("data") or {}
    raise RuntimeError(f"github_discussions: gave up after {max_tries} tries: {last_err}")


def fetch_nodes(session: requests.Session, cfg: dict, q: str, max_pages: int, label: str,
                notes: list[str] | None = None) -> list[dict]:
    """Up to max_pages pages of discussions for one search string, following the cursor. Logs the
    rateLimit field of every answer; when what is left cannot pay for another page, stops and says so."""
    gd = cfg["github_discussions"]
    endpoint = gd.get("endpoint", DEFAULT_ENDPOINT)
    first = int(gd.get("per_page", 100))
    nodes: list[dict] = []
    after = None
    for page in range(1, max_pages + 1):
        data = post_graphql(session, endpoint, {"q": q, "first": first, "after": after},
                            max_tries=int(gd.get("max_tries", 6)))
        search = data.get("search") or {}
        page_nodes = [n for n in (search.get("nodes") or []) if isinstance(n, dict) and n]
        nodes.extend(page_nodes)
        limit = data.get("rateLimit") or {}
        total = search.get("discussionCount", 0)
        log.info("github_discussions %s page %d: %d discussions (total reported %s); GraphQL points: cost %s, "
                 "%s of %s left, reset %s", label, page, len(page_nodes), total, limit.get("cost"),
                 limit.get("remaining"), limit.get("limit"), limit.get("resetAt"))
        info = search.get("pageInfo") or {}
        after = info.get("endCursor")
        if not info.get("hasNextPage") or not after or not page_nodes:
            break
        # Stop before a page the budget cannot pay for: the next page costs about what this one did.
        remaining, cost = limit.get("remaining"), limit.get("cost")
        if isinstance(remaining, int) and remaining < max(int(cost or 1), 1):
            why = (f"github_discussions kept page {page} only ({len(nodes)} discussions): the GraphQL budget is spent "
                   f"until {limit.get('resetAt') or 'the hour turns'}.")
            log.warning("%s", why)
            if notes is not None:
                notes.append(why)
            break
        if page < max_pages:
            time.sleep(float(gd.get("delay_seconds", 1.0)))
    return nodes


def to_record(node: dict, fetched_at: str, body_max_chars: int) -> dict:
    comments = node.get("comments") or {}
    return make_record(
        source=SOURCE,
        url=node.get("url") or "",
        title=node.get("title") or "",
        body=(node.get("body") or "")[:body_max_chars],
        score=node.get("upvoteCount"),
        comments=comments.get("totalCount"),
        views="",  # GitHub does not expose discussion view counts
        created_at=node.get("createdAt") or "",
        last_activity=node.get("updatedAt") or "",
        status="answered" if node.get("answer") else "",
        fetched_at=fetched_at,
    )


def nodes_to_records(nodes: list[dict], fetched_at: str, body_max_chars: int, min_upvotes: int = 0) -> list[dict]:
    """Validate, apply the upvote floor, dedupe by URL, sort most upvoted first."""
    records = []
    bad = 0
    for node in nodes:
        rec = to_record(node, fetched_at, body_max_chars)
        problems = validate_record(rec)
        if problems:
            bad += 1
            log.warning("skipping invalid record %s: %s", rec.get("url"), problems)
            continue
        if min_upvotes and (rec["score"] == "" or rec["score"] < min_upvotes):
            continue
        records.append(rec)
    if bad:
        log.warning("github_discussions: %d invalid items skipped", bad)
    by_url = {r["url"]: r for r in records}
    return sorted(by_url.values(), key=lambda r: (-(r["score"] or 0), r["url"]))


def search(session: requests.Session, cfg: dict, query: str, since: datetime, **options) -> list[dict]:
    """One question in, validated records out. Options: repos (list; empty means all of GitHub, shared
    with the issues source through --repos); notes (list[str]): when the token is missing, the sentence
    saying the source was skipped is appended, so ppf.py prints it in the header and meta.note and does
    not cache the partial answer. Other options belong to other sources."""
    gd = cfg["github_discussions"]
    if not env("GITHUB_TOKEN"):
        why = ("github_discussions skipped: GITHUB_TOKEN is empty in .env (the GraphQL API needs one; see README, "
               "Getting a GitHub token).")
        log.warning("%s", why)
        if options.get("notes") is not None:
            options["notes"].append(why)
        return []
    repos = options.get("repos")
    if repos is None:
        repos = gd.get("search_repos") or []
    q = build_search_query(query, since, list(repos), gd.get("search_in", "title"))
    nodes = fetch_nodes(session, cfg, q, int(gd.get("max_pages", 3)), repr(query), options.get("notes"))
    records = nodes_to_records(nodes, now_iso(), int(gd.get("body_max_chars", 2000)), int(gd.get("search_min_upvotes", 0)))
    log.info("github_discussions %r: %d records (repos=%s, since %s)", query, len(records), repos or "all",
             since.date() if since else "any date")
    return records
