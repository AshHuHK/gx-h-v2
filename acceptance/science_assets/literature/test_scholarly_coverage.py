"""Paper coverage is counted by stable publication IDs, including across repeated provider pages."""
import json
from pathlib import Path
import runpy
import unittest

ROOT = Path(__file__).resolve().parents[3] / "gx-h/plugins/installed"
API = runpy.run_path(str(ROOT / "org.gx.scholarly-sources/1.0.0/methods/scholarly.py"))
V1 = runpy.run_path(str(ROOT / "org.gx.scholarly-pages/1.0.0/methods/windows.py"))
NEW = ROOT / "org.gx.scholarly-pages/1.1.0/methods/windows.py"
QUERY = "rain gauge"


def epmc(rows, total, cursor=None):
    return json.dumps({"resultList": {"result":rows}, "hitCount":total, "nextCursorMark":cursor}).encode()


def cross(ids, total, cursor=None):
    return json.dumps({"message":{"items":[{"DOI":v} for v in ids], "total-results":total,
                                  "next-cursor":cursor}}).encode()


def first(pages, ep, cr):
    replies = iter([ep, cr])
    return pages["discover"](API, {"schema": API["REQUEST"]}, QUERY, "first", lambda _:next(replies))


class ScholarlyCoverage(unittest.TestCase):
    def setUp(self):
        self.pages = runpy.run_path(str(NEW)) if NEW.is_file() else V1

    def test_cross_page_duplicate_does_not_exhaust(self):
        _, one = first(self.pages, epmc([],0),cross(["10.1/a","10.1/b"],4,"x"))
        again, second = self.pages["discover"](API,one,QUERY,"next",
                                              lambda _: cross(["10.1/B","10.1/A"],4,"y"))
        self.assertNotEqual("exhausted",again["sources"][1]["coverage"])
        s = again["sources"][1]
        self.assertEqual((2,0,2,4), (s["returned"],s["new_unique"],s["cumulative_unique"],s["seen_through"]))
        self.assertTrue(s["has_more"])
        overlap, third = self.pages["discover"](API,second,QUERY,"next",
                                               lambda _:cross(["10.1/a","10.1/c"],4,"y"))
        self.assertEqual((1,3), (overlap["sources"][1]["new_unique"],overlap["merged"]["cumulative_unique"]))
        replay,saved = self.pages["discover"](API,third,QUERY,"replay", self.fail)
        self.assertEqual(replay["merged"],overlap["merged"])
        self.assertEqual(saved,third)
        last,_ = self.pages["discover"](API,third,QUERY,"next",lambda _:cross(["10.1/d"],4,"z"))
        self.assertEqual(("exhausted",4),(last["sources"][1]["coverage"],last["merged"]["cumulative_unique"]))

    def test_sources_and_article_access_are_separate(self):
        rows = [{"id":"123","source":"MED","doi":"10.1/a","pmcid":"PMC123","isOpenAccess":"Y",
                 "inEPMC":"Y","abstractText":"Recorded abstract"}, {"id":"456","source":"MED","doi":"10.1/b"}]
        page,_=first(self.pages,epmc(rows,12,"p"),cross(["10.1/A","10.1/c"],2))
        self.assertEqual([2,2],[s["new_unique"] for s in page["sources"]])
        self.assertEqual(3,page["merged"]["new_unique"])
        self.assertEqual("partial",page["sources"][0]["coverage"])
        access=page["merged"]["access"]
        self.assertEqual((1,0,0,1,2),(access["marked_open_access"],access["full_text_obtained"],
                          access["localized_reads"],access["abstract_only"],access["content_unknown"]))

    def test_missing_totals_and_stalled_cursor(self):
        page,one=first(self.pages,epmc([],0),cross(["10.1/b"],None,"c"))
        s=page["sources"][1]
        self.assertEqual(("hits",None,True), (s["status"],s["provider_hits"],s["has_more"]))
        again,_=self.pages["discover"](API,one,QUERY,"next",lambda _:cross(["10.1/B"],None,"c"))
        self.assertEqual(("continuation_unknown",None,1),
                         (again["sources"][1]["coverage"],again["sources"][1]["has_more"],again["merged"]["cumulative_unique"]))

    def test_empty_failure_and_v1_partial_history(self):
        def unavailable(_):
            raise API["UpstreamUnavailable"]("upstream down")
        first_calls=iter((epmc([],0),None))
        def answer(url):
            return next(first_calls) or unavailable(url)
        page,_=self.pages["discover"](API,{"schema":API["REQUEST"]},QUERY,"first",answer)
        self.assertEqual(["empty","unavailable"],[s["status"] for s in page["sources"]])
        self.assertEqual([0,None],[s["provider_hits"] for s in page["sources"]])
        _,p1=first(V1,epmc([],0),cross(["10.1/a"],4,"c1"))
        _,p2=V1["discover"](API,p1,QUERY,"next",lambda _:cross(["10.1/b"],4,"c2"))
        replay,_=self.pages["discover"](API,p2,QUERY,"replay",self.fail)
        self.assertFalse(replay["sources"][1]["history_complete"])
        self.assertEqual(1,replay["merged"]["cumulative_unique"])
        self.assertIsNone(replay["sources"][1]["new_unique"])
        three,_=self.pages["discover"](API,p2,QUERY,"next",lambda _:cross(["10.1/c"],4,"c3"))
        self.assertFalse(three["merged"]["history_complete"])
        self.assertEqual(2,three["merged"]["cumulative_unique"])

    def test_new_publication_to_one_source_can_already_be_in_merged_history(self):
        _, original = first(self.pages, epmc([{"doi": "10.1/shared"}], 1), cross(["10.1/other"], 2, "c"))
        page, snapshot = self.pages["discover"](API, original, QUERY, "next",
                                                 lambda _: cross(["10.1/SHARED"], 2, "c"))
        self.assertEqual([0, 1], [s["new_unique"] for s in page["sources"]])
        self.assertEqual((0, 2), (page["merged"]["new_unique"], page["merged"]["cumulative_unique"]))
        self.assertEqual("not_queried", page["sources"][0]["status"])
        replay, _ = self.pages["discover"](API, snapshot, QUERY, "replay", self.fail)
        self.assertEqual(replay["merged"], page["merged"])

    def test_accession_with_newly_reported_doi_is_one_page_candidate(self):
        records = [{"source":"AGR", "id":"AGR123", "title":"Instrument study"},
                   {"source":"AGR", "id":"AGR123", "doi":"10.1/gauge", "title":"Instrument study"}]
        page, _ = first(self.pages, epmc(records, 2), cross(["10.1/gauge"], 1))
        self.assertEqual(1, page["merged"]["cumulative_unique"])
        self.assertEqual(1, page["unique_count"])
        self.assertEqual([("europepmc", 1), ("europepmc", 2), ("crossref", 1)],
                         [(row["provider"], row["rank"]) for row in page["candidates"][0]["records"]])

    def test_authenticated_v1_job_can_supply_v2_continuation(self):
        from gx_core.errors import SchemaInvalid, ScopeDenied
        from gx_core.plugins import loader

        registry = loader.bootstrap(selected=[("org.gx.scholarly-pages", "1.1.0")])
        method = registry.get("org.gx.scholarly-pages", "1.1.0").contributions.goal_methods["scholarly-page-search@2"]
        _, archived = first(V1, epmc([], 0), cross(["10.1/a"], 2, "next"))

        class Input:
            def job_output(self, ref, capability, name):
                if ref == "foreign":
                    raise ScopeDenied("Different assignment")
                if (ref, capability, name) != ("output", "scholarly-page-search@1", "search-snapshot.json"):
                    raise SchemaInvalid("Named output mismatch")

            def read_json(self, ref):
                return archived

        parameters = {"query": QUERY, "action": "next"}
        method.validate(Input(), "scholarly-page-search@2", ["output"], parameters, producers={})
        with self.assertRaises(ScopeDenied):
            method.validate(Input(), "scholarly-page-search@2", ["foreign"], parameters, producers={})
        with self.assertRaises(ValueError):
            method.validate(Input(), "scholarly-page-search@2", ["unverified"], parameters, producers={})


if __name__ == "__main__":
    unittest.main()
