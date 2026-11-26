# Copyright (c) 2026 Sean R Fulton. All rights reserved. No permission is granted to use, copy, modify,
# or distribute this software without written permission.

"""Unit tests for issue #23: the Stack Exchange site picked from the question when --site is absent.

Order: --site, then the stackexchange.sites map in config.yaml (first matching key in config order,
whole words, ignoring case), then stackexchange.site. No network: fetch_items or common.get_json is
mocked, the store is in memory and time.sleep is patched out.
"""

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from unittest import mock

import collect_se
import common
import ppf
from common import has_phrase, load_config, words_of
from store import Store

SINCE = datetime(2024, 11, 25, tzinfo=timezone.utc)

CFG = {
    "stackexchange": {
        "endpoint": "https://api.stackexchange.com/2.3/search/advanced",
        "site": "salesforce",
        "sites": {
            "salesforce": "salesforce",
            "apex": "salesforce",
            "bread": "cooking",
            "recipe": "cooking",
            "running": "fitness",
            "money": "money",
            "va claim": "law",
            "bike": "bicycles",
            "flow": "salesforce",
        },
        "pagesize": 100, "delay_seconds": 0, "body_max_chars": 2000,
        "search_min_score": 0, "search_max_pages": 2, "search_in": "title",
    }
}

ITEM = {
    "link": "https://cooking.stackexchange.com/questions/4242/how-do-i-keep-bread-from-going-stale",
    "title": "How do I keep bread from going stale?",
    "body": "<p>My loaf is hard the next morning.</p>",
    "score": 12, "answer_count": 3, "view_count": 900,
    "creation_date": 1735689600, "last_activity_date": 1735689600, "is_answered": True,
}


class WordHelpersTest(unittest.TestCase):
    def test_words_of_keeps_accents_and_drops_punctuation(self):
        self.assertEqual(words_of("crème brûlée, VA_claim?"), ["crème", "brûlée", "va", "claim"])

    def test_has_phrase_matches_whole_words_in_order(self):
        words = words_of("My VA claim for a bike")
        self.assertTrue(has_phrase(words, "va claim"))
        self.assertTrue(has_phrase(words, "BIKE"))
        self.assertFalse(has_phrase(words, "claim va"))
        self.assertFalse(has_phrase(words, "bik"))
        self.assertFalse(has_phrase(words, ""))


class TopicSiteTest(unittest.TestCase):
    def test_whole_word_match_ignoring_case(self):
        self.assertEqual(collect_se.topic_site(CFG, "how to keep BREAD from going stale"), ("cooking", "bread"))

    def test_substring_is_not_a_match(self):
        self.assertEqual(collect_se.topic_site(CFG, "breadth of the running track"), ("fitness", "running"))
        self.assertEqual(collect_se.topic_site(CFG, "breadth first search"), ("", ""))

    def test_phrase_key_needs_the_words_next_to_each_other(self):
        self.assertEqual(collect_se.topic_site(CFG, "my VA claim was denied"), ("law", "va claim"))
        self.assertEqual(collect_se.topic_site(CFG, "va hospital claim form"), ("", ""))

    def test_several_matches_first_in_config_order_wins(self):
        # "bread" is listed before "running" and "money" in CFG, whatever order the words have in the question.
        self.assertEqual(collect_se.topic_site(CFG, "money for running shoes and bread"), ("cooking", "bread"))
        self.assertEqual(collect_se.topic_site(CFG, "apex trigger for bread orders"), ("salesforce", "apex"))

    def test_no_match_or_no_map_gives_nothing(self):
        self.assertEqual(collect_se.topic_site(CFG, "quantum entanglement"), ("", ""))
        self.assertEqual(collect_se.topic_site({"stackexchange": {"site": "salesforce"}}, "bread"), ("", ""))
        self.assertEqual(collect_se.topic_site({"stackexchange": {"site": "salesforce", "sites": None}}, "bread"), ("", ""))

    def test_empty_value_is_skipped(self):
        cfg = {"stackexchange": {"site": "salesforce", "sites": {"bread": "", "recipe": "cooking"}}}
        self.assertEqual(collect_se.topic_site(cfg, "bread recipe"), ("cooking", "recipe"))


class ChooseSiteTest(unittest.TestCase):
    def test_site_option_wins_over_the_map(self):
        site, why = collect_se.choose_site(CFG, "bread recipe", "stackoverflow")
        self.assertEqual(site, "stackoverflow")
        self.assertEqual(why, "stackexchange searched stackoverflow.com (from --site).")

    def test_map_match_names_the_site_and_the_key(self):
        site, why = collect_se.choose_site(CFG, "how to keep bread from going stale")
        self.assertEqual(site, "cooking")
        self.assertEqual(why, "stackexchange searched cooking.stackexchange.com (site map in config.yaml matched 'bread').")

    def test_no_match_uses_the_config_default_and_says_so(self):
        site, why = collect_se.choose_site(CFG, "quantum entanglement")
        self.assertEqual(site, "salesforce")
        self.assertIn("stackexchange searched salesforce.stackexchange.com (config default", why)
        self.assertIn("--site", why)

    def test_site_host(self):
        self.assertEqual(collect_se.site_host("cooking"), "cooking.stackexchange.com")
        self.assertEqual(collect_se.site_host("salesforce"), "salesforce.stackexchange.com")
        self.assertEqual(collect_se.site_host("stackoverflow"), "stackoverflow.com")
        self.assertEqual(collect_se.site_host("mathoverflow.net"), "mathoverflow.net")
        self.assertEqual(collect_se.site_host("meta.stackoverflow"), "meta.stackoverflow.com")
        self.assertEqual(collect_se.site_host("es.stackoverflow"), "es.stackoverflow.com")
        self.assertEqual(collect_se.site_host("meta.cooking"), "meta.cooking.stackexchange.com")
        self.assertEqual(collect_se.site_host("cooking.stackexchange.com"), "cooking.stackexchange.com")

    def test_blank_site_option_counts_as_absent(self):
        self.assertEqual(collect_se.choose_site(CFG, "bread recipe", "  ")[0], "cooking")
        self.assertEqual(collect_se.choose_site(CFG, "bread recipe", None)[0], "cooking")


class SearchTest(unittest.TestCase):
    """collect_se.search hands the chosen site to fetch_items and the sentence to the info list."""

    def run_search(self, query, **options):
        info = []
        with mock.patch.object(collect_se, "fetch_items", return_value=[ITEM]) as fi:
            records = collect_se.search(object(), CFG, query, SINCE, info=info, **options)
        return fi.call_args.args[4], info, records

    def test_map_match(self):
        site, info, records = self.run_search("how to keep bread from going stale")
        self.assertEqual(site, "cooking")
        self.assertEqual(info, ["stackexchange searched cooking.stackexchange.com (site map in config.yaml matched 'bread')."])
        self.assertEqual(records[0]["source"], "stackexchange")

    def test_no_match_default(self):
        site, info, _ = self.run_search("quantum entanglement")
        self.assertEqual(site, "salesforce")
        self.assertIn("config default", info[0])

    def test_site_option_wins(self):
        site, info, _ = self.run_search("how to keep bread from going stale", site="stackoverflow")
        self.assertEqual(site, "stackoverflow")
        self.assertEqual(info, ["stackexchange searched stackoverflow.com (from --site)."])

    def test_several_matches_first_in_config_order(self):
        site, _, _ = self.run_search("running out of money")
        self.assertEqual(site, "fitness")

    def test_works_without_an_info_list(self):
        with mock.patch.object(collect_se, "fetch_items", return_value=[ITEM]):
            records = collect_se.search(object(), CFG, "bread", SINCE)
        self.assertEqual(len(records), 1)


class RealConfigTest(unittest.TestCase):
    """The shipped config.yaml: the seeded map, its order, and the YAML boolean trap."""

    def setUp(self):
        self.cfg = load_config()
        self.sites = self.cfg["stackexchange"]["sites"]

    def test_every_key_and_value_is_a_string(self):
        # YAML reads bare yes/no/on/off as booleans; such a key would never match a word.
        for key, value in self.sites.items():
            with self.subTest(key=key):
                self.assertIsInstance(key, str)
                self.assertIsInstance(value, str)
                self.assertTrue(value.strip())

    def test_default_site_is_salesforce(self):
        self.assertEqual(self.cfg["stackexchange"]["site"], "salesforce")

    def test_seeded_topics(self):
        expected = {
            "how to keep bread from going stale": "cooking",
            "best RECIPE for sourdough": "cooking",
            "running shoes for flat feet": "fitness",
            "gym workout plan": "fitness",
            "budget for a new car": "money",
            "tax on a side business": "money",
            "home repair of a leaking roof": "diy",
            "plumbing under the sink": "diy",
            "visa for a flight to Japan": "travel",
            "VA claim denied, what next": "law",
            "legal help with a landlord": "law",
            "parenting a toddler who will not sleep": "parenting",
            "garden tomatoes wilting": "gardening",
            "hiking boots for camping": "outdoors",
            "bike chain keeps slipping": "bicycles",
            "salesforce flow fault email": "salesforce",
            "apex trigger recursion": "salesforce",
            "flow fault email": "salesforce",
        }
        for query, site in expected.items():
            with self.subTest(query=query):
                self.assertEqual(collect_se.choose_site(self.cfg, query)[0], site)

    def test_salesforce_question_with_no_flags_stays_on_salesforce(self):
        self.assertEqual(collect_se.choose_site(self.cfg, "report filter on a lookup field")[0], "salesforce")
        self.assertEqual(collect_se.choose_site(self.cfg, "Salesforce report export limit")[0], "salesforce")

    def test_vague_flow_is_listed_last_so_other_topics_win(self):
        self.assertEqual(list(self.sites)[-1], "flow")
        self.assertEqual(collect_se.choose_site(self.cfg, "cash flow budget spreadsheet")[0], "money")


class PpfHeaderTest(unittest.TestCase):
    """End to end through ppf with a faked API: the header and JSON notes name the site and why."""

    def setUp(self):
        self.cfg = load_config()
        self.sites_sent = []

        def fake_get_json(session, url, params=None, **kw):
            self.sites_sent.append(params["site"])
            return {"items": [ITEM], "has_more": False, "quota_remaining": 250}, None

        patches = [mock.patch.object(collect_se, "get_json", fake_get_json),
                   mock.patch.object(collect_se.time, "sleep"),
                   mock.patch.object(common, "load_env")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def run_it(self, query, **kw):
        return ppf.run_search(self.cfg, query, ["se"], "24m", True, 30, None, None, store=self.store, **kw)

    def test_bread_question_searches_cooking_and_the_header_says_so(self):
        result = self.run_it("how to keep bread from going stale")
        self.assertEqual(self.sites_sent, ["cooking"])
        header = ppf.format_md(result).splitlines()[2]
        self.assertIn("stackexchange searched cooking.stackexchange.com (site map in config.yaml matched 'bread')", header)
        self.assertEqual(result["meta"]["per_source"]["stackexchange"]["note"],
                         "stackexchange searched cooking.stackexchange.com (site map in config.yaml matched 'bread').")
        self.assertTrue(result["meta"]["note"].startswith("stackexchange searched cooking.stackexchange.com"))
        self.assertTrue(any("CC BY-SA" in a for a in result["meta"]["attribution"]))

    def test_salesforce_question_with_no_flags_searches_salesforce(self):
        result = self.run_it("flow fault email goes to the wrong user")
        self.assertEqual(self.sites_sent, ["salesforce"])
        self.assertIn("salesforce.stackexchange.com", result["meta"]["per_source"]["stackexchange"]["note"])

    def test_site_flag_overrides_the_map(self):
        result = self.run_it("how to keep bread from going stale", site="stackoverflow")
        self.assertEqual(self.sites_sent, ["stackoverflow"])
        self.assertIn("stackexchange searched stackoverflow.com (from --site)", ppf.format_md(result))

    def test_cache_scope_says_auto_without_site(self):
        self.assertIn("|site=auto|", ppf.search_scope(self.cfg, "24m", None, None))
        self.assertIn("|site=cooking|", ppf.search_scope(self.cfg, "24m", None, None, site="cooking"))

    def test_command_line_json_carries_the_note(self):
        self.cfg["paths"]["store"] = ":memory:"
        out = io.StringIO()
        with mock.patch.object(common, "load_config", return_value=self.cfg), redirect_stdout(out):
            rc = ppf.main(["search", "how to keep bread from going stale", "--sources", "se", "--refresh", "--format", "json"])
        self.assertEqual(rc, 0)
        meta = json.loads(out.getvalue())["meta"]
        self.assertIn("cooking.stackexchange.com", meta["per_source"]["stackexchange"]["note"])
        self.assertEqual(self.sites_sent, ["cooking"])


if __name__ == "__main__":
    unittest.main()
