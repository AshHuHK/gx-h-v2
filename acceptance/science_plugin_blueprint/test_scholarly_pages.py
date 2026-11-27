"""Publisher paging and scientific coverage. No provider credentials or network required."""
import copy
import json
from pathlib import Path
import runpy
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2] / "gx-h/plugins/installed"
API = runpy.run_path(str(ROOT / "org.gx.scholarly-sources/1.0.0/methods/scholarly.py"))
PAGES = runpy.run_path(str(ROOT / "org.gx.scholarly-pages/1.0.0/methods/windows.py"))
REQUEST = {"schema": API["REQUEST"]}


def data(items, total, cursor=None, europe=False):
    if europe:
        return json.dumps({"resultList": {"result": items}, "hitCount": total, "nextCursorMark": cursor}).encode()
    return json.dumps({"message": {"items": items, "total-results": total, "next-cursor": cursor}}).encode()


class PageCoverage(unittest.TestCase):
    def first(self, responses):
        return PAGES["discover"](API, REQUEST, "rain gauge", "first", lambda _: next(responses))

    def test_same_paper_two_providers_and_key_original_only_on_next_page(self):
        doi = "10.0000/gauge"
        page, original = self.first(iter([data([{"doi": doi}], 1, europe=True),
                                          data([{"DOI": doi}], 2, "p+2==")]))
        self.assertEqual((2, 1), (page["returned_before_dedup"], page["unique_count"]))
        self.assertEqual([False, True], [s["has_more"] for s in page["sources"]])
        calls = []

        def second(url):
            calls.append(url)
            self.assertEqual(["p+2=="], parse_qs(urlsplit(url).query)["cursor"])
            return data([{"DOI": "10.0000/second", "title": ["Independent comparison"]}], 2, "p+2==")
        page2, snapshot = PAGES["discover"](API, original, "rain gauge", "next", second)
        self.assertEqual(len(calls), 1)
        self.assertEqual([False, False], [s["has_more"] for s in page2["sources"]])
        self.assertEqual(page2["sources"][0]["status"], "not_queried")
        self.assertEqual(page2["sources"][1]["seen_through"], 2)
        replay, saved = PAGES["discover"](API, snapshot, "rain gauge", "replay", self.fail)
        self.assertEqual(snapshot, saved)
        self.assertEqual(page2["candidates"], replay["candidates"])
        self.assertEqual(page2["sources"], replay["sources"])
        with self.assertRaisesRegex(ValueError, "usable continuation"):
            PAGES["discover"](API, snapshot, "rain gauge", "next", self.fail)
        changed = copy.deepcopy(snapshot)
        changed["calls"][1]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "digest"):
            PAGES["discover"](API, changed, "rain gauge", "replay", self.fail)
        with self.assertRaisesRegex(ValueError, "change the question"):
            PAGES["discover"](API, snapshot, "another site", "next", self.fail)

    def test_failure_and_true_zero_have_separate_coverage(self):
        def reply(url):
            if "ebi.ac.uk" in url:
                return b'{"hitCount":0,"resultList":{}}'
            raise API["UpstreamUnavailable"]("provider unavailable")
        page, _ = PAGES["discover"](API, REQUEST, "rain gauge", "first", reply)
        empty, unavailable = page["sources"]
        self.assertEqual((empty["status"], empty["coverage"], empty["provider_hits"]), ("empty", "exhausted", 0))
        self.assertEqual((unavailable["status"], unavailable["has_more"]), ("unavailable", None))

    def test_stalled_page_is_unknown_and_same_cursor_can_advance(self):
        repeated = data([{"DOI": "10.0000/a"}], 5, "cursor1")
        _, original = self.first(iter([data([], 0, europe=True), repeated]))
        stalled, _ = PAGES["discover"](API, original, "rain gauge", "next", lambda _: repeated)
        self.assertEqual(stalled["sources"][1]["status"], "unavailable")
        self.assertIsNone(stalled["sources"][1]["has_more"])
        distinct = data([{"DOI": "10.0000/b"}], 5, "cursor1")
        advanced, _ = PAGES["discover"](API, original, "rain gauge", "next", lambda _: distinct)
        self.assertEqual(advanced["sources"][1]["coverage"], "partial")
        self.assertTrue(advanced["sources"][1]["has_more"])

    def test_reject_invalid_archive_and_unknown_window(self):
        page, previous = self.first(iter([data([{"doi": "10.0000/p"}], 4, europe=True), b"<error>"]))
        self.assertEqual([s["coverage"] for s in page["sources"]], ["continuation_unknown", "unknown"])
        previous["calls"][0]["url"] = "https://unrelated.invalid"
        with self.assertRaisesRegex(ValueError, "archived catalogue URL"):
            PAGES["discover"](API, previous, "rain gauge", "next", self.fail)
        with self.assertRaisesRegex(ValueError, "both original provider"):
            PAGES["validate_search"]({**previous, "calls": []}, "rain gauge", "next")


if __name__ == "__main__":
    unittest.main()
