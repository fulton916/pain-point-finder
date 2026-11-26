# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Unit tests for the YouTube source (issue #25): record mapping, the comment url, unit counting and the
max_units stop, a missing key skipping the source while the others run, comments disabled on a video,
the key redacted from output, the 30-day purge, the Moderate cap, and yt staying out of the default
sources. No network: requests.Session.get is replaced by a fake that answers by URL path, and time.sleep
is patched out."""

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from unittest import mock

import requests

import collect_hn
import collect_youtube
import common
import merge_and_score
import ppf
from common import FIELDS, load_config, validate_record
from store import Store

SINCE = datetime(2025, 11, 26, 0, 0, 0, tzinfo=timezone.utc)
FETCHED = "2026-11-26T00:00:00+00:00"
KEY = "AIzaFAKEKEY1234567890abcdefghijklmnop"

VIDEO = {"video_id": "dQw4w9WgXcQ", "title": "Best Running Shoes of 2026", "channel": "Shoe Reviewer"}

THREAD = {
    "kind": "youtube#commentThread",
    "id": "UgzCommentIdAbc123",
    "snippet": {
        "channelId": "UCxyz",
        "videoId": "dQw4w9WgXcQ",
        "topLevelComment": {
            "kind": "youtube#comment",
            "id": "UgzCommentIdAbc123",
            "snippet": {
                "channelId": "UCxyz",
                "videoId": "dQw4w9WgXcQ",
                "textDisplay": "Every pair of these wears through at the heel in two months.\nThe toe box is too narrow "
                               "and my knees hurt after 10k. Please tell me which shoe fixes this, I have tried six brands.",
                "textOriginal": "Every pair of these wears through at the heel in two months.",
                "authorDisplayName": "A Runner",
                "likeCount": 412,
                "publishedAt": "2026-03-04T10:11:12Z",
                "updatedAt": "2026-03-05T08:00:00Z",
            },
        },
        "canReply": True,
        "totalReplyCount": 37,
        "isPublic": True,
    },
}

CFG = {
    "youtube": {
        "endpoint": "https://www.googleapis.com/youtube/v3",
        "videos": 5, "comments_per_video": 100, "search_list_cost": 1, "max_units": 600, "sweep_max_units": 2000,
        "delay_seconds": 0, "search_min_likes": 0, "min_likes": 5, "title_chars": 120, "body_max_chars": 2000,
        "store_days": 30,
    }
}


def thread(n: int, likes: int = 1, video_id: str = "dQw4w9WgXcQ", text: str | None = None) -> dict:
    t = json.loads(json.dumps(THREAD))
    t["id"] = t["snippet"]["topLevelComment"]["id"] = f"Ugz{n:04d}"
    t["snippet"]["videoId"] = t["snippet"]["topLevelComment"]["snippet"]["videoId"] = video_id
    t["snippet"]["topLevelComment"]["snippet"]["likeCount"] = likes
    t["snippet"]["topLevelComment"]["snippet"]["textDisplay"] = text or f"Comment number {n} about running shoes"
    return t


def video_item(video_id: str, title: str = "A video", channel: str = "A channel") -> dict:
    return {"kind": "youtube#searchResult", "id": {"kind": "youtube#video", "videoId": video_id},
            "snippet": {"title": title, "channelTitle": channel, "publishedAt": "2026-01-01T00:00:00Z"}}


def fake_response(status: int, payload: dict) -> mock.Mock:
    resp = mock.Mock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.headers = {}
    resp.text = json.dumps(payload)
    resp.reason = "fake"
    return resp


def api_error(status: int, reason: str, message: str = "nope") -> mock.Mock:
    return fake_response(status, {"error": {"code": status, "message": message,
                                            "errors": [{"message": message, "domain": "youtube", "reason": reason}]}})


class FakeSession:
    """Answers search.list with `videos`, commentThreads.list from `comments` (videoId -> list of answers,
    each a payload dict or a response Mock; a list is one page per call). Records every call."""

    def __init__(self, videos: list[dict] | mock.Mock | None = None, comments: dict | None = None):
        self.videos = videos if videos is not None else []
        self.comments = comments or {}
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/search"):
            if isinstance(self.videos, mock.Mock):
                return self.videos
            return fake_response(200, {"items": self.videos})
        if url.endswith("/commentThreads"):
            answers = self.comments.get(params["videoId"], [])
            page = answers.pop(0) if answers else {"items": []}
            return page if isinstance(page, mock.Mock) else fake_response(200, page)
        if url.endswith("/videos"):
            return fake_response(200, {"items": [{"id": "x"}]})
        raise AssertionError(f"unexpected url {url}")


def env_key(value: str = KEY):
    return mock.patch.dict("os.environ", {"YOUTUBE_API_KEY": value})


def no_key():
    return mock.patch.dict("os.environ", {"YOUTUBE_API_KEY": ""})


class RecordMappingTests(unittest.TestCase):
    def test_to_record_maps_a_comment_thread_to_the_11_fields(self):
        rec = collect_youtube.to_record(THREAD, VIDEO, FETCHED)
        self.assertEqual(list(rec), FIELDS)
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["source"], "youtube")
        self.assertEqual(rec["url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=UgzCommentIdAbc123")
        self.assertEqual(rec["score"], 412)
        self.assertEqual(rec["comments"], 37)
        self.assertEqual(rec["created_at"], "2026-03-04T10:11:12Z")
        self.assertEqual(rec["last_activity"], "2026-03-05T08:00:00Z")
        self.assertEqual((rec["views"], rec["status"]), ("", ""))
        self.assertEqual(rec["fetched_at"], FETCHED)

    def test_title_is_the_first_120_characters_of_the_comment_on_one_line(self):
        rec = collect_youtube.to_record(THREAD, VIDEO, FETCHED)
        self.assertEqual(len(rec["title"]), 120)
        self.assertNotIn("\n", rec["title"])
        self.assertTrue(rec["title"].startswith("Every pair of these wears through at the heel in two months. The toe box"))
        self.assertEqual(collect_youtube.to_record(THREAD, VIDEO, FETCHED, title_chars=10)["title"], "Every pair")

    def test_body_starts_with_the_video_title_and_channel_in_brackets(self):
        rec = collect_youtube.to_record(THREAD, VIDEO, FETCHED)
        self.assertTrue(rec["body"].startswith("[Best Running Shoes of 2026 (Shoe Reviewer)] Every pair of these"))
        self.assertIn("my knees hurt after 10k", rec["body"])
        no_channel = collect_youtube.to_record(THREAD, {"video_id": "dQw4w9WgXcQ", "title": "T", "channel": ""}, FETCHED)
        self.assertTrue(no_channel["body"].startswith("[T] Every pair"))
        bare = collect_youtube.to_record(THREAD, {"video_id": "dQw4w9WgXcQ"}, FETCHED)
        self.assertTrue(bare["body"].startswith("Every pair"))
        self.assertEqual(len(collect_youtube.to_record(THREAD, VIDEO, FETCHED, body_max_chars=40)["body"]), 40)

    def test_missing_counts_and_dates_stay_empty_not_zero(self):
        t = json.loads(json.dumps(THREAD))
        del t["snippet"]["totalReplyCount"]
        for k in ("likeCount", "updatedAt"):
            del t["snippet"]["topLevelComment"]["snippet"][k]
        rec = collect_youtube.to_record(t, VIDEO, FETCHED)
        self.assertEqual((rec["score"], rec["comments"], rec["last_activity"]), ("", "", ""))
        self.assertEqual(validate_record(rec), [])

    def test_comments_without_text_or_id_are_dropped(self):
        self.assertIsNone(collect_youtube.to_record(thread(1, text="   "), VIDEO, FETCHED))
        t = thread(2)
        t["snippet"]["topLevelComment"]["id"] = ""
        t["id"] = ""
        self.assertIsNone(collect_youtube.to_record(t, VIDEO, FETCHED))
        self.assertIsNone(collect_youtube.to_record({}, VIDEO, FETCHED))

    def test_the_video_id_falls_back_to_the_video_when_the_comment_lacks_it(self):
        t = thread(3)
        del t["snippet"]["topLevelComment"]["snippet"]["videoId"]
        del t["snippet"]["videoId"]
        rec = collect_youtube.to_record(t, VIDEO, FETCHED)
        self.assertEqual(rec["url"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=Ugz0003")

    def test_threads_to_records_dedupes_by_url_sorts_most_liked_first_and_applies_the_like_floor(self):
        pairs = [(thread(1, 3), VIDEO), (thread(2, 90), VIDEO), (thread(1, 3), VIDEO), (thread(3, 10), VIDEO)]
        records = collect_youtube.threads_to_records(pairs, FETCHED)
        self.assertEqual([r["score"] for r in records], [90, 10, 3])
        self.assertEqual(len({r["url"] for r in records}), 3)
        floored = collect_youtube.threads_to_records(pairs, FETCHED, min_likes=5)
        self.assertEqual([r["score"] for r in floored], [90, 10])


class UrlAndParamTests(unittest.TestCase):
    def test_comment_url_opens_the_comment_under_its_video(self):
        self.assertEqual(collect_youtube.comment_url("abc", "Ugz1"), "https://www.youtube.com/watch?v=abc&lc=Ugz1")
        self.assertEqual(collect_youtube.comment_url("", "Ugz1"), "")
        self.assertEqual(collect_youtube.comment_url("abc", ""), "")

    def test_search_params_follow_the_design(self):
        p = collect_youtube.search_params(CFG, "best  running\nshoes", SINCE, KEY)
        self.assertEqual(p, {"part": "snippet", "type": "video", "order": "relevance", "q": "best running shoes",
                             "maxResults": 5, "key": KEY, "publishedAfter": "2025-11-26T00:00:00Z"})
        self.assertNotIn("publishedAfter", collect_youtube.search_params(CFG, "x", None, KEY))
        big = {"youtube": dict(CFG["youtube"], videos=500)}
        self.assertEqual(collect_youtube.search_params(big, "x", None, KEY)["maxResults"], 50)

    def test_since_is_rfc3339_utc(self):
        local = datetime(2026, 8, 26, 5, 30, 15, 123456, tzinfo=timezone(timedelta(hours=5)))
        self.assertEqual(collect_youtube.since_param(local), "2026-08-26T00:30:15Z")
        self.assertEqual(collect_youtube.since_param(datetime(2026, 8, 26)), "2026-08-26T00:00:00Z")
        self.assertEqual(collect_youtube.since_param(None), "")

    def test_comments_params_follow_the_design(self):
        p = collect_youtube.comments_params(CFG, "vid1", KEY)
        self.assertEqual(p, {"part": "snippet", "videoId": "vid1", "order": "relevance", "maxResults": 100,
                             "textFormat": "plainText", "key": KEY})
        self.assertEqual(collect_youtube.comments_params(CFG, "vid1", KEY, "tok")["pageToken"], "tok")
        small = {"youtube": dict(CFG["youtube"], comments_per_video=20)}
        self.assertEqual(collect_youtube.comments_params(small, "vid1", KEY)["maxResults"], 20)
        huge = {"youtube": dict(CFG["youtube"], comments_per_video=250)}
        self.assertEqual(collect_youtube.comments_params(huge, "vid1", KEY)["maxResults"], 100)


class UnitCountingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(collect_youtube.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_one_question_spends_one_search_call_plus_one_unit_per_video(self):
        session = FakeSession([video_item(f"v{i}") for i in range(5)],
                              {f"v{i}": [{"items": [thread(i * 10 + j, j) for j in range(3)]}] for i in range(5)})
        info: list[str] = []
        with env_key():
            records = collect_youtube.search(session, CFG, "best running shoes", SINCE, info=info)
        self.assertEqual(len(records), 15)
        self.assertEqual(len(session.calls), 6)
        self.assertEqual(info, ["youtube read the top comments of 5 videos; spent 6 quota units (1 search.list call at 1 each, "
                                "5 commentThreads pages at 1 each; max_units 600)."])

    def test_search_list_cost_from_config_is_counted(self):
        cfg = {"youtube": dict(CFG["youtube"], search_list_cost=100, videos=2)}
        session = FakeSession([video_item("v1"), video_item("v2")], {"v1": [{"items": [thread(1)]}], "v2": [{"items": [thread(2)]}]})
        info: list[str] = []
        with env_key():
            collect_youtube.search(session, cfg, "x", SINCE, info=info)
        self.assertIn("spent 102 quota units (1 search.list call at 100 each, 2 commentThreads pages at 1 each; max_units 600)", info[0])

    def test_the_run_stops_before_max_units(self):
        cfg = {"youtube": dict(CFG["youtube"], max_units=3)}
        session = FakeSession([video_item(f"v{i}") for i in range(5)], {f"v{i}": [{"items": [thread(i)]}] for i in range(5)})
        with env_key(), self.assertLogs("ppf", level="WARNING") as logs:
            records = collect_youtube.search(session, cfg, "x", SINCE)
        # 1 search + 2 comment pages = 3 units; the third video would make 4. One warning, not one per video.
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(len(records), 2)
        stops = [line for line in logs.output if "stopping before max_units (3)" in line]
        self.assertEqual(len(stops), 1, logs.output)
        self.assertIn("3 of 5 videos not read", stops[0])

    def test_a_max_units_below_the_search_cost_sends_nothing(self):
        cfg = {"youtube": dict(CFG["youtube"], max_units=0)}
        session = FakeSession([video_item("v1")])
        with env_key():
            records = collect_youtube.search(session, cfg, "x", SINCE)
        self.assertEqual(records, [])
        self.assertEqual(session.calls, [])

    def test_more_than_100_comments_per_video_pages_and_each_page_costs_a_unit(self):
        cfg = {"youtube": dict(CFG["youtube"], comments_per_video=150, videos=1)}
        pages = [{"items": [thread(n) for n in range(100)], "nextPageToken": "p2"}, {"items": [thread(n) for n in range(100, 200)]}]
        session = FakeSession([video_item("v1")], {"v1": pages})
        info: list[str] = []
        with env_key():
            records = collect_youtube.search(session, cfg, "x", SINCE, info=info)
        self.assertEqual(len(records), 150)
        self.assertEqual(session.calls[2][1]["pageToken"], "p2")
        self.assertIn("3 quota units (1 search.list call at 1 each, 2 commentThreads pages at 1 each", info[0])

    def test_the_total_is_logged(self):
        session = FakeSession([video_item("v1")], {"v1": [{"items": [thread(1)]}]})
        with env_key(), self.assertLogs("ppf", level="INFO") as logs:
            collect_youtube.search(session, CFG, "x", SINCE)
        self.assertTrue(any("spent 2 quota units" in line for line in logs.output), logs.output)


class ErrorHandlingTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(collect_youtube.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_comments_disabled_on_a_video_is_skipped_with_a_log_line_not_a_crash(self):
        session = FakeSession([video_item("v1", "Quiet video"), video_item("v2")],
                              {"v1": [api_error(403, "commentsDisabled", "The video has disabled comments.")],
                               "v2": [{"items": [thread(1, 7)]}]})
        info: list[str] = []
        with env_key(), self.assertLogs("ppf", level="INFO") as logs:
            records = collect_youtube.search(session, CFG, "x", SINCE, info=info)
        self.assertEqual([r["score"] for r in records], [7])
        self.assertTrue(any("comments are disabled on video v1" in line for line in logs.output))
        self.assertIn("read the top comments of 1 video, 1 skipped (comments disabled or video unavailable)", info[0])

    def test_a_deleted_or_forbidden_video_is_skipped_not_a_failure(self):
        session = FakeSession([video_item("v1"), video_item("v2"), video_item("v3")],
                              {"v1": [api_error(404, "videoNotFound", "The video identified by the videoId parameter cannot be found.")],
                               "v2": [api_error(403, "forbidden", "insufficient permissions")],
                               "v3": [{"items": [thread(1, 7)]}]})
        info: list[str] = []
        with env_key(), self.assertLogs("ppf", level="INFO") as logs:
            records = collect_youtube.search(session, CFG, "x", SINCE, info=info)
        self.assertEqual([r["score"] for r in records], [7])
        self.assertTrue(any("videoNotFound: cannot read video v1" in line for line in logs.output), logs.output)
        self.assertIn("read the top comments of 1 video, 2 skipped", info[0])

    def test_429_waits_for_retry_after(self):
        slow = fake_response(429, {})
        slow.headers = {"Retry-After": "7"}
        session = mock.Mock()
        session.get.side_effect = [slow, fake_response(200, {"items": []})]
        with mock.patch.object(collect_youtube.time, "sleep") as sleep:
            collect_youtube.api_get(session, "https://x/search", {"key": KEY})
        self.assertEqual(sleep.call_args_list, [mock.call(8.0)])

    def test_quota_exceeded_raises_a_clear_error(self):
        session = FakeSession(api_error(403, "quotaExceeded", "The request cannot be completed because you have exceeded your quota."))
        with env_key():
            with self.assertRaises(collect_youtube.QuotaExceeded) as ctx:
                collect_youtube.search(session, CFG, "x", SINCE)
        self.assertIn("quotaExceeded", str(ctx.exception))
        self.assertIsInstance(ctx.exception, RuntimeError)  # ppf.py catches RuntimeError and marks the source failed

    def test_a_bad_key_raises_the_api_message_without_the_key(self):
        session = FakeSession(api_error(400, "keyInvalid", "API key not valid. Please pass a valid API key."))
        with env_key():
            with self.assertRaises(RuntimeError) as ctx:
                collect_youtube.search(session, CFG, "x", SINCE)
        self.assertIn("API key not valid", str(ctx.exception))
        self.assertNotIn(KEY, str(ctx.exception))

    def test_5xx_and_429_are_retried_then_given_up(self):
        answers = [fake_response(503, {}), fake_response(429, {}), fake_response(200, {"items": []})]
        session = mock.Mock()
        session.get.side_effect = answers
        data = collect_youtube.api_get(session, "https://x/search", {"key": KEY})
        self.assertEqual(data, {"items": []})
        self.assertEqual(session.get.call_count, 3)
        session.get.side_effect = [fake_response(500, {})] * 2
        with self.assertRaises(RuntimeError):
            collect_youtube.api_get(session, "https://x/search", {"key": KEY}, max_tries=2)

    def test_a_connection_error_message_never_shows_the_key(self):
        common.SECRETS.add(KEY)
        self.addCleanup(common.SECRETS.discard, KEY)
        session = mock.Mock()
        session.get.side_effect = requests.ConnectionError(f"https://x/search?key={KEY} refused")
        with self.assertRaises(RuntimeError) as ctx:
            collect_youtube.api_get(session, "https://x/search", {"key": KEY}, max_tries=1)
        self.assertNotIn(KEY, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_a_question_with_no_words_is_refused_before_any_request(self):
        session = FakeSession()
        with env_key():
            with self.assertRaises(ValueError):
                collect_youtube.search(session, CFG, "   ", SINCE)
        self.assertEqual(session.calls, [])

    def test_a_non_dict_json_answer_is_an_empty_page(self):
        session = mock.Mock()
        session.get.return_value = fake_response(200, [])
        self.assertEqual(collect_youtube.api_get(session, "https://x/search", {}), {})


class MissingKeyTests(unittest.TestCase):
    def setUp(self):
        collect_youtube._WARNED_NO_KEY = False
        self.cfg = load_config()

    def test_without_the_key_the_source_is_skipped_with_a_note_and_no_request(self):
        session = FakeSession()
        notes: list[str] = []
        with no_key():
            records = collect_youtube.search(session, CFG, "x", SINCE, notes=notes)
        self.assertEqual(records, [])
        self.assertEqual(session.calls, [])
        self.assertEqual(notes, ["youtube skipped: YOUTUBE_API_KEY is empty in .env (see README, Getting a YouTube API key)."])

    def test_hn_and_yt_without_the_key_still_prints_hacker_news_and_says_youtube_was_skipped(self):
        hn_record = common.make_record(source="hackernews", url="https://news.ycombinator.com/item?id=1", title="Running shoes thread",
                                       body="x", score=120, comments=40, created_at="2026-11-01T00:00:00Z", fetched_at=FETCHED)
        store = Store(":memory:")
        try:
            with no_key(), mock.patch.object(common, "load_env"), \
                 mock.patch.object(collect_hn, "search", return_value=[hn_record]) as hn:
                result = ppf.run_search(self.cfg, "running shoes", ["hn", "yt"], "12m", True, 30, None, None, store=store,
                                        session=FakeSession())
            self.assertTrue(hn.called)
            text = ppf.format_md(result)
            self.assertIn("hackernews (1 records, fetched)", text)
            self.assertIn("youtube (0 records, fetched; youtube skipped: YOUTUBE_API_KEY is empty in .env", text)
            self.assertIn("[Running shoes thread](https://news.ycombinator.com/item?id=1)", text)
            self.assertIn("youtube skipped: YOUTUBE_API_KEY is empty", result["meta"]["note"])
            self.assertIn("youtube skipped", result["meta"]["per_source"]["youtube"]["note"])
            self.assertNotIn("error", result["meta"]["per_source"]["youtube"])
            # A skipped source is a partial answer: not served from the cache next time.
            self.assertIsNone(store.cached("running shoes", "youtube", ppf.search_scope(self.cfg, "12m", None, None), 24,
                                           datetime.now(timezone.utc)))
        finally:
            store.close()

    def test_check_key_without_a_key_fails_with_the_readme_pointer(self):
        out = io.StringIO()
        with no_key(), redirect_stdout(out):
            rc = collect_youtube.check_key(CFG)
        self.assertEqual(rc, 1)
        self.assertIn("YOUTUBE_API_KEY is empty", out.getvalue())

    def test_sweep_main_without_a_key_writes_nothing_and_exits_zero(self):
        with no_key(), mock.patch.object(collect_youtube, "load_env"), mock.patch.object(collect_youtube, "write_jsonl") as wj, \
             mock.patch.object(collect_youtube, "setup_logging"):
            self.assertEqual(collect_youtube.main([]), 0)
        wj.assert_not_called()


class RedactionTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(common.SECRETS.discard, KEY)
        patcher = mock.patch.object(collect_youtube.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_load_env_registers_the_youtube_key_as_a_secret(self):
        with env_key(), mock.patch.object(common, "load_dotenv"):
            common.load_env()
        self.assertIn(KEY, common.SECRETS)
        self.assertIn("YOUTUBE_API_KEY", common.SECRET_NAMES)
        self.assertEqual(common.redact(f"url?key={KEY}&q=x"), "url?key=***&q=x")

    def test_the_key_never_appears_in_markdown_json_or_log_output(self):
        cfg = load_config()
        session = FakeSession([video_item("v1", "Best Running Shoes", "Shoe Reviewer")], {"v1": [{"items": [THREAD]}]})
        store = Store(":memory:")
        err = io.StringIO()
        handler = __import__("logging").StreamHandler(err)
        common.log.addHandler(handler)
        self.addCleanup(common.log.removeHandler, handler)
        try:
            with env_key(), mock.patch.object(common, "load_dotenv"):
                result = ppf.run_search(cfg, "running shoes", ["yt"], "12m", True, 30, None, None, store=store, session=session)
        finally:
            store.close()
        md = ppf.format_md(result)
        js = ppf.format_json(result)
        self.assertIn(KEY, session.calls[0][1]["key"])  # it was sent to the API...
        for text in (md, js, err.getvalue()):
            self.assertNotIn(KEY, text)  # ...and nowhere else
        self.assertIn("412 likes", md)
        self.assertIn("37 replies", md)
        self.assertIn("date 2026-03-05", md)
        self.assertIn("(https://www.youtube.com/watch?v=dQw4w9WgXcQ&lc=UgzCommentIdAbc123)", md)
        self.assertIn("**Moderate** · youtube", md)
        self.assertIn(collect_youtube.ATTRIBUTION, md)
        self.assertIn("https://www.youtube.com/t/terms", md)
        self.assertIn("https://policies.google.com/privacy", md)
        self.assertIn(collect_youtube.ATTRIBUTION, result["meta"]["attribution"])
        self.assertIn("youtube read the top comments of 1 video; spent 2 quota units", md)

    def test_check_key_reports_the_quota_facts_and_hides_the_key(self):
        out = io.StringIO()
        with env_key(), mock.patch.object(collect_youtube.requests, "Session", return_value=FakeSession()), redirect_stdout(out):
            rc = collect_youtube.check_key(CFG)
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("OK: key accepted", text)
        self.assertIn("not reported by the API", text)
        self.assertIn("10,000 units a day", text)
        self.assertIn("100 search.list calls", text)
        self.assertIn("this check spent 1 unit", text)
        self.assertIn(collect_youtube.QUOTAS_PAGE, text)
        self.assertNotIn(KEY, text)

    def test_check_key_with_a_rejected_key_prints_the_api_message_only(self):
        out = io.StringIO()
        session = mock.Mock()
        session.get.return_value = api_error(400, "keyInvalid", "API key not valid. Please pass a valid API key.")
        with env_key(), mock.patch.object(collect_youtube.requests, "Session", return_value=session), redirect_stdout(out):
            rc = collect_youtube.check_key(CFG)
        self.assertEqual(rc, 1)
        self.assertIn("FAILED: youtube API error HTTP 400 keyInvalid: API key not valid", out.getvalue())
        self.assertNotIn(KEY, out.getvalue())


class StoragePolicyTests(unittest.TestCase):
    def test_store_purge_removes_old_rows_of_one_source_only(self):
        store = Store(":memory:")
        try:
            old = common.make_record(source="youtube", url="https://www.youtube.com/watch?v=a&lc=1", title="old", body="",
                                     fetched_at="2026-09-01T00:00:00+00:00")
            new = common.make_record(source="youtube", url="https://www.youtube.com/watch?v=a&lc=2", title="new", body="",
                                     fetched_at="2026-11-25T00:00:00+00:00")
            other = common.make_record(source="hackernews", url="https://news.ycombinator.com/item?id=1", title="hn", body="",
                                       fetched_at="2026-09-01T00:00:00+00:00")
            store.upsert([old, new, other])
            store.log_query("q", "youtube", "scope", "2026-09-01T00:00:00+00:00", [old, new])
            gone = store.purge("youtube", "2026-10-27T00:00:00+00:00")
            self.assertEqual(gone, 1)
            self.assertEqual(store.counts(), {"hackernews": 1, "youtube": 1})
            hits = store.conn.execute("SELECT url FROM query_hits").fetchall()
            self.assertEqual([h["url"] for h in hits], ["https://www.youtube.com/watch?v=a&lc=2"])
        finally:
            store.close()

    def test_search_purges_youtube_rows_older_than_store_days(self):
        store = Store(":memory:")
        try:
            old = common.make_record(source="youtube", url="https://www.youtube.com/watch?v=a&lc=1", title="old", body="",
                                     fetched_at=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat())
            store.upsert([old])
            session = FakeSession([video_item("v1")], {"v1": [{"items": [thread(1)]}]})
            with env_key(), mock.patch.object(collect_youtube.time, "sleep"):
                collect_youtube.search(session, CFG, "x", SINCE, store=store)
            self.assertEqual(store.counts(), {})  # the new records are stored by ppf.py, not by search()
            cfg = {"youtube": dict(CFG["youtube"], store_days=60)}
            store.upsert([old])
            with env_key(), mock.patch.object(collect_youtube.time, "sleep"):
                collect_youtube.search(FakeSession([video_item("v1")], {"v1": [{"items": [thread(1)]}]}), cfg, "x", SINCE, store=store)
            self.assertEqual(store.counts(), {"youtube": 1})
        finally:
            store.close()

    def test_purge_without_a_store_is_a_no_op(self):
        self.assertEqual(collect_youtube.purge_old_rows(None, CFG), 0)


class ScoringAndRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_yt_is_registered_with_likes_and_replies_but_not_a_default_source(self):
        self.assertIs(ppf.SOURCES["yt"], collect_youtube)
        self.assertEqual(ppf.ALIASES["youtube"], "yt")
        self.assertEqual(ppf.UNITS["youtube"], "likes")
        self.assertEqual(ppf.COMMENT_UNITS["youtube"], "replies")
        self.assertNotIn("yt", self.cfg["search"]["default_sources"])
        self.assertEqual(ppf.parse_sources("hn,yt"), ["hn", "yt"])
        self.assertEqual(ppf.parse_sources("youtube"), ["yt"])
        self.assertIn("yt", ppf.build_parser()._subparsers._group_actions[0].choices["search"].format_help())

    def test_config_holds_the_youtube_block_thresholds_and_cap(self):
        yt = self.cfg["youtube"]
        self.assertEqual(yt["endpoint"], "https://www.googleapis.com/youtube/v3")
        self.assertEqual((yt["videos"], yt["comments_per_video"], yt["max_units"]), (5, 100, 600))
        self.assertEqual(yt["store_days"], 30)
        self.assertGreater(float(yt["delay_seconds"]), 0)
        scoring = self.cfg["scoring"]
        self.assertEqual(scoring["source_thresholds"]["youtube"], {"moderate": 20, "strong": 200})
        self.assertEqual(scoring["label_caps"]["youtube"], "Moderate")
        self.assertIn("data/raw/youtube.jsonl", self.cfg["paths"]["sources"])

    def test_attribution_links_to_youtube_terms_and_google_privacy_policy(self):
        self.assertIn("https://www.youtube.com/t/terms", collect_youtube.ATTRIBUTION)
        self.assertIn("https://policies.google.com/privacy", collect_youtube.ATTRIBUTION)

    def test_no_youtube_row_alone_is_labeled_above_moderate(self):
        now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        pairs = [(thread(1, 100000, text="Every running shoe I buy wears through at the heel in two months"), VIDEO),
                 (thread(2, 25, text="Why do trail shoes never come in wide sizes"), VIDEO),
                 (thread(3, 2, text="Lost another toenail to a narrow toe box"), VIDEO)]
        for t, _ in pairs:
            t["snippet"]["topLevelComment"]["snippet"]["publishedAt"] = "2026-11-20T00:00:00Z"
            t["snippet"]["topLevelComment"]["snippet"]["updatedAt"] = "2026-11-20T00:00:00Z"
        records = collect_youtube.threads_to_records(pairs, FETCHED)
        rows, _ = merge_and_score.rank(records, self.cfg["scoring"], as_of=now, mode="search")
        labels = {r["best_url"].rsplit("=", 1)[-1]: r["evidence_label"] for r in rows}
        self.assertEqual(labels, {"Ugz0001": "Moderate", "Ugz0002": "Moderate", "Ugz0003": "Anecdotal"})

    def test_the_same_problem_on_youtube_and_another_source_can_still_be_strong(self):
        now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        yt = collect_youtube.to_record(THREAD, VIDEO, FETCHED)
        gh = common.make_record(source="github", url="https://github.com/acme/x/issues/1", title=yt["title"], body="",
                                score=3, comments=1, created_at="2026-11-01T00:00:00Z", last_activity="2026-11-20T00:00:00Z",
                                status="open", fetched_at=FETCHED)
        rows, _ = merge_and_score.rank([yt, gh], self.cfg["scoring"], as_of=now, mode="search")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence_label"], "Strong")


class SweepTests(unittest.TestCase):
    def test_sweep_writes_youtube_jsonl_with_one_budget_for_all_keywords(self):
        cfg = {"youtube": dict(CFG["youtube"], sweep_max_units=4), "keywords": ["a", "b", "c"], "months_back": 1,
               "paths": {"raw_dir": "data/raw"}}
        session = FakeSession([video_item("v1")], {"v1": [{"items": [thread(1, 9)]}, {"items": [thread(2, 9)]}, {"items": [thread(3, 9)]}]})
        with env_key(), mock.patch.object(collect_youtube, "load_env"), mock.patch.object(collect_youtube, "load_config", return_value=cfg), \
             mock.patch.object(collect_youtube, "setup_logging"), mock.patch.object(collect_youtube.requests, "Session", return_value=session), \
             mock.patch.object(collect_youtube.time, "sleep"), mock.patch.object(collect_youtube, "write_jsonl", return_value=2) as wj:
            self.assertEqual(collect_youtube.main([]), 0)
        # budget 4: keyword a = 2 units, keyword b = 2 units, keyword c cannot afford its search.list call.
        self.assertEqual(len(session.calls), 4)
        self.assertEqual(wj.call_args.args[0], "data/raw/youtube.jsonl")
        self.assertEqual(len(wj.call_args.args[1]), 2)


if __name__ == "__main__":
    unittest.main()
