# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Acceptance tests for issue #25: YouTube comments as a search source (`yt`), YouTube Data API v3.

One test class per "Done when" line, in the issue's order. Everything runs through ppf.main (the command
the user types) or collect_youtube.main, offline: requests.Session.get is replaced by a fake that answers
by URL (Hacker News on hn.algolia.com, YouTube on googleapis.com), common.load_env is mocked so no .env is
read, os.environ is set explicitly (key present or absent), time.sleep is patched out and the store is
in memory. The fake payloads follow the shapes in the issue (search.list items, commentThreads.list items,
error bodies). There is no YouTube key on this machine, so nothing here touches the network.

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
from unittest import mock

import requests

import collect_youtube
import common
import ppf
from common import load_config

KEY = "AIzaACCEPTANCE-FAKE-KEY-0123456789abcdefghij"
NOW = datetime.now(timezone.utc).replace(microsecond=0)
PUBLISHED = (NOW - timedelta(days=40)).strftime("%Y-%m-%dT%H:%M:%SZ")
UPDATED = (NOW - timedelta(days=39)).strftime("%Y-%m-%dT%H:%M:%SZ")

YT_BASE = "https://www.googleapis.com/youtube/v3"
YT_SEARCH = f"{YT_BASE}/search"
YT_COMMENTS = f"{YT_BASE}/commentThreads"
YT_VIDEOS = f"{YT_BASE}/videos"
HN_ENDPOINT = "https://hn.algolia.com/api/v1/search"
YT_TERMS = "https://www.youtube.com/t/terms"
GOOGLE_PRIVACY = "https://policies.google.com/privacy"

TOP_COMMENT = ("Every pair of these wears through at the heel in two months. The toe box is too narrow and my "
               "knees hurt after 10k. Please tell me which shoe fixes this, I have tried six brands.")


def video_item(video_id: str, title: str, channel: str = "Run Review") -> dict:
    """One search.list item, the shape given in the issue."""
    return {"kind": "youtube#searchResult", "id": {"kind": "youtube#video", "videoId": video_id},
            "snippet": {"title": title, "channelTitle": channel}}


def comment_thread(video_id: str, comment_id: str, text: str, likes: int, replies: int,
                   published: str = PUBLISHED, updated: str = UPDATED) -> dict:
    """One commentThreads.list item, the shape given in the issue."""
    return {"kind": "youtube#commentThread", "id": comment_id,
            "snippet": {"videoId": video_id, "totalReplyCount": replies,
                        "topLevelComment": {"kind": "youtube#comment", "id": comment_id,
                                            "snippet": {"textDisplay": text, "likeCount": likes,
                                                        "publishedAt": published, "updatedAt": updated}}}}


VIDEOS = [video_item(f"vid000{i}", f"Best Running Shoes 2026 part {i}") for i in range(1, 6)]
COMMENTS = {f"vid000{i}": [comment_thread(f"vid000{i}", f"UgcTop{i}",
                                          f"Part {i} review: the heel on these wore through in {i} months and the toe box pinched my foot",
                                          412 - i, 37),
                           comment_thread(f"vid000{i}", f"UgcLow{i}", f"Shoe number {i} fits fine for me", 3, 0)]
            for i in range(1, 6)}
COMMENTS["vid0001"] = [comment_thread("vid0001", "UgcTop1", TOP_COMMENT, 412, 37),
                       comment_thread("vid0001", "UgcLow1", "Shoe number 1 fits fine for me", 3, 0)]

HN_HIT = {"objectID": "4242", "title": "Ask HN: Which running shoes last more than 300 miles?", "url": "",
          "story_text": "Mine fall apart at the heel.", "points": 120, "num_comments": 40,
          "created_at": (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%SZ")}


def fake_response(status: int, payload) -> mock.Mock:
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = {}
    resp.text = json.dumps(payload)
    resp.reason = "fake"
    if status >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}", response=resp)
    else:
        resp.raise_for_status.return_value = None
    return resp


def api_error(status: int, reason: str, message: str) -> mock.Mock:
    """A YouTube API error body, the shape given in the issue."""
    return fake_response(status, {"error": {"code": status, "message": message,
                                            "errors": [{"message": message, "domain": "youtube", "reason": reason}]}})


class FakeInternet:
    """requests.Session.get stand-in. Hacker News answers with hn_hits. YouTube answers search.list with
    `videos`, commentThreads.list with `comments[videoId]` (one page), or, when `endless` is set, a new
    one-comment page with a nextPageToken on every call (a video that never runs out of comments). When
    `youtube_error` is set every googleapis call raises it. Records every call."""

    def __init__(self, videos=None, comments=None, hn_hits=None, endless=False, youtube_error=None,
                 search_response=None):
        self.videos = videos if videos is not None else []
        self.comments = comments or {}
        self.hn_hits = hn_hits or []
        self.endless = endless
        self.youtube_error = youtube_error
        self.search_response = search_response
        self.calls: list[tuple[str, dict]] = []
        self.pages = 0

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params))
        if url.startswith(HN_ENDPOINT):
            return fake_response(200, {"hits": self.hn_hits, "nbPages": 1})
        if not url.startswith(YT_BASE):
            raise AssertionError(f"unexpected request to {url}")
        if self.youtube_error is not None:
            raise self.youtube_error
        if url == YT_SEARCH:
            return self.search_response or fake_response(200, {"items": self.videos})
        if url == YT_COMMENTS:
            vid = params["videoId"]
            if self.endless:
                self.pages += 1
                item = comment_thread(vid, f"Ugc{vid}-{self.pages}", f"Endless comment {self.pages} about shoes",
                                      self.pages % 50, 0)
                return fake_response(200, {"items": [item], "nextPageToken": f"page{self.pages}"})
            return fake_response(200, {"items": self.comments.get(vid, [])})
        if url == YT_VIDEOS:
            return fake_response(200, {"items": [{"id": "x"}]})
        raise AssertionError(f"unexpected YouTube url {url}")

    @property
    def youtube_calls(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if c[0].startswith(YT_BASE)]


def test_config() -> dict:
    cfg = copy.deepcopy(load_config())
    cfg["paths"]["store"] = ":memory:"
    cfg["youtube"]["delay_seconds"] = 0
    cfg["hacker_news"]["delay_seconds"] = 0
    return cfg


class CliRun:
    def __init__(self, rc: int, out: str, logs: str, internet: FakeInternet):
        self.rc, self.out, self.logs, self.internet = rc, out, logs, internet


def run_cli(argv: list[str], internet: FakeInternet, key: str | None = KEY, cfg: dict | None = None) -> CliRun:
    """Run `python ppf.py <argv>` offline: in-memory store, no .env, the key only in os.environ when
    given, every log line of the ppf logger captured."""
    cfg = cfg or test_config()
    env = {"YOUTUBE_API_KEY": key} if key else {}
    collect_youtube._WARNED_NO_KEY = False
    out, logbuf = io.StringIO(), io.StringIO()
    handler = logging.StreamHandler(logbuf)
    handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    old_level = common.log.level
    common.log.addHandler(handler)
    common.log.setLevel(logging.INFO)
    try:
        with mock.patch.object(common, "load_config", return_value=cfg), \
             mock.patch.object(common, "load_env"), \
             mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(requests.Session, "get", side_effect=internet), \
             mock.patch("time.sleep"), redirect_stdout(out):
            rc = ppf.main(argv)
    finally:
        common.log.removeHandler(handler)
        common.log.setLevel(old_level)
        collect_youtube._WARNED_NO_KEY = False
    return CliRun(rc, out.getvalue(), logbuf.getvalue(), internet)


def sources_header(md: str) -> str:
    for line in md.splitlines():
        if line.startswith("Sources:"):
            return line
    raise AssertionError(f"no 'Sources:' header line in:\n{md}")


class SecretCleanup(unittest.TestCase):
    def setUp(self):
        self.addCleanup(common.SECRETS.discard, KEY)


class WithTheKeySearchYtReturnsCommentsWithLikesRepliesDatesAndLinks(SecretCleanup):
    """Done when: with YOUTUBE_API_KEY in .env, `python ppf.py search "best running shoes" --sources yt
    --since 12m` returns comments with like counts, reply counts, dates and links that open the comment
    under its video."""

    def test_search_best_running_shoes_sources_yt_since_12m_prints_likes_replies_dates_and_comment_links(self):
        run = run_cli(["search", "best running shoes", "--sources", "yt", "--since", "12m"],
                      FakeInternet(VIDEOS, COMMENTS))
        self.assertEqual(run.rc, 0, run.out + run.logs)
        md = run.out
        self.assertIn("youtube (10 records, fetched; youtube read the top comments of 5 videos; spent 6 quota units",
                      sources_header(md))
        # The most-liked comment: title = first 120 characters of the comment, link = watch?v=<video>&lc=<comment>.
        title = " ".join(TOP_COMMENT.split())[:120]
        self.assertEqual(len(title), 120)
        self.assertIn(f"1. **[{title}](https://www.youtube.com/watch?v=vid0001&lc=UgcTop1)**", md)
        first_row = md.split("1. **[", 1)[1].splitlines()[1]
        self.assertIn("412 likes", first_row)
        self.assertIn("37 replies", first_row)
        self.assertIn(f"date {UPDATED[:10]}", first_row)
        self.assertIn("youtube", first_row)
        # The body starts with the video title and channel in brackets, so the reader knows which video.
        self.assertIn("[Best Running Shoes 2026 part 1 (Run Review)] Every pair of these", md)
        # Every result link points at a comment under its video.
        links = re.findall(r"\*\*\[.*?\]\((https?://[^)]+)\)\*\*", md)
        self.assertEqual(len(links), 10)
        for link in links:
            self.assertRegex(link, r"^https://www\.youtube\.com/watch\?v=vid000[1-5]&lc=Ugc(Top|Low)[1-5]$")
        # Low-liked comments carry their own counts; nothing is guessed.
        self.assertIn("3 likes", md)
        self.assertIn("0 replies", md)

    def test_the_api_is_asked_for_top_videos_since_12_months_then_each_videos_most_relevant_comments(self):
        run = run_cli(["search", "best running shoes", "--sources", "yt", "--since", "12m"],
                      FakeInternet(VIDEOS, COMMENTS))
        calls = run.internet.youtube_calls
        self.assertEqual(calls[0][0], YT_SEARCH)
        search = calls[0][1]
        self.assertEqual((search["type"], search["order"], search["q"], search["maxResults"], search["key"]),
                         ("video", "relevance", "best running shoes", 5, KEY))
        published_after = datetime.strptime(search["publishedAfter"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        self.assertLess(abs((NOW - published_after) - timedelta(days=365)), timedelta(days=3))
        comment_calls = [c for c in calls[1:] if c[0] == YT_COMMENTS]
        self.assertEqual(len(comment_calls), 5)
        self.assertEqual(sorted(c[1]["videoId"] for c in comment_calls), [f"vid000{i}" for i in range(1, 6)])
        for _, p in comment_calls:
            self.assertEqual((p["order"], p["maxResults"], p["textFormat"], p["key"]), ("relevance", 100, "plainText", KEY))
        self.assertEqual(len(calls), 6)

    def test_json_rows_carry_score_comments_date_and_the_comment_url(self):
        run = run_cli(["search", "best running shoes", "--sources", "yt", "--since", "12m", "--format", "json"],
                      FakeInternet(VIDEOS, COMMENTS))
        self.assertEqual(run.rc, 0)
        result = json.loads(run.out)
        self.assertEqual(result["meta"]["per_source"]["youtube"]["records"], 10)
        self.assertEqual(result["meta"]["since"], "12m")
        top = result["rows"][0]
        self.assertEqual(top["best_url"], "https://www.youtube.com/watch?v=vid0001&lc=UgcTop1")
        self.assertEqual(top["raw_score"], 412)
        self.assertEqual(top["comments"], 37)
        self.assertEqual(top["latest_activity"], UPDATED[:10])
        self.assertEqual(top["best_source"], "youtube")
        for row in result["rows"]:
            self.assertRegex(row["best_url"], r"^https://www\.youtube\.com/watch\?v=[^&]+&lc=.+$")
            self.assertIsInstance(row["raw_score"], int)
            self.assertIsInstance(row["comments"], int)
            self.assertTrue(row["latest_activity"])


class WithoutTheKeyHnAndYtStillPrintsHackerNewsAndSaysYoutubeWasSkipped(SecretCleanup):
    """Done when: without the key, `--sources hn,yt` still prints Hacker News results and the header says
    YouTube was skipped for a missing key."""

    def test_sources_hn_yt_without_a_key_prints_hacker_news_rows_and_the_skipped_note(self):
        run = run_cli(["search", "running shoes", "--sources", "hn,yt"], FakeInternet(hn_hits=[HN_HIT]), key=None)
        self.assertEqual(run.rc, 0, run.out + run.logs)
        header = sources_header(run.out)
        self.assertIn("hackernews (1 records, fetched)", header)
        self.assertIn("youtube (0 records, fetched; youtube skipped: YOUTUBE_API_KEY is empty in .env", header)
        self.assertIn("1. **[Ask HN: Which running shoes last more than 300 miles?](https://news.ycombinator.com/item?id=4242)**",
                      run.out)
        self.assertIn("120 points", run.out)
        self.assertNotIn("No results.", run.out)
        self.assertNotIn("failed", header)
        # No request went to YouTube without a key.
        self.assertEqual(run.internet.youtube_calls, [])
        self.assertEqual(len(run.internet.calls), 1)
        self.assertIn("YOUTUBE_API_KEY is empty", run.logs)

    def test_the_json_meta_note_says_youtube_was_skipped_and_marks_no_error(self):
        run = run_cli(["search", "running shoes", "--sources", "hn,yt", "--format", "json"],
                      FakeInternet(hn_hits=[HN_HIT]), key=None)
        result = json.loads(run.out)
        self.assertEqual(result["meta"]["sources"], ["hackernews", "youtube"])
        self.assertEqual(result["meta"]["per_source"]["hackernews"]["records"], 1)
        yt = result["meta"]["per_source"]["youtube"]
        self.assertEqual(yt["records"], 0)
        self.assertNotIn("error", yt)
        self.assertIn("youtube skipped: YOUTUBE_API_KEY is empty", yt["note"])
        self.assertIn("youtube skipped: YOUTUBE_API_KEY is empty", result["meta"]["note"])
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["rows"][0]["best_source"], "hackernews")


class CheckKeyReportsWhetherTheKeyWorksAndTheUnits(SecretCleanup):
    """Done when: `python collect_youtube.py --check-key` reports whether the key works and the units
    left today (if the API does not expose remaining quota, report the daily quota and the units this run
    spent, and say so)."""

    def run_check_key(self, internet: FakeInternet | None, key: str | None) -> tuple[int, str]:
        out = io.StringIO()
        session = mock.Mock()
        session.get.side_effect = internet or (lambda *a, **k: AssertionError("no request expected"))
        env = {"YOUTUBE_API_KEY": key} if key else {}
        with mock.patch.dict("os.environ", env, clear=True), \
             mock.patch.object(collect_youtube, "load_env"), \
             mock.patch.object(collect_youtube, "setup_logging"), \
             mock.patch.object(collect_youtube, "load_config", return_value=test_config()), \
             mock.patch.object(collect_youtube.requests, "Session", return_value=session), \
             mock.patch("time.sleep"), redirect_stdout(out):
            rc = collect_youtube.main(["--check-key"])
        return rc, out.getvalue()

    def test_check_key_with_a_working_key_says_ok_and_reports_the_daily_quota_and_this_runs_units(self):
        internet = FakeInternet()
        rc, text = self.run_check_key(internet, KEY)
        self.assertEqual(rc, 0, text)
        self.assertIn("OK", text.splitlines()[0])
        # The API exposes no remaining quota, so the output must say so, then give the daily quota and the spend.
        self.assertRegex(text, r"(?i)not reported by the API|does not (say|report)|not exposed")
        self.assertIn("10,000", text)
        self.assertRegex(text, r"(?i)spent 1 unit")
        self.assertRegex(text, r"(?i)units left today")
        # Exactly one cheap call, with the key, and the key is not echoed.
        self.assertEqual(len(internet.calls), 1)
        self.assertEqual(internet.calls[0][1]["key"], KEY)
        self.assertNotIn(KEY, text)

    def test_check_key_with_a_rejected_key_says_failed_with_the_api_message_and_no_key(self):
        internet = FakeInternet()
        internet.youtube_error = None
        session_answer = api_error(400, "keyInvalid", "API key not valid. Please pass a valid API key.")
        out = io.StringIO()
        session = mock.Mock()
        session.get.return_value = session_answer
        with mock.patch.dict("os.environ", {"YOUTUBE_API_KEY": KEY}, clear=True), \
             mock.patch.object(collect_youtube, "load_env"), \
             mock.patch.object(collect_youtube, "setup_logging"), \
             mock.patch.object(collect_youtube, "load_config", return_value=test_config()), \
             mock.patch.object(collect_youtube.requests, "Session", return_value=session), \
             mock.patch("time.sleep"), redirect_stdout(out):
            rc = collect_youtube.main(["--check-key"])
        text = out.getvalue()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", text)
        self.assertIn("API key not valid", text)
        self.assertNotIn(KEY, text)

    def test_check_key_without_a_key_says_failed_and_names_the_missing_variable(self):
        rc, text = self.run_check_key(None, None)
        self.assertEqual(rc, 1)
        self.assertIn("FAILED", text)
        self.assertIn("YOUTUBE_API_KEY", text)


class OneQuestionSpendsAtMost600UnitsAndTheLogShowsTheNumber(SecretCleanup):
    """Done when: one question spends at most 600 units and the log shows the number."""

    def test_config_caps_a_question_at_600_units(self):
        self.assertEqual(load_config()["youtube"]["max_units"], 600)

    def test_a_default_question_logs_its_unit_count_and_stays_under_600(self):
        run = run_cli(["search", "best running shoes", "--sources", "yt", "--since", "12m"],
                      FakeInternet(VIDEOS, COMMENTS))
        spent = re.findall(r"spent (\d+) quota units", run.logs)
        self.assertTrue(spent, run.logs)
        self.assertEqual(int(spent[-1]), 6)   # 1 search.list + 5 commentThreads pages
        self.assertLessEqual(int(spent[-1]), 600)
        self.assertIn("spent 6 quota units", sources_header(run.out))
        self.assertEqual(len(run.internet.youtube_calls), 6)

    def test_a_video_that_never_runs_out_of_comments_is_stopped_at_600_units_and_the_log_says_600(self):
        # The hostile case: 50 videos (search.list's maximum), each with endless comment pages. The stop
        # must come from max_units, not from the data running out.
        cfg = test_config()
        cfg["youtube"]["videos"] = 50
        cfg["youtube"]["comments_per_video"] = 10 ** 6
        internet = FakeInternet([video_item(f"vid{i:03d}", f"Video {i}") for i in range(50)], endless=True)
        info: list[str] = []
        session = mock.Mock()
        session.get.side_effect = internet
        with mock.patch.dict("os.environ", {"YOUTUBE_API_KEY": KEY}, clear=True), mock.patch("time.sleep"), \
             self.assertLogs("ppf", level="INFO") as logs:
            records = collect_youtube.search(session, cfg, "best running shoes", NOW - timedelta(days=365), info=info)
        self.assertEqual(len(internet.calls), 600)        # 1 search.list + 599 commentThreads pages
        self.assertEqual(len(records), 599)
        joined = "\n".join(logs.output)
        spent = [int(n) for n in re.findall(r"spent (\d+) quota units", joined)]
        self.assertTrue(spent, joined)
        self.assertEqual(spent[-1], 600)
        self.assertLessEqual(max(spent), 600)
        self.assertIn("stopping before max_units (600)", joined)
        self.assertIn("spent 600 quota units", info[0])

    def test_the_header_and_log_number_agree_through_the_cli_when_the_budget_stops_the_run(self):
        cfg = test_config()
        cfg["youtube"]["videos"] = 50
        cfg["youtube"]["comments_per_video"] = 10 ** 6
        cfg["youtube"]["max_units"] = 12          # a small budget keeps the CLI run fast; the mechanism is the same
        run = run_cli(["search", "best running shoes", "--sources", "yt"],
                      FakeInternet([video_item(f"vid{i:03d}", f"Video {i}") for i in range(50)], endless=True), cfg=cfg)
        self.assertEqual(run.rc, 0, run.out + run.logs)
        self.assertEqual(len(run.internet.youtube_calls), 12)
        self.assertIn("spent 12 quota units", sources_header(run.out))
        self.assertIn("spent 12 quota units", run.logs)
        self.assertIn("stopping before max_units (12)", run.logs)


class TheKeyNeverAppearsAndTheTermsLinePrintsWithEveryYtResultSet(SecretCleanup):
    """Done when: the key never appears in markdown, JSON or log output, and the required YouTube terms
    line prints with every result set that includes `yt`."""

    def test_the_key_is_sent_to_the_api_but_absent_from_markdown_json_and_log_output(self):
        for fmt in ("md", "json"):
            with self.subTest(format=fmt):
                run = run_cli(["search", "best running shoes", "--sources", "yt", "--format", fmt],
                              FakeInternet(VIDEOS, COMMENTS))
                self.assertEqual(run.rc, 0)
                self.assertEqual(run.internet.youtube_calls[0][1]["key"], KEY)   # it did go to the API
                self.assertNotIn(KEY, run.out)
                self.assertNotIn(KEY, run.logs)
                self.assertTrue(run.out.strip())
                self.assertTrue(run.logs.strip())

    def test_the_key_is_redacted_when_a_connection_error_message_carries_the_request_url(self):
        # requests puts the full URL, query string included, in connection error messages.
        error = requests.ConnectionError(f"HTTPSConnectionPool: Max retries exceeded with url: "
                                         f"/youtube/v3/search?part=snippet&key={KEY}&q=best+running+shoes")
        for fmt in ("md", "json"):
            with self.subTest(format=fmt):
                run = run_cli(["search", "best running shoes", "--sources", "yt", "--format", fmt],
                              FakeInternet(youtube_error=error))
                self.assertNotIn(KEY, run.out)
                self.assertNotIn(KEY, run.logs)
                self.assertIn("***", run.logs)
                if fmt == "json":
                    yt = json.loads(run.out)["meta"]["per_source"]["youtube"]
                    self.assertIn("error", yt)
                    self.assertNotIn(KEY, yt["error"])
                else:
                    self.assertIn("youtube (failed:", sources_header(run.out))

    def test_the_key_is_redacted_when_the_api_echoes_it_in_an_error_message(self):
        # Defensive: even if an error body quoted the key, nothing printed or logged may show it.
        bad = api_error(400, "keyInvalid", f"API key not valid: {KEY}")
        for fmt in ("md", "json"):
            with self.subTest(format=fmt):
                run = run_cli(["search", "best running shoes", "--sources", "yt", "--format", fmt],
                              FakeInternet(search_response=bad))
                self.assertNotIn(KEY, run.out)
                self.assertNotIn(KEY, run.logs)
                self.assertIn("keyInvalid", run.out)

    def test_the_terms_line_prints_with_every_result_set_that_includes_yt(self):
        cases = {
            "yt with results": (["search", "best running shoes", "--sources", "yt"], FakeInternet(VIDEOS, COMMENTS), KEY),
            "yt with no results": (["search", "best running shoes", "--sources", "yt"], FakeInternet([], {}), KEY),
            "hn,yt without a key": (["search", "running shoes", "--sources", "hn,yt"], FakeInternet(hn_hits=[HN_HIT]), None),
            "youtube by its long name": (["search", "best running shoes", "--sources", "youtube"], FakeInternet(VIDEOS, COMMENTS), KEY),
        }
        for name, (argv, internet, key) in cases.items():
            with self.subTest(case=name):
                run = run_cli(argv, internet, key=key)
                self.assertEqual(run.rc, 0, run.out + run.logs)
                self.assertIn(YT_TERMS, run.out)
                self.assertIn(GOOGLE_PRIVACY, run.out)
                # Both links sit on one line in the header, before the rows.
                terms_lines = [l for l in run.out.splitlines() if YT_TERMS in l and GOOGLE_PRIVACY in l]
                self.assertEqual(len(terms_lines), 1, run.out)
                rows_start = run.out.find("\n1. **[")
                if rows_start != -1:
                    self.assertLess(run.out.find(YT_TERMS), rows_start)

    def test_the_terms_line_is_in_json_meta_attribution(self):
        run = run_cli(["search", "best running shoes", "--sources", "yt", "--format", "json"], FakeInternet(VIDEOS, COMMENTS))
        attribution = json.loads(run.out)["meta"]["attribution"]
        self.assertTrue(any(YT_TERMS in a and GOOGLE_PRIVACY in a for a in attribution), attribution)

    def test_the_terms_line_is_tied_to_yt_not_printed_for_other_sources_alone(self):
        run = run_cli(["search", "running shoes", "--sources", "hn"], FakeInternet(hn_hits=[HN_HIT]), key=None)
        self.assertEqual(run.rc, 0)
        self.assertNotIn(YT_TERMS, run.out)
        self.assertNotIn(GOOGLE_PRIVACY, run.out)


if __name__ == "__main__":
    unittest.main()
