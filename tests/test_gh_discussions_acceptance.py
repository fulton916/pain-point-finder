# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Acceptance tests for issue #33: GitHub Discussions as a search source (`gd`), via the GraphQL API.

One test class per "Done when" line, in the issue's order, plus one class of supporting checks for the
work steps (registration, config, rate-limit backoff, the pause between pages, the docs). Everything runs
through ppf.main (the command the user types), offline: requests.Session.post (the GraphQL endpoint) and
requests.Session.get (the REST issues search and the Hacker News API) are replaced by a fake that answers
by URL, common.load_dotenv is mocked so no .env is read (common.load_env still runs, so the token is
registered as a secret the way a live run does it), GITHUB_TOKEN is set or cleared in os.environ per
test, time.sleep is patched out and the store is in memory. The fake payloads follow the field names the
issue lists (upvoteCount, comments.totalCount, createdAt, updatedAt, answer, url).

Written by the tester agent, not by the author of the feature.
"""

import copy
import io
import json
import logging
import re
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import requests

import collect_gh
import collect_gh_discussions
import common
import ppf
from common import ROOT, load_config

TOKEN = "ghp_acceptance_token_0123456789abcdefXYZ"
GRAPHQL = "https://api.github.com/graphql"
ISSUES = "https://api.github.com/search/issues"
HN = "https://hn.algolia.com/api/v1/search"

NOW = datetime.now(timezone.utc).replace(microsecond=0)


def iso(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(days_ago: int) -> str:
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%d")


SHARED_TITLE = "Bulk API 2.0 job times out on large loads"
DISC_URL = "https://github.com/jsforce/jsforce/discussions/{}"
ISSUE_URL = "https://github.com/jsforce/jsforce/issues/{}"


def discussion(title: str, number: int, upvotes: int, comments: int, answered: bool = False,
               body: str = "We load five million rows nightly and the job dies at hour two.",
               created: str = iso(200), updated: str = iso(70)) -> dict:
    """One node of search(type: DISCUSSION), the fields the issue names."""
    return {"title": title, "url": DISC_URL.format(number), "body": body, "upvoteCount": upvotes,
            "createdAt": created, "updatedAt": updated, "comments": {"totalCount": comments},
            "answer": {"id": f"DC_{number}"} if answered else None}


def graphql_page(nodes: list[dict], has_next: bool = False, cursor: str | None = None, remaining: int = 4990,
                 errors: list[dict] | None = None) -> dict:
    out = {"data": {
        "rateLimit": {"limit": 5000, "cost": 1, "remaining": remaining, "resetAt": "2026-11-27T01:00:00Z"},
        "search": {"discussionCount": len(nodes), "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                   "nodes": nodes},
    }}
    if errors:
        out["errors"] = errors
    return out


def issue(title: str, number: int, reactions: int, comments: int, state: str = "open",
          created: str = iso(220), updated: str = iso(60)) -> dict:
    return {"html_url": ISSUE_URL.format(number), "title": title, "body": "Same job, same failure.",
            "reactions": {"total_count": reactions}, "comments": comments, "created_at": created,
            "updated_at": updated, "state": state}


def issues_answer(items: list[dict]) -> dict:
    return {"total_count": len(items), "items": items}


HN_HIT = {"objectID": "41000001", "title": "Bulk API 2.0 is a trap for nightly loads", "url": "",
          "points": 80, "num_comments": 10, "created_at": iso(100), "story_text": ""}
HN_ANSWER = {"hits": [HN_HIT], "nbPages": 1}


class FakeResponse:
    def __init__(self, status: int, data=None, headers=None, text=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = text if text is not None else ("" if data is None else json.dumps(data)[:200])
        self.reason = {401: "Unauthorized", 403: "Forbidden", 429: "Too Many Requests", 502: "Bad Gateway"}.get(status, "OK")

    def json(self):
        if self._data is None:
            raise ValueError("no JSON")
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeGitHub:
    """Stands in for requests.Session.post (GraphQL) and requests.Session.get (REST issues, Hacker News).
    `posts` are answered in order; when they run out, an empty discussions page. Records every call."""

    def __init__(self, posts=None, issues=None, hn=None):
        self.posts = list(posts or [])
        self.issues = issues if issues is not None else issues_answer([])
        self.hn = hn if hn is not None else {"hits": [], "nbPages": 0}
        self.post_calls: list[dict] = []
        self.get_calls: list[tuple[str, dict]] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.post_calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        if url != GRAPHQL:
            raise AssertionError(f"unexpected POST to {url}")
        if not self.posts:
            return FakeResponse(200, graphql_page([]))
        answer = self.posts.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def get(self, url, params=None, headers=None, timeout=None):
        self.get_calls.append((url, dict(params or {})))
        if url == ISSUES:
            return FakeResponse(200, self.issues)
        if url == HN:
            return FakeResponse(200, self.hn)
        raise AssertionError(f"unexpected GET to {url}")

    @property
    def variables(self) -> list[dict]:
        return [c["json"]["variables"] for c in self.post_calls]


def test_config() -> dict:
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["store"] = ":memory:"
    return cfg


class CliRun:
    def __init__(self, rc: int, out: str, logs: str, fake: FakeGitHub, sleep: mock.Mock):
        self.rc, self.out, self.logs, self.fake, self.sleep = rc, out, logs, fake, sleep

    @property
    def header(self) -> str:
        for line in self.out.splitlines():
            if line.startswith("Sources:"):
                return line
        raise AssertionError(f"no 'Sources:' header line in:\n{self.out}")

    @property
    def since_date(self) -> str:
        m = re.search(r"Since (\d{4}-\d{2}-\d{2}) \(", self.header)
        assert m, self.header
        return m.group(1)

    @property
    def rows(self) -> list[str]:
        """The numbered result lines of the markdown output."""
        return [line for line in self.out.splitlines() if re.match(r"^\d+\. \*\*\[", line)]

    def bits_under(self, title_part: str) -> str:
        """The `**Label** · source · ...` line under the row whose title holds title_part."""
        lines = self.out.splitlines()
        for i, line in enumerate(lines):
            if re.match(r"^\d+\. \*\*\[", line) and title_part in line:
                return lines[i + 1]
        raise AssertionError(f"no row with {title_part!r} in:\n{self.out}")


def run_cli(argv: list[str], fake: FakeGitHub, token: str | None = TOKEN, cfg: dict | None = None) -> CliRun:
    """Run `python ppf.py <argv>` offline: in-memory store, no .env (but load_env still registers the token
    as a secret, as it does live), no network, no waiting, every log line of the ppf logger captured."""
    cfg = cfg or test_config()
    out, logbuf = io.StringIO(), io.StringIO()
    handler = logging.StreamHandler(logbuf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    old_level = common.log.level
    common.log.addHandler(handler)
    common.log.setLevel(logging.DEBUG)
    env = {"GITHUB_TOKEN": token or ""}
    try:
        with mock.patch.dict("os.environ", env), \
             mock.patch.object(common, "load_config", return_value=cfg), \
             mock.patch.object(common, "load_dotenv"), \
             mock.patch.object(requests.Session, "get", side_effect=fake.get), \
             mock.patch.object(requests.Session, "post", side_effect=fake.post), \
             mock.patch("time.sleep") as sleep, redirect_stdout(out):
            rc = ppf.main(argv)
    finally:
        common.log.removeHandler(handler)
        common.log.setLevel(old_level)
        common.SECRETS.discard(TOKEN)
    return CliRun(rc, out.getvalue(), logbuf.getvalue(), fake, sleep)


GD_ARGV = ["search", "jsforce bulk api", "--sources", "gd", "--since", "24m"]
TWO_DISCUSSIONS = [discussion(SHARED_TITLE, 1400, upvotes=12, comments=4, updated=iso(70)),
                   discussion("Bulk API: how to poll job status", 1388, upvotes=3, comments=2, answered=True,
                              updated=iso(300))]


class SearchGdReturnsDiscussionsWithUpvotesCommentsDatesAnsweredStatusAndLinks(unittest.TestCase):
    """Done when: `python ppf.py search "jsforce bulk api" --sources gd --since 24m` returns discussions
    with upvote counts, comment counts, dates, answered status and links."""

    def setUp(self):
        self.run = run_cli(GD_ARGV, FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))]))
        self.assertEqual(self.run.rc, 0, self.run.out + self.run.logs)

    def test_one_graphql_post_asks_for_discussions_with_the_words_in_the_title_since_the_window_start(self):
        fake = self.run.fake
        self.assertEqual(len(fake.post_calls), 1, fake.post_calls)
        self.assertEqual(fake.post_calls[0]["url"], GRAPHQL)
        sent = fake.post_calls[0]["json"]
        self.assertIn("search(type: DISCUSSION", sent["query"])
        self.assertEqual(sent["variables"], {"q": f"jsforce bulk api in:title updated:>={self.run.since_date}",
                                             "first": 100, "after": None})
        self.assertEqual(fake.post_calls[0]["headers"]["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(fake.get_calls, [], "gd alone must send no REST request")

    def test_rows_show_upvotes_comments_dates_answered_status_and_links(self):
        run = self.run
        self.assertEqual(len(run.rows), 2, run.out)
        self.assertIn("github_discussions (2 records, fetched)", run.header)
        self.assertIn(f"]({DISC_URL.format(1400)})", run.rows[0])
        self.assertIn(f"]({DISC_URL.format(1388)})", run.rows[1])
        open_bits = run.bits_under(SHARED_TITLE)
        self.assertIn("github_discussions", open_bits)
        self.assertIn("12 upvotes", open_bits)
        self.assertIn("4 comments", open_bits)
        self.assertIn(f"date {day(70)}", open_bits)
        self.assertNotIn("answered", open_bits)
        answered_bits = run.bits_under("how to poll job status")
        self.assertIn("3 upvotes", answered_bits)
        self.assertIn("2 comments", answered_bits)
        self.assertIn(f"date {day(300)}", answered_bits)
        self.assertTrue(answered_bits.rstrip().endswith("answered"), answered_bits)
        self.assertNotIn("no date", run.out)

    def test_json_carries_the_same_fields_per_row(self):
        run = run_cli(GD_ARGV + ["--format", "json"], FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))]))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        result = json.loads(run.out)
        self.assertEqual(result["meta"]["sources"], ["github_discussions"])
        self.assertEqual(result["meta"]["per_source"]["github_discussions"], {"records": 2, "from_cache": False})
        by_url = {r["best_url"]: r for r in result["rows"]}
        top = by_url[DISC_URL.format(1400)]
        self.assertEqual((top["raw_score"], top["comments"], top["latest_activity"], top["status"]),
                         (12, 4, day(70), ""))
        self.assertEqual(top["sources"], "github_discussions")
        self.assertEqual(top["best_source"], "github_discussions")
        self.assertTrue(top["snippet"].startswith("We load five million rows"))
        answered = by_url[DISC_URL.format(1388)]
        self.assertEqual((answered["raw_score"], answered["comments"], answered["latest_activity"], answered["status"]),
                         (3, 2, day(300), "answered"))

    def test_compact_prints_one_line_per_discussion_with_upvotes_date_status_title_and_url(self):
        run = run_cli(GD_ARGV + ["--format", "compact"], FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))]))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertIn(f"| github_discussions | 12 upvotes | {day(70)} | - | {SHARED_TITLE} | {DISC_URL.format(1400)}", run.out)
        self.assertIn(f"| github_discussions | 3 upvotes | {day(300)} | answered | Bulk API: how to poll job status | "
                      f"{DISC_URL.format(1388)}", run.out)

    def test_an_answered_discussion_ranks_below_an_open_one_with_the_same_upvotes(self):
        nodes = [discussion("Open: bulk api job stuck", 10, upvotes=10, comments=2, updated=iso(50)),
                 discussion("Answered: bulk api job stuck", 11, upvotes=10, comments=2, answered=True, updated=iso(50))]
        run = run_cli(GD_ARGV + ["--format", "json"], FakeGitHub(posts=[FakeResponse(200, graphql_page(nodes))]))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        rows = json.loads(run.out)["rows"]
        self.assertEqual([r["status"] for r in rows], ["", "answered"])

    def test_repos_narrows_the_search_with_repo_terms_as_for_gh(self):
        run = run_cli(GD_ARGV + ["--repos", "jsforce/jsforce,forcedotcom/cli"],
                      FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))]))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        q = run.fake.variables[0]["q"]
        self.assertTrue(q.startswith("jsforce bulk api in:title updated:>="), q)
        self.assertTrue(q.endswith(" repo:jsforce/jsforce repo:forcedotcom/cli"), q)

    def test_paging_follows_the_cursor_for_at_most_max_pages_pages(self):
        pages = [FakeResponse(200, graphql_page([discussion(f"bulk api page {i}", i, upvotes=i, comments=0)],
                                                has_next=True, cursor=f"c{i}")) for i in range(1, 6)]
        run = run_cli(GD_ARGV + ["--format", "json"], FakeGitHub(posts=pages))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertEqual(len(run.fake.post_calls), load_config()["github_discussions"]["max_pages"])
        self.assertEqual([v["after"] for v in run.fake.variables], [None, "c1", "c2"])
        result = json.loads(run.out)
        self.assertEqual(result["meta"]["per_source"]["github_discussions"]["records"], 3)
        self.assertEqual(result["meta"]["requests_sent"], 3)

    def test_no_match_prints_no_results_without_an_error(self):
        run = run_cli(GD_ARGV, FakeGitHub(posts=[FakeResponse(200, graphql_page([]))]))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertIn("github_discussions (0 records, fetched)", run.header)
        self.assertIn("No results.", run.out)
        self.assertNotIn("failed", run.header)


class SourcesGhGdReturnsBothDedupedWhenAnIssueAndADiscussionShareATitle(unittest.TestCase):
    """Done when: `--sources gh,gd` returns both, deduped when an issue and a discussion share a title."""

    ARGV = ["search", "jsforce bulk api", "--sources", "gh,gd", "--since", "24m"]
    ISSUES = [issue(SHARED_TITLE, 1399, reactions=7, comments=3),
              issue("Bulk API ingest rejects CSV with a BOM", 1350, reactions=2, comments=1)]

    def fake(self) -> FakeGitHub:
        return FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))], issues=issues_answer(self.ISSUES))

    def setUp(self):
        self.run = run_cli(self.ARGV + ["--format", "json"], self.fake())
        self.assertEqual(self.run.rc, 0, self.run.out + self.run.logs)
        self.result = json.loads(self.run.out)

    def test_both_apis_are_asked_once_and_both_sources_report_their_records(self):
        fake = self.run.fake
        self.assertEqual([u for u, _ in fake.get_calls], [ISSUES])
        self.assertIn("jsforce bulk api in:title", fake.get_calls[0][1]["q"])
        self.assertEqual([c["url"] for c in fake.post_calls], [GRAPHQL])
        per = self.result["meta"]["per_source"]
        self.assertEqual(per["github"], {"records": 2, "from_cache": False})
        self.assertEqual(per["github_discussions"], {"records": 2, "from_cache": False})
        self.assertEqual(self.result["meta"]["sources"], ["github", "github_discussions"])

    def test_the_issue_and_the_discussion_with_one_title_are_one_row_from_both_sources_labeled_strong(self):
        meta = self.result["meta"]
        self.assertEqual((meta["records"], meta["rows"], meta["merges"]), (4, 3, 1))
        shared = [r for r in self.result["rows"] if r["title"] == SHARED_TITLE]
        self.assertEqual(len(shared), 1, [r["title"] for r in self.result["rows"]])
        row = shared[0]
        self.assertEqual(set(row["sources"].split(";")), {"github", "github_discussions"})
        self.assertEqual(row["evidence_label"], "Strong")
        # The row keeps the higher score and its link: the discussion's 12 upvotes over the issue's 7 reactions.
        self.assertEqual(row["raw_score"], 12)
        self.assertEqual(row["best_url"], DISC_URL.format(1400))
        self.assertEqual(row["best_source"], "github_discussions")

    def test_the_other_titles_stay_separate_rows_each_from_one_source(self):
        singles = {r["title"]: r["sources"] for r in self.result["rows"] if r["title"] != SHARED_TITLE}
        self.assertEqual(singles, {"Bulk API ingest rejects CSV with a BOM": "github",
                                   "Bulk API: how to poll job status": "github_discussions"})

    def test_markdown_shows_the_merged_row_as_github_plus_github_discussions(self):
        run = run_cli(self.ARGV, self.fake())
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertEqual(len(run.rows), 3, run.out)
        self.assertIn("github (2 records, fetched)", run.header)
        self.assertIn("github_discussions (2 records, fetched)", run.header)
        bits = run.bits_under(SHARED_TITLE)
        self.assertIn("**Strong**", bits)
        self.assertIn("github + github_discussions", bits)
        self.assertIn("12 upvotes", bits)
        self.assertEqual(run.out.count(SHARED_TITLE), 1, "the shared title prints once")


class WithoutGithubTokenTheHeaderSaysDiscussionsSkippedAndTheOtherSourcesStillPrint(unittest.TestCase):
    """Done when: without `GITHUB_TOKEN` the header says GitHub Discussions was skipped and the other
    sources still print."""

    ARGV = ["search", "jsforce bulk api", "--sources", "hn,gd", "--since", "24m"]

    def setUp(self):
        self.run = run_cli(self.ARGV, FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))], hn=HN_ANSWER),
                           token=None)
        self.assertEqual(self.run.rc, 0, self.run.out + self.run.logs)

    def test_the_header_says_github_discussions_was_skipped_for_want_of_the_token(self):
        header = self.run.header
        self.assertIn("github_discussions (0 records, fetched; github_discussions skipped: GITHUB_TOKEN", header)
        self.assertNotIn("failed", header)

    def test_no_graphql_request_is_sent_and_no_authorization_header_is_built(self):
        self.assertEqual(self.run.fake.post_calls, [], "the GraphQL API must not be asked without a token")
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": ""}):
            self.assertNotIn("Authorization", collect_gh_discussions.headers())

    def test_the_other_source_still_prints_its_rows(self):
        run = self.run
        self.assertEqual([u for u, _ in run.fake.get_calls], [HN])
        self.assertIn("hackernews (1 records, fetched)", run.header)
        self.assertEqual(len(run.rows), 1, run.out)
        self.assertIn("Bulk API 2.0 is a trap for nightly loads", run.rows[0])
        self.assertIn("80 points", run.bits_under("trap for nightly loads"))

    def test_json_marks_the_skip_as_a_note_not_an_error(self):
        run = run_cli(self.ARGV + ["--format", "json"],
                      FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))], hn=HN_ANSWER), token=None)
        self.assertEqual(run.rc, 0, run.out + run.logs)
        result = json.loads(run.out)
        gd = result["meta"]["per_source"]["github_discussions"]
        self.assertEqual((gd["records"], gd["from_cache"]), (0, False))
        self.assertNotIn("error", gd)
        self.assertIn("github_discussions skipped", gd["note"])
        self.assertIn("GITHUB_TOKEN", gd["note"])
        self.assertIn("github_discussions skipped", result["meta"]["note"])
        self.assertEqual([r["sources"] for r in result["rows"]], ["hackernews"])

    def test_compact_header_says_so_too(self):
        run = run_cli(self.ARGV + ["--format", "compact"],
                      FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))], hn=HN_ANSWER), token=None)
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertIn("github_discussions skipped: GITHUB_TOKEN", run.header)
        self.assertIn("| hackernews | 80 points |", run.out)

    def test_gd_alone_without_the_token_is_a_clean_empty_answer(self):
        run = run_cli(GD_ARGV, FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))]), token=None)
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertIn("github_discussions skipped: GITHUB_TOKEN", run.header)
        self.assertIn("No results.", run.out)
        self.assertEqual(run.fake.post_calls, [])


class TheTokenNeverAppearsInMarkdownJsonOrLogOutput(unittest.TestCase):
    """Done when: the token never appears in markdown, JSON or log output."""

    def assert_token_hidden(self, run: CliRun):
        self.assertNotIn(TOKEN, run.out, "the token is in the printed output")
        self.assertNotIn(TOKEN, run.logs, "the token is in a log line")

    def test_a_normal_run_in_all_three_formats_sends_the_token_only_as_a_header(self):
        for fmt in ("md", "json", "compact"):
            with self.subTest(format=fmt):
                run = run_cli(GD_ARGV + ["--format", fmt], FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS))]))
                self.assertEqual(run.rc, 0, run.out + run.logs)
                self.assertEqual(run.fake.post_calls[0]["headers"]["Authorization"], f"Bearer {TOKEN}")
                self.assertNotIn(TOKEN, json.dumps(run.fake.post_calls[0]["json"]), "the token is in the GraphQL payload")
                self.assert_token_hidden(run)

    def test_a_401_whose_body_echoes_the_token_is_redacted_in_the_header_and_the_logs(self):
        for fmt in ("md", "json", "compact"):
            with self.subTest(format=fmt):
                run = run_cli(GD_ARGV + ["--format", fmt],
                              FakeGitHub(posts=[FakeResponse(401, text=f"Bad credentials for {TOKEN}")]))
                self.assertEqual(run.rc, 2, "a failed only source with no rows exits 2")
                self.assert_token_hidden(run)
                self.assertIn("***", run.out, "the redaction marker should stand where the token was")
                self.assertIn("HTTP 401", run.out)

    def test_a_connection_error_naming_the_token_is_redacted(self):
        boom = [requests.ConnectionError(f"connection refused while sending {TOKEN}") for _ in range(6)]
        run = run_cli(GD_ARGV, FakeGitHub(posts=boom))
        self.assertEqual(run.rc, 2, run.out + run.logs)
        self.assertIn("github_discussions (failed:", run.header)
        self.assert_token_hidden(run)

    def test_a_graphql_error_message_naming_the_token_is_redacted(self):
        bad = FakeResponse(200, {"errors": [{"message": f"token {TOKEN} lacks the read:discussion scope"}]})
        run = run_cli(GD_ARGV + ["--format", "json"], FakeGitHub(posts=[bad]))
        self.assertEqual(run.rc, 2, run.out + run.logs)
        result = json.loads(run.out)
        self.assertIn("error", result["meta"]["per_source"]["github_discussions"])
        self.assertIn("***", result["meta"]["per_source"]["github_discussions"]["error"])
        self.assert_token_hidden(run)

    def test_a_rate_limit_message_naming_the_token_is_redacted(self):
        limited = [FakeResponse(403, text=f"secondary rate limit for {TOKEN}", headers={"Retry-After": "2"})
                   for _ in range(6)]
        run = run_cli(GD_ARGV, FakeGitHub(posts=limited))
        self.assertEqual(run.rc, 2, run.out + run.logs)
        self.assertIn("github_discussions (failed:", run.header)
        self.assert_token_hidden(run)

    def test_the_token_is_registered_as_a_secret_by_load_env(self):
        self.assertIn("GITHUB_TOKEN", common.SECRET_NAMES)
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": TOKEN}), mock.patch.object(common, "load_dotenv"):
            try:
                common.load_env()
                self.assertIn(TOKEN, common.SECRETS)
                self.assertEqual(common.redact(f"x {TOKEN} y"), "x *** y")
            finally:
                common.SECRETS.discard(TOKEN)


class WorkStepChecks(unittest.TestCase):
    """Supporting checks for the issue's work steps (not "Done when" lines): registration, config,
    rate-limit backoff through the CLI, the pause between pages, and the docs."""

    def test_gd_is_registered_with_upvotes_and_stays_out_of_the_default_sources(self):
        cfg = load_config()
        self.assertIs(ppf.SOURCES["gd"], collect_gh_discussions)
        self.assertEqual(collect_gh_discussions.SOURCE, "github_discussions")
        self.assertEqual(ppf.UNITS["github_discussions"], "upvotes")
        self.assertEqual(ppf.parse_sources("gh,gd"), ["gh", "gd"])
        self.assertEqual(ppf.parse_sources("github_discussions"), ["gd"])
        self.assertNotIn("gd", cfg["search"]["default_sources"])
        self.assertEqual(collect_gh.SOURCE, "github")

    def test_config_has_the_block_the_thresholds_and_the_answered_weight(self):
        cfg = load_config()
        gd = cfg["github_discussions"]
        self.assertEqual(gd["endpoint"], GRAPHQL)
        self.assertEqual(gd["max_pages"], 3)
        self.assertEqual(gd["per_page"], 100)
        self.assertEqual(cfg["scoring"]["source_thresholds"]["github_discussions"], {"moderate": 5, "strong": 20})
        self.assertEqual(cfg["scoring"]["status_weights"]["answered"], 0.5)

    def test_a_rate_limited_answer_is_waited_out_and_retried_through_the_cli(self):
        limited = FakeResponse(200, {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
                               headers={"Retry-After": "3"})
        run = run_cli(GD_ARGV, FakeGitHub(posts=[limited, FakeResponse(200, graphql_page(TWO_DISCUSSIONS))]))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertEqual(len(run.fake.post_calls), 2)
        self.assertIn(mock.call(4.0), run.sleep.call_args_list, "Retry-After 3 plus one second")
        self.assertIn("github_discussions (2 records, fetched)", run.header)
        self.assertIn("RATE_LIMITED", run.logs)

    def test_the_rate_limit_field_of_every_answer_is_logged(self):
        run = run_cli(GD_ARGV, FakeGitHub(posts=[FakeResponse(200, graphql_page(TWO_DISCUSSIONS, remaining=4321))]))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertIn("4321 of 5000 left", run.logs)

    def test_a_spent_budget_stops_paging_and_the_header_says_so(self):
        first = FakeResponse(200, graphql_page([discussion("bulk api one", 1, 5, 0)], has_next=True, cursor="c1", remaining=0))
        run = run_cli(GD_ARGV, FakeGitHub(posts=[first, FakeResponse(200, graphql_page([discussion("bulk api two", 2, 4, 0)]))]))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertEqual(len(run.fake.post_calls), 1)
        self.assertIn("budget is spent", run.header)
        self.assertEqual(len(run.rows), 1)

    def test_a_second_passes_between_pages_and_none_after_the_last(self):
        pages = [FakeResponse(200, graphql_page([discussion(f"bulk api page {i}", i, upvotes=i, comments=0)],
                                                has_next=True, cursor=f"c{i}")) for i in range(1, 4)]
        run = run_cli(GD_ARGV, FakeGitHub(posts=pages))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertEqual(len(run.fake.post_calls), 3)
        waits = [c for c in run.sleep.call_args_list if c.args and float(c.args[0]) >= 1.0]
        self.assertEqual(len(waits), 2, run.sleep.call_args_list)

    def test_readme_and_skill_describe_gd_and_claude_md_records_the_rate_limit(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        gd_rows = [l for l in readme.splitlines() if l.startswith("| `gd` |")]
        self.assertEqual(len(gd_rows), 1, "expected one `gd` row in the README source table")
        self.assertIn("GraphQL", gd_rows[0])
        self.assertIn("### GitHub Discussions (`gd`)", readme)
        self.assertIn("--sources gh,gd", readme)
        skill = (ROOT / "docs" / "skill" / "SKILL.md").read_text(encoding="utf-8")
        flag_rows = [l for l in skill.splitlines() if l.startswith("| `--sources")]
        self.assertEqual(len(flag_rows), 1)
        self.assertIn("`gd`", flag_rows[0])
        self.assertIn("GITHUB_TOKEN is empty", skill)
        self.assertIn("5/20 upvotes", skill)
        brief = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertTrue(any(l.startswith("| `gd` |") for l in brief.splitlines()), "no `gd` row in the CLAUDE.md Sources table")
        self.assertIn("5,000 points per hour", brief)
        self.assertIn("2026-11-27", brief)

    INSTALLED = Path.home() / ".claude" / "skills" / "pain-point-finder" / "SKILL.md"

    @unittest.skipUnless(INSTALLED.exists(), "installed skill copy not present on this machine (CI has none)")
    def test_the_installed_skill_copy_is_byte_identical(self):
        self.assertEqual(self.INSTALLED.read_bytes(), (ROOT / "docs" / "skill" / "SKILL.md").read_bytes(),
                         "docs/skill/SKILL.md and the ~/.claude/skills copy differ")


if __name__ == "__main__":
    unittest.main()
