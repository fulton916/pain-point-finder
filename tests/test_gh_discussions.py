# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Unit tests for the GitHub Discussions source (issue #33): the search string, record mapping, the
answered status, paging with the cursor, the missing token, rate-limit backoff (RATE_LIMITED, 403, 429),
the token never printing, registration in ppf, and dedupe when an issue and a discussion share a title.
No network: the session is faked and time.sleep is patched out."""

import io
import json
import logging
import unittest
from datetime import datetime, timezone
from unittest import mock

import requests

import collect_gh_discussions as gd
import common
import merge_and_score
import ppf
from common import FIELDS, RateLimited, load_config, validate_record

SINCE = datetime(2024, 11, 27, 0, 0, 0, tzinfo=timezone.utc)
FETCHED = "2026-11-27T00:00:00+00:00"
TOKEN = "ghp_secret_token_value_1234567890"

CFG = {
    "github_discussions": {
        "endpoint": "https://api.github.com/graphql", "per_page": 100, "max_pages": 3, "delay_seconds": 0,
        "max_tries": 6, "search_in": "title", "search_repos": [], "search_min_upvotes": 0, "body_max_chars": 2000,
    }
}


def node(title="Bulk API 2.0 times out on large jobs", url="https://github.com/jsforce/jsforce/discussions/1400",
         upvotes=12, comments=4, answered=False, body="We load 5 million rows nightly and the job dies.",
         created="2025-03-01T10:00:00Z", updated="2025-09-15T08:30:00Z") -> dict:
    return {
        "title": title, "url": url, "body": body, "upvoteCount": upvotes, "createdAt": created, "updatedAt": updated,
        "comments": {"totalCount": comments}, "answer": {"id": "DC_abc"} if answered else None,
    }


def page(nodes, has_next=False, cursor=None, total=None, remaining=4990, cost=1, errors=None) -> dict:
    data = {
        "rateLimit": {"limit": 5000, "cost": cost, "remaining": remaining, "resetAt": "2026-11-27T01:00:00Z"},
        "search": {"discussionCount": total if total is not None else len(nodes),
                   "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}, "nodes": nodes},
    }
    out = {"data": data}
    if errors:
        out["errors"] = errors
    return out


class FakeResponse:
    def __init__(self, status: int, data=None, headers=None, text=None):
        self.status_code = status
        self._data = data
        self.headers = headers or {}
        self.text = text if text is not None else ("" if data is None else json.dumps(data)[:200])
        self.reason = {401: "Unauthorized", 403: "Forbidden", 429: "Too Many Requests"}.get(status, "OK")

    def json(self):
        if self._data is None:
            raise ValueError("no JSON")
        return self._data


class FakeSession:
    """Answers POSTs in order and records every request (url, json payload, headers)."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[dict] = []

    def post(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": dict(headers or {})})
        if not self.answers:
            return FakeResponse(200, page([]))
        return self.answers.pop(0)


def with_token(test):
    """Run a test with GITHUB_TOKEN set (and registered as a secret), then clear it."""
    def wrapper(self):
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": TOKEN}):
            common.SECRETS.add(TOKEN)
            try:
                return test(self)
            finally:
                common.SECRETS.discard(TOKEN)
    return wrapper


class QueryStringTests(unittest.TestCase):
    def test_words_in_title_and_updated_since(self):
        q = gd.build_search_query("jsforce bulk api", SINCE, [])
        self.assertEqual(q, "jsforce bulk api in:title updated:>=2024-11-27")

    def test_repos_add_repo_terms_like_the_issues_source(self):
        q = gd.build_search_query("bulk api", SINCE, ["jsforce/jsforce", "forcedotcom/cli"])
        self.assertTrue(q.endswith(" repo:jsforce/jsforce repo:forcedotcom/cli"))

    def test_quotes_are_dropped_and_spaces_squeezed(self):
        self.assertTrue(gd.build_search_query('  "bulk"   api ', SINCE, []).startswith("bulk api in:title"))

    def test_no_since_means_no_updated_term(self):
        self.assertEqual(gd.build_search_query("x y", None, []), "x y in:title")

    def test_over_256_characters_is_refused(self):
        with self.assertRaises(ValueError):
            gd.build_search_query("bulk api", SINCE, [f"owner/repo-number-{i:03d}" for i in range(20)])

    def test_no_words_is_refused(self):
        with self.assertRaises(ValueError):
            gd.build_search_query('""', SINCE, [])

    @with_token
    def test_the_search_string_is_sent_as_the_q_variable_with_first_100(self):
        s = FakeSession([FakeResponse(200, page([node()]))])
        gd.search(s, CFG, "jsforce bulk api", SINCE)
        sent = s.calls[0]["json"]
        self.assertEqual(sent["variables"], {"q": "jsforce bulk api in:title updated:>=2024-11-27", "first": 100,
                                             "after": None})
        self.assertIn("search(type: DISCUSSION", sent["query"])
        self.assertIn("rateLimit", sent["query"])
        self.assertEqual(s.calls[0]["url"], "https://api.github.com/graphql")

    @with_token
    def test_repos_option_narrows_the_search(self):
        s = FakeSession([FakeResponse(200, page([node()]))])
        gd.search(s, CFG, "bulk api", SINCE, repos=["jsforce/jsforce"])
        self.assertIn(" repo:jsforce/jsforce", s.calls[0]["json"]["variables"]["q"])

    @with_token
    def test_config_search_repos_apply_when_the_option_is_absent(self):
        cfg = {"github_discussions": dict(CFG["github_discussions"], search_repos=["salesforce/lwc"])}
        s = FakeSession([FakeResponse(200, page([node()]))])
        gd.search(s, cfg, "bulk api", SINCE)
        self.assertIn(" repo:salesforce/lwc", s.calls[0]["json"]["variables"]["q"])


class RecordMappingTests(unittest.TestCase):
    def test_to_record_maps_a_discussion_to_the_11_fields(self):
        rec = gd.to_record(node(), FETCHED, 2000)
        self.assertEqual(list(rec), FIELDS)
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["source"], "github_discussions")
        self.assertEqual(rec["url"], "https://github.com/jsforce/jsforce/discussions/1400")
        self.assertEqual(rec["title"], "Bulk API 2.0 times out on large jobs")
        self.assertEqual(rec["score"], 12)
        self.assertEqual(rec["comments"], 4)
        self.assertEqual(rec["views"], "")
        self.assertEqual(rec["created_at"], "2025-03-01T10:00:00Z")
        self.assertEqual(rec["last_activity"], "2025-09-15T08:30:00Z")
        self.assertEqual(rec["fetched_at"], FETCHED)

    def test_status_is_answered_when_the_discussion_has_an_answer_else_empty(self):
        self.assertEqual(gd.to_record(node(answered=True), FETCHED, 2000)["status"], "answered")
        self.assertEqual(gd.to_record(node(answered=False), FETCHED, 2000)["status"], "")

    def test_the_body_is_cut_at_body_max_chars(self):
        self.assertEqual(len(gd.to_record(node(body="x" * 5000), FETCHED, 100)["body"]), 100)

    def test_missing_counts_stay_empty_not_zero(self):
        n = node()
        del n["upvoteCount"]
        n["comments"] = None
        rec = gd.to_record(n, FETCHED, 2000)
        self.assertEqual(rec["score"], "")
        self.assertEqual(rec["comments"], "")

    def test_nodes_to_records_drops_invalid_dedupes_by_url_and_sorts_most_upvoted_first(self):
        nodes = [node(upvotes=3, url="https://github.com/a/b/discussions/1"),
                 node(upvotes=9, url="https://github.com/a/b/discussions/2"),
                 node(upvotes=9, url="https://github.com/a/b/discussions/2"),
                 node(title="", url="https://github.com/a/b/discussions/3")]
        recs = gd.nodes_to_records(nodes, FETCHED, 2000)
        self.assertEqual([r["score"] for r in recs], [9, 3])

    def test_the_upvote_floor_applies(self):
        recs = gd.nodes_to_records([node(upvotes=1, url="https://x/1"), node(upvotes=5, url="https://x/2")], FETCHED,
                                   2000, min_upvotes=5)
        self.assertEqual([r["score"] for r in recs], [5])


class PagingTests(unittest.TestCase):
    @with_token
    def test_pages_follow_the_cursor_up_to_max_pages(self):
        s = FakeSession([
            FakeResponse(200, page([node(url="https://x/1")], has_next=True, cursor="c1", total=250)),
            FakeResponse(200, page([node(url="https://x/2")], has_next=True, cursor="c2", total=250)),
            FakeResponse(200, page([node(url="https://x/3")], has_next=True, cursor="c3", total=250)),
            FakeResponse(200, page([node(url="https://x/4")], has_next=True, cursor="c4", total=250)),
        ])
        with mock.patch("time.sleep") as sleep:
            recs = gd.search(s, CFG, "bulk api", SINCE)
        self.assertEqual(len(recs), 3)
        self.assertEqual(len(s.calls), 3)
        self.assertEqual([c["json"]["variables"]["after"] for c in s.calls], [None, "c1", "c2"])
        self.assertEqual(sleep.call_count, 2, "a pause between pages, none after the last")

    @with_token
    def test_paging_stops_when_there_is_no_next_page(self):
        s = FakeSession([FakeResponse(200, page([node()], has_next=False))])
        recs = gd.search(s, CFG, "bulk api", SINCE)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(s.calls), 1)

    @with_token
    def test_an_empty_answer_gives_no_records_and_no_error(self):
        s = FakeSession([FakeResponse(200, page([]))])
        self.assertEqual(gd.search(s, CFG, "bulk api", SINCE), [])

    @with_token
    def test_a_spent_budget_stops_paging_and_notes_it(self):
        s = FakeSession([
            FakeResponse(200, page([node(url="https://x/1")], has_next=True, cursor="c1", remaining=0)),
            FakeResponse(200, page([node(url="https://x/2")])),
        ])
        notes: list[str] = []
        recs = gd.search(s, CFG, "bulk api", SINCE, notes=notes)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(s.calls), 1)
        self.assertTrue(notes and "budget is spent" in notes[0], notes)

    @with_token
    def test_each_request_counts_in_stats(self):
        s = FakeSession([FakeResponse(200, page([node()]))])
        before = common.STATS["requests"]
        gd.search(s, CFG, "bulk api", SINCE)
        self.assertEqual(common.STATS["requests"], before + 1)


class MissingTokenTests(unittest.TestCase):
    def test_without_the_token_the_source_is_skipped_with_a_note_and_no_request(self):
        s = FakeSession([FakeResponse(200, page([node()]))])
        notes: list[str] = []
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": ""}):
            recs = gd.search(s, CFG, "bulk api", SINCE, notes=notes)
        self.assertEqual(recs, [])
        self.assertEqual(s.calls, [])
        self.assertEqual(len(notes), 1)
        self.assertIn("github_discussions skipped", notes[0])
        self.assertIn("GITHUB_TOKEN", notes[0])

    def test_without_the_token_no_authorization_header_is_built(self):
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": ""}):
            self.assertNotIn("Authorization", gd.headers())

    @with_token
    def test_with_the_token_the_bearer_header_is_sent(self):
        s = FakeSession([FakeResponse(200, page([node()]))])
        gd.search(s, CFG, "bulk api", SINCE)
        self.assertEqual(s.calls[0]["headers"]["Authorization"], f"Bearer {TOKEN}")

    def test_ppf_header_says_skipped_and_the_other_sources_still_print(self):
        hn_record = common.make_record(source="hackernews", url="https://news.ycombinator.com/item?id=1",
                                       title="Bulk API pain", body="", score=80, comments=10, views="",
                                       created_at="2025-06-01T00:00:00Z", last_activity="", status="",
                                       fetched_at=FETCHED)
        cfg = load_config()
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": ""}), \
                mock.patch("ppf.common.load_env"), \
                mock.patch.object(ppf.collect_hn, "search", return_value=[hn_record]), \
                mock.patch.object(ppf.Store, "__init__", return_value=None), \
                mock.patch.object(ppf.Store, "cached", return_value=None), \
                mock.patch.object(ppf.Store, "upsert", return_value=(1, 0)), \
                mock.patch.object(ppf.Store, "log_query"), \
                mock.patch.object(ppf.Store, "counts", return_value={}), \
                mock.patch.object(ppf.Store, "close"):
            result = ppf.run_search(cfg, "bulk api", ["hn", "gd"], "24m", True, 30, None, None,
                                    session=FakeSession([]))
        text = ppf.format_md(result)
        self.assertIn("github_discussions skipped: GITHUB_TOKEN is empty", text)
        self.assertIn("Bulk API pain", text)
        self.assertEqual(result["meta"]["per_source"]["github_discussions"]["records"], 0)
        self.assertNotIn("error", result["meta"]["per_source"]["github_discussions"])


class RateLimitTests(unittest.TestCase):
    @with_token
    def test_rate_limited_error_waits_for_the_reset_header_and_retries(self):
        limited = FakeResponse(200, {"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
                               headers={"X-RateLimit-Reset": "1"})
        s = FakeSession([limited, FakeResponse(200, page([node()]))])
        with mock.patch("time.sleep") as sleep, mock.patch("time.time", return_value=0.0):
            recs = gd.search(s, CFG, "bulk api", SINCE)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(s.calls), 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertGreaterEqual(sleep.call_args[0][0], 1.0)

    @with_token
    def test_a_reset_too_far_away_fails_the_source_at_once(self):
        far = FakeResponse(403, text="budget spent", headers={"X-RateLimit-Reset": str(10_000)})
        s = FakeSession([far, FakeResponse(200, page([node()]))])
        with mock.patch("time.sleep") as sleep, mock.patch("time.time", return_value=0.0), \
                self.assertRaises(RateLimited) as ctx:
            gd.search(s, CFG, "bulk api", SINCE)
        self.assertEqual(sleep.call_count, 0)
        self.assertEqual(len(s.calls), 1)
        self.assertIn("Try again later", str(ctx.exception))

    @with_token
    def test_a_budget_too_small_for_another_page_stops_paging(self):
        s = FakeSession([
            FakeResponse(200, page([node(url="https://x/1")], has_next=True, cursor="c1", remaining=1, cost=2)),
            FakeResponse(200, page([node(url="https://x/2")])),
        ])
        notes: list[str] = []
        self.assertEqual(len(gd.search(s, CFG, "bulk api", SINCE, notes=notes)), 1)
        self.assertEqual(len(s.calls), 1)
        self.assertTrue(notes and "budget is spent" in notes[0], notes)

    @with_token
    def test_rate_limited_forever_gives_up_with_rate_limited(self):
        s = FakeSession([FakeResponse(200, {"errors": [{"type": "RATE_LIMITED", "message": "x"}]}) for _ in range(6)])
        with mock.patch("time.sleep"), self.assertRaises(RateLimited):
            gd.search(s, CFG, "bulk api", SINCE)
        self.assertEqual(len(s.calls), 6)

    @with_token
    def test_http_403_and_429_wait_for_retry_after_and_retry(self):
        s = FakeSession([FakeResponse(403, text="secondary rate limit", headers={"Retry-After": "7"}),
                         FakeResponse(429, text="slow down"),
                         FakeResponse(200, page([node()]))])
        with mock.patch("time.sleep") as sleep:
            recs = gd.search(s, CFG, "bulk api", SINCE)
        self.assertEqual(len(recs), 1)
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(sleep.call_args_list[0][0][0], 8.0, "Retry-After 7 plus one second")

    @with_token
    def test_5xx_is_retried_and_401_is_not(self):
        s = FakeSession([FakeResponse(502), FakeResponse(200, page([node()]))])
        with mock.patch("time.sleep"):
            self.assertEqual(len(gd.search(s, CFG, "bulk api", SINCE)), 1)
        s = FakeSession([FakeResponse(401, text="Bad credentials")])
        with self.assertRaises(RuntimeError) as ctx:
            gd.search(s, CFG, "bulk api", SINCE)
        self.assertIn("401", str(ctx.exception))
        self.assertEqual(len(s.calls), 1)

    @with_token
    def test_another_graphql_error_raises_with_its_message(self):
        s = FakeSession([FakeResponse(200, {"errors": [{"message": "Field 'nope' doesn't exist"}]})])
        with self.assertRaises(RuntimeError) as ctx:
            gd.search(s, CFG, "bulk api", SINCE)
        self.assertIn("doesn't exist", str(ctx.exception))

    @with_token
    def test_the_rate_limit_field_is_logged(self):
        s = FakeSession([FakeResponse(200, page([node()], remaining=4321, cost=1))])
        with self.assertLogs("ppf", level="INFO") as logs:
            gd.search(s, CFG, "bulk api", SINCE)
        self.assertTrue(any("4321 of 5000 left" in line for line in logs.output), logs.output)


class TokenNeverPrintsTests(unittest.TestCase):
    @with_token
    def test_errors_logs_and_records_never_carry_the_token(self):
        s = FakeSession([FakeResponse(401, text=f"Bad credentials for {TOKEN}"),
                         FakeResponse(200, page([node(body=f"token {TOKEN} in body")]))])
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        common.log.addHandler(handler)
        try:
            with self.assertRaises(RuntimeError) as ctx:
                gd.search(s, CFG, "bulk api", SINCE)
            self.assertNotIn(TOKEN, str(ctx.exception))
            self.assertIn("***", str(ctx.exception))
            recs = gd.search(s, CFG, "bulk api", SINCE)
        finally:
            common.log.removeHandler(handler)
        self.assertNotIn(TOKEN, stream.getvalue())
        # The token is a header, never a record field; a body that happened to hold it is data, printed as given.
        self.assertNotIn(TOKEN, json.dumps({k: v for k, v in recs[0].items() if k != "body"}))

    @with_token
    def test_a_connection_error_message_is_redacted(self):
        class Boom:
            calls = 0

            def post(self, url, json=None, headers=None, timeout=None):
                raise requests.ConnectionError(f"refused for {TOKEN}")
        with mock.patch("time.sleep"), self.assertLogs("ppf", level="WARNING") as logs, self.assertRaises(RuntimeError) as ctx:
            gd.search(Boom(), CFG, "bulk api", SINCE)
        self.assertNotIn(TOKEN, str(ctx.exception))
        self.assertNotIn(TOKEN, "\n".join(logs.output))

    def test_github_token_is_a_named_secret(self):
        self.assertIn("GITHUB_TOKEN", common.SECRET_NAMES)


class RegistrationAndScoringTests(unittest.TestCase):
    def test_gd_is_registered_with_its_unit_but_not_a_default_source(self):
        self.assertIs(ppf.SOURCES["gd"], gd)
        self.assertEqual(gd.SOURCE, "github_discussions")
        self.assertEqual(ppf.UNITS["github_discussions"], "upvotes")
        self.assertEqual(ppf.parse_sources("gh,gd,github_discussions,discussions"), ["gh", "gd"])
        self.assertNotIn("gd", load_config()["search"]["default_sources"])

    def test_config_has_the_block_and_thresholds(self):
        cfg = load_config()
        self.assertEqual(cfg["github_discussions"]["max_pages"], 3)
        self.assertEqual(cfg["github_discussions"]["endpoint"], "https://api.github.com/graphql")
        t = cfg["scoring"]["source_thresholds"]["github_discussions"]
        self.assertEqual((t["moderate"], t["strong"]), (5, 20))
        self.assertEqual(cfg["scoring"]["status_weights"]["answered"], 0.5)

    def test_an_issue_and_a_discussion_with_the_same_title_merge_into_one_strong_row(self):
        title = "Bulk API 2.0 times out on large jobs"
        issue = common.make_record(source="github", url="https://github.com/jsforce/jsforce/issues/1399", title=title,
                                   body="", score=7, comments=3, views="", created_at="2025-03-01T00:00:00Z",
                                   last_activity="2025-09-01T00:00:00Z", status="open", fetched_at=FETCHED)
        disc = gd.to_record(node(title=title), FETCHED, 2000)
        rows, merge_log = merge_and_score.rank([issue, disc], load_config()["scoring"],
                                               as_of=datetime(2026, 11, 27, tzinfo=timezone.utc), mode="search")
        self.assertEqual(len(rows), 1)
        self.assertEqual(set(rows[0]["sources"].split(";")), {"github", "github_discussions"})
        self.assertEqual(rows[0]["evidence_label"], "Strong")
        self.assertEqual(len(merge_log), 1)

    def test_labels_follow_the_thresholds_and_answered_sinks(self):
        scoring = load_config()["scoring"]
        as_of = datetime(2026, 11, 27, tzinfo=timezone.utc)
        # Distinct titles, or the deduper would merge them into one row.
        recs = [gd.to_record(node(title="Strong one", upvotes=25, url="https://x/strong"), FETCHED, 2000),
                gd.to_record(node(title="Moderate one", upvotes=5, url="https://x/moderate"), FETCHED, 2000),
                gd.to_record(node(title="Anecdotal one", upvotes=1, url="https://x/anecdotal"), FETCHED, 2000)]
        rows, _ = merge_and_score.rank(recs, scoring, as_of=as_of, mode="search")
        labels = {r["best_url"].rsplit("/", 1)[1]: r["evidence_label"] for r in rows}
        self.assertEqual(labels, {"strong": "Strong", "moderate": "Moderate", "anecdotal": "Anecdotal"})
        open_rec = gd.to_record(node(title="Open one", upvotes=10, url="https://x/open"), FETCHED, 2000)
        answered_rec = gd.to_record(node(title="Answered one", upvotes=10, url="https://x/answered", answered=True),
                                    FETCHED, 2000)
        rows, _ = merge_and_score.rank([open_rec, answered_rec], scoring, as_of=as_of, mode="search")
        self.assertEqual([r["status"] for r in rows], ["", "answered"])

    def test_compact_output_prints_upvotes_and_answered(self):
        rec = gd.to_record(node(upvotes=12, answered=True), FETCHED, 2000)
        rows, _ = merge_and_score.rank([rec], load_config()["scoring"],
                                       as_of=datetime(2026, 11, 27, tzinfo=timezone.utc), mode="search")
        row = dict(rows[0], best_source="github_discussions", snippet="")
        result = {"meta": {"query": "q", "per_source": {"github_discussions": {"records": 1, "from_cache": False}},
                           "since_date": "2024-11-27", "since": "24m", "records": 1, "rows": 1, "merges": 0,
                           "dropped": 0, "shown": 1, "requests_sent": 1, "generated": FETCHED, "note": "n",
                           "attribution": []}, "rows": [row]}
        text = ppf.format_compact(result)
        self.assertIn("| github_discussions | 12 upvotes | 2025-09-15 | answered | Bulk API 2.0 times out on large jobs | "
                      "https://github.com/jsforce/jsforce/discussions/1400", text)


if __name__ == "__main__":
    unittest.main()
