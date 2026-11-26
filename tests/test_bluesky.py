# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Unit tests for the Bluesky source (issue #24): record mapping, url building, since handling, paging,
the host fallback, an empty result, the Moderate cap, and bs staying out of the default sources.
No network: common.get_json is mocked and time.sleep is patched out."""

import unittest
from datetime import datetime, timezone
from unittest import mock

import requests

import collect_bluesky
import merge_and_score
import ppf
from common import FIELDS, RateLimited, load_config, validate_record

SINCE = datetime(2026, 8, 26, 0, 0, 0, tzinfo=timezone.utc)
FETCHED = "2026-11-26T00:00:00+00:00"

POST = {
    "uri": "at://did:plc:j2kmiyhld5btzozgzwy3lc2m/app.bsky.feed.post/3n33smmejik2s",
    "cid": "bafyreia",
    "author": {"did": "did:plc:j2kmiyhld5btzozgzwy3lc2m", "handle": "runner.example.com", "displayName": "A Runner"},
    "record": {
        "$type": "app.bsky.feed.post",
        "createdAt": "2026-11-24T23:34:00.899Z",
        "langs": ["en"],
        "text": "Fellow running idiots: I need new running shoes.\nBrooks Ghost 18, Asics Cumulus 28, or a secret third thing? "
                "My knees hurt after 10k in the old pair and the toe box is too narrow.",
    },
    "replyCount": 22,
    "repostCount": 0,
    "likeCount": 41,
    "quoteCount": 1,
    "indexedAt": "2026-11-24T23:34:01.000Z",
}

CFG = {
    "bluesky": {
        "endpoint": "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
        "fallback_endpoint": "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts",
        "sort": "top", "limit": 100, "max_pages": 3, "delay_seconds": 0,
        "search_min_likes": 0, "min_likes": 5, "title_chars": 120, "body_max_chars": 2000,
    }
}


def post(n: int, likes: int = 1) -> dict:
    return dict(POST, uri=f"at://did:plc:x/app.bsky.feed.post/rkey{n}", likeCount=likes,
                record=dict(POST["record"], text=f"Post number {n} about running shoes"))


def page(posts: list[dict], cursor: str | None) -> tuple[dict, None]:
    data = {"posts": posts, "hitsTotal": 999}
    if cursor is not None:
        data["cursor"] = cursor
    return data, None


def forbidden(code: int = 403) -> RateLimited:
    return RateLimited(f"gave up after 1 tries: {code} <html><body><h1>{code} Forbidden</h1></body></html>")


class RecordMappingTests(unittest.TestCase):
    def test_to_record_maps_the_post_view_to_the_11_fields(self):
        rec = collect_bluesky.to_record(POST, FETCHED, 2000)
        self.assertEqual(list(rec), FIELDS)
        self.assertEqual(validate_record(rec), [])
        self.assertEqual(rec["source"], "bluesky")
        self.assertEqual(rec["url"], "https://bsky.app/profile/runner.example.com/post/3n33smmejik2s")
        self.assertEqual(rec["score"], 41)
        self.assertEqual(rec["comments"], 22)
        self.assertEqual(rec["created_at"], "2026-11-24T23:34:00.899Z")
        self.assertEqual(rec["body"], POST["record"]["text"])
        self.assertEqual((rec["views"], rec["last_activity"], rec["status"]), ("", "", ""))
        self.assertEqual(rec["fetched_at"], FETCHED)

    def test_title_is_the_first_120_characters_on_one_line(self):
        rec = collect_bluesky.to_record(POST, FETCHED, 2000)
        self.assertEqual(len(rec["title"]), 120)
        self.assertNotIn("\n", rec["title"])
        self.assertTrue(rec["title"].startswith("Fellow running idiots: I need new running shoes. Brooks Ghost 18"))
        short = collect_bluesky.to_record(dict(POST, record=dict(POST["record"], text="  Short  post ")), FETCHED, 2000)
        self.assertEqual(short["title"], "Short post")
        self.assertEqual(short["body"], "Short  post")
        tiny = collect_bluesky.to_record(POST, FETCHED, 2000, title_chars=10)
        self.assertEqual(tiny["title"], "Fellow run")

    def test_body_is_cut_at_body_max_chars(self):
        rec = collect_bluesky.to_record(POST, FETCHED, 50)
        self.assertEqual(len(rec["body"]), 50)

    def test_posts_without_text_or_under_the_like_floor_are_skipped(self):
        self.assertIsNone(collect_bluesky.to_record(dict(POST, record={"createdAt": "x"}), FETCHED, 2000))
        self.assertIsNone(collect_bluesky.to_record(dict(POST, record=dict(POST["record"], text="   ")), FETCHED, 2000))
        self.assertIsNone(collect_bluesky.to_record(dict(POST, likeCount=4), FETCHED, 2000, min_likes=5))
        self.assertIsNotNone(collect_bluesky.to_record(dict(POST, likeCount=5), FETCHED, 2000, min_likes=5))

    def test_missing_counts_stay_empty_not_zero(self):
        stripped = {k: v for k, v in POST.items() if k not in ("likeCount", "replyCount")}
        rec = collect_bluesky.to_record(stripped, FETCHED, 2000)
        self.assertEqual((rec["score"], rec["comments"]), ("", ""))
        self.assertEqual(validate_record(rec), [])

    def test_posts_to_records_dedupes_by_url_and_sorts_most_liked_first(self):
        records = collect_bluesky.posts_to_records([post(1, 3), post(2, 90), dict(post(1, 3)), post(3, 10)], FETCHED, 2000)
        self.assertEqual([r["score"] for r in records], [90, 10, 3])
        self.assertEqual(len({r["url"] for r in records}), 3)

    def test_posts_to_records_drops_posts_with_no_url(self):
        bad = dict(POST, uri="at://did:plc:x/app.bsky.feed.post")  # no record key
        records = collect_bluesky.posts_to_records([bad, POST], FETCHED, 2000)
        self.assertEqual([r["url"] for r in records], ["https://bsky.app/profile/runner.example.com/post/3n33smmejik2s"])


class UrlTests(unittest.TestCase):
    def test_rkey_is_the_last_part_of_the_post_uri(self):
        self.assertEqual(collect_bluesky.rkey_of("at://did:plc:abc/app.bsky.feed.post/3n33smmejik2s"), "3n33smmejik2s")
        self.assertEqual(collect_bluesky.rkey_of("at://did:plc:abc/app.bsky.feed.like/3n33smmejik2s"), "")
        self.assertEqual(collect_bluesky.rkey_of("https://bsky.app/profile/x/post/y"), "")
        self.assertEqual(collect_bluesky.rkey_of(""), "")

    def test_url_uses_the_handle_and_falls_back_to_the_did(self):
        self.assertEqual(collect_bluesky.post_url(POST), "https://bsky.app/profile/runner.example.com/post/3n33smmejik2s")
        no_handle = dict(POST, author={"did": "did:plc:j2kmiyhld5btzozgzwy3lc2m"})
        self.assertEqual(collect_bluesky.post_url(no_handle),
                         "https://bsky.app/profile/did:plc:j2kmiyhld5btzozgzwy3lc2m/post/3n33smmejik2s")
        self.assertEqual(collect_bluesky.post_url(dict(POST, author={})), "")
        self.assertEqual(collect_bluesky.post_url(dict(POST, uri="")), "")


class ParamsAndSinceTests(unittest.TestCase):
    def test_build_params_sends_words_top_limit_and_since_as_iso_utc(self):
        p = collect_bluesky.build_params(CFG, "best  running\nshoes", SINCE)
        self.assertEqual(p, {"q": "best running shoes", "sort": "top", "limit": 100, "since": "2026-08-26T00:00:00Z"})

    def test_since_is_converted_to_utc_and_omitted_when_none(self):
        from datetime import timedelta
        local = datetime(2026, 8, 26, 5, 30, 15, 123456, tzinfo=timezone(timedelta(hours=5)))
        self.assertEqual(collect_bluesky.since_param(local), "2026-08-26T00:30:15Z")
        self.assertEqual(collect_bluesky.since_param(datetime(2026, 8, 26)), "2026-08-26T00:00:00Z")
        self.assertEqual(collect_bluesky.since_param(None), "")
        self.assertNotIn("since", collect_bluesky.build_params(CFG, "x", None))

    def test_cursor_is_sent_only_when_given_and_limit_is_clamped(self):
        self.assertNotIn("cursor", collect_bluesky.build_params(CFG, "x", None))
        self.assertEqual(collect_bluesky.build_params(CFG, "x", None, "abc")["cursor"], "abc")
        big = {"bluesky": dict(CFG["bluesky"], limit=500)}
        self.assertEqual(collect_bluesky.build_params(big, "x", None)["limit"], 100)
        zero = {"bluesky": dict(CFG["bluesky"], limit=0)}
        self.assertEqual(collect_bluesky.build_params(zero, "x", None)["limit"], 1)


class PagingTests(unittest.TestCase):
    def setUp(self):
        collect_bluesky.REFUSED.clear()
        self.sleep = mock.patch.object(collect_bluesky.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def test_pages_follow_the_cursor_and_stop_at_max_pages(self):
        answers = [page([post(1)], "c1"), page([post(2)], "c2"), page([post(3)], "c3"), page([post(4)], "c4")]
        with mock.patch.object(collect_bluesky, "get_json", side_effect=answers) as gj:
            posts = collect_bluesky.fetch_posts(object(), CFG, "running shoes", SINCE, 3)
        self.assertEqual(gj.call_count, 3)
        self.assertEqual([p["uri"][-5:] for p in posts], ["rkey1", "rkey2", "rkey3"])
        self.assertNotIn("cursor", gj.call_args_list[0].kwargs["params"])
        self.assertEqual(gj.call_args_list[1].kwargs["params"]["cursor"], "c1")
        self.assertEqual(gj.call_args_list[2].kwargs["params"]["cursor"], "c2")
        for call in gj.call_args_list:
            self.assertEqual(call.kwargs["params"]["since"], "2026-08-26T00:00:00Z")

    def test_paging_stops_when_no_cursor_comes_back(self):
        answers = [page([post(1)], "c1"), page([post(2)], None)]
        with mock.patch.object(collect_bluesky, "get_json", side_effect=answers) as gj:
            posts = collect_bluesky.fetch_posts(object(), CFG, "running shoes", SINCE, 5)
        self.assertEqual(gj.call_count, 2)
        self.assertEqual(len(posts), 2)

    def test_paging_stops_on_an_empty_page_even_with_a_cursor(self):
        answers = [page([post(1)], "c1"), page([], "c2")]
        with mock.patch.object(collect_bluesky, "get_json", side_effect=answers) as gj:
            posts = collect_bluesky.fetch_posts(object(), CFG, "running shoes", SINCE, 5)
        self.assertEqual(gj.call_count, 2)
        self.assertEqual(len(posts), 1)

    def test_a_refused_later_page_keeps_the_first_page_and_says_so(self):
        info: list[str] = []
        answers = [page([post(1), post(2)], "c1"), forbidden(403)]
        with mock.patch.object(collect_bluesky, "get_json", side_effect=answers) as gj:
            posts = collect_bluesky.fetch_posts(object(), CFG, "running shoes", SINCE, 3, info)
        self.assertEqual(gj.call_count, 2)
        self.assertEqual(len(posts), 2)
        self.assertEqual(gj.call_args_list[1].kwargs["max_tries"], 1)
        self.assertEqual(info, ["bluesky kept page 1 only (2 posts): public.api.bsky.app refused page 2 with HTTP 403."])

    def test_empty_result_gives_no_records_and_one_request(self):
        with mock.patch.object(collect_bluesky, "get_json", return_value=page([], None)) as gj:
            records = collect_bluesky.search(object(), CFG, "zxqv nothing", SINCE)
        self.assertEqual(records, [])
        self.assertEqual(gj.call_count, 1)

    def test_delay_between_pages_comes_from_config(self):
        cfg = {"bluesky": dict(CFG["bluesky"], delay_seconds=2.5)}
        answers = [page([post(1)], "c1"), page([post(2)], "c2"), page([post(3)], None)]
        with mock.patch.object(collect_bluesky, "get_json", side_effect=answers), \
             mock.patch.object(collect_bluesky.time, "sleep") as sleep:
            collect_bluesky.fetch_posts(object(), cfg, "x", SINCE, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(2.5), mock.call(2.5)])


class HostFallbackTests(unittest.TestCase):
    def setUp(self):
        collect_bluesky.REFUSED.clear()
        self.addCleanup(collect_bluesky.REFUSED.clear)
        patcher = mock.patch.object(collect_bluesky.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_public_host_is_tried_once_then_the_fallback_answers(self):
        info: list[str] = []
        answers = [forbidden(403), page([post(1)], None)]
        with mock.patch.object(collect_bluesky, "get_json", side_effect=answers) as gj:
            records = collect_bluesky.search(object(), CFG, "running shoes", SINCE, info=info)
        self.assertEqual(len(records), 1)
        first, second = gj.call_args_list
        self.assertEqual(first.args[1], CFG["bluesky"]["endpoint"])
        self.assertEqual(first.kwargs["max_tries"], 1)
        self.assertEqual(second.args[1], CFG["bluesky"]["fallback_endpoint"])
        self.assertNotIn("max_tries", second.kwargs)
        self.assertEqual(info, ["bluesky searched api.bsky.app (public.api.bsky.app refused with HTTP 403)."])
        self.assertIn(CFG["bluesky"]["endpoint"], collect_bluesky.REFUSED)

    def test_a_refused_host_is_not_probed_again_in_the_same_process(self):
        answers = [forbidden(429), page([post(1)], None), page([post(2)], None)]
        with mock.patch.object(collect_bluesky, "get_json", side_effect=answers) as gj:
            collect_bluesky.search(object(), CFG, "first", SINCE)
            collect_bluesky.search(object(), CFG, "second", SINCE)
        self.assertEqual(gj.call_count, 3)
        self.assertEqual(gj.call_args_list[2].args[1], CFG["bluesky"]["fallback_endpoint"])

    def test_no_connection_also_falls_back(self):
        answers = [RuntimeError("gave up after 1 tries: connection refused"), page([post(1)], None)]
        info: list[str] = []
        with mock.patch.object(collect_bluesky, "get_json", side_effect=answers):
            collect_bluesky.search(object(), CFG, "x", SINCE, info=info)
        self.assertEqual(info, ["bluesky searched api.bsky.app (public.api.bsky.app refused with no answer)."])

    def test_without_a_fallback_the_error_is_raised(self):
        cfg = {"bluesky": {k: v for k, v in CFG["bluesky"].items() if k != "fallback_endpoint"}}
        with mock.patch.object(collect_bluesky, "get_json", side_effect=forbidden(403)) as gj:
            with self.assertRaises(RateLimited):
                collect_bluesky.search(object(), cfg, "x", SINCE)
        self.assertEqual(gj.call_count, 1)
        self.assertNotIn("max_tries", gj.call_args.kwargs)

    def test_a_4xx_from_the_public_host_is_our_mistake_and_is_raised(self):
        resp = mock.Mock()
        resp.status_code = 400
        bad = requests.HTTPError("HTTP 400 Bad Request from https://x", response=resp)
        with mock.patch.object(collect_bluesky, "get_json", side_effect=bad) as gj:
            with self.assertRaises(requests.HTTPError):
                collect_bluesky.search(object(), CFG, "x", SINCE)
        self.assertEqual(gj.call_count, 1)

    def test_a_question_with_no_words_is_refused_before_any_request(self):
        with mock.patch.object(collect_bluesky, "get_json") as gj:
            with self.assertRaises(ValueError):
                collect_bluesky.search(object(), CFG, "   ", SINCE)
        gj.assert_not_called()


class ScoringAndRegistrationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config()

    def test_bs_is_registered_with_likes_and_replies_but_not_a_default_source(self):
        self.assertIs(ppf.SOURCES["bs"], collect_bluesky)
        self.assertEqual(ppf.ALIASES["bluesky"], "bs")
        self.assertEqual(ppf.UNITS["bluesky"], "likes")
        self.assertEqual(ppf.COMMENT_UNITS["bluesky"], "replies")
        self.assertNotIn("bs", self.cfg["search"]["default_sources"])
        self.assertEqual(ppf.parse_sources("rd,bs"), ["rd", "bs"])
        self.assertEqual(ppf.parse_sources("bluesky"), ["bs"])

    def test_thresholds_and_cap_live_in_config(self):
        scoring = self.cfg["scoring"]
        self.assertEqual(scoring["source_thresholds"]["bluesky"], {"moderate": 10, "strong": 50})
        self.assertEqual(scoring["label_caps"]["bluesky"], "Moderate")
        self.assertIn("data/raw/bluesky.jsonl", self.cfg["paths"]["sources"])
        self.assertTrue(self.cfg["bluesky"]["endpoint"].startswith("https://public.api.bsky.app/"))
        self.assertEqual(self.cfg["bluesky"]["max_pages"], 3)
        self.assertGreaterEqual(float(self.cfg["bluesky"]["delay_seconds"]), 1.0)

    def test_no_bluesky_row_alone_is_labeled_above_moderate(self):
        now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        fresh = dict(POST, record=dict(POST["record"], createdAt="2026-11-20T00:00:00Z"))
        def with_text(rkey: str, text: str, **counts) -> dict:
            return dict(fresh, uri=f"at://d/app.bsky.feed.post/{rkey}", record=dict(fresh["record"], text=text), **counts)
        # Three different problems, so the ranker does not merge them as one title.
        records = collect_bluesky.posts_to_records(
            [with_text("a", "Every running shoe I buy wears through at the heel in two months", likeCount=100000, replyCount=5000),
             with_text("b", "Why do trail shoes never come in wide sizes", likeCount=12),
             with_text("c", "Lost another toenail to a narrow toe box", likeCount=2)], FETCHED, 2000)
        rows, _ = merge_and_score.rank(records, self.cfg["scoring"], as_of=now, mode="search")
        labels = {r["best_url"].rsplit("/", 1)[-1]: r["evidence_label"] for r in rows}
        self.assertEqual(labels, {"a": "Moderate", "b": "Moderate", "c": "Anecdotal"})
        for r in rows:
            self.assertNotEqual(r["evidence_label"], "Strong")

    def test_the_same_problem_on_bluesky_and_another_source_can_still_be_strong(self):
        from common import make_record
        now = datetime(2026, 11, 26, tzinfo=timezone.utc)
        bs = collect_bluesky.to_record(dict(POST, record=dict(POST["record"], createdAt="2026-11-20T00:00:00Z")), FETCHED, 2000)
        gh = make_record(source="github", url="https://github.com/acme/x/issues/1", title=bs["title"], body="",
                         score=3, comments=1, created_at="2026-11-01T00:00:00Z", last_activity="2026-11-20T00:00:00Z",
                         status="open", fetched_at=FETCHED)
        rows, _ = merge_and_score.rank([bs, gh], self.cfg["scoring"], as_of=now, mode="search")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence_label"], "Strong")

    def test_search_prints_likes_replies_date_and_the_bsky_link(self):
        from store import Store
        store = Store(":memory:")
        try:
            with mock.patch.object(collect_bluesky, "get_json", return_value=page([POST], None)):
                result = ppf.run_search(self.cfg, "running shoes", ["bs"], "3m", True, 30, None, None, store=store,
                                        session=object())
        finally:
            store.close()
        text = ppf.format_md(result)
        self.assertIn("bluesky (1 records, fetched)", text)
        self.assertIn("(https://bsky.app/profile/runner.example.com/post/3n33smmejik2s)", text)
        self.assertIn("41 likes", text)
        self.assertIn("22 replies", text)
        self.assertIn("date 2026-11-24", text)
        self.assertIn("**Moderate** · bluesky", text)
        self.assertIn(collect_bluesky.ATTRIBUTION, text)


if __name__ == "__main__":
    unittest.main()
