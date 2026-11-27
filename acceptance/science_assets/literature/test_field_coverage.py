import unittest

from adapters.scientific.publication_coverage import summarize


def paper(doi=None, pmcid=None, records=()):
    return {"identifiers": {"doi": [doi] if doi else [], "pmcid": [pmcid] if pmcid else []},
            "records": [{"provider": p, "abstract_returned": a, "marked_open_access": o} for p,a,o in records]}


def page(key, query, n, entries, replay=False):
    return {"job_id":"job-"+key, "artifact_ref":key, "query":query, "candidates":entries,
            "source_mode":"snapshot_replay" if replay else "live_search",
            "sources":[{"provider":p,"page":n,"returned":sum(1 for e in entries for s in e["records"] if s["provider"]==p),
                        "provider_hits":48,"coverage":"partial"} for p in ("a","b")]}


class FieldCoverage(unittest.TestCase):
    def test_query_window_source_and_full_text_receipts_have_different_denominators(self):
        first = page("1", "q", 1, [paper("10/a", records=[("a",True,False)]),
                                    paper(pmcid="PMC1",records=[("b",False,True)]),
                                    paper("10/b", records=[("a",True,False)])])
        replay = dict(first, artifact_ref="r", job_id="replay", source_mode="snapshot_replay")
        later = page("2", "other q", 1, [paper("10/a","PMC1",[("a",True,True)]),
                                         paper("10/c",records=[("a",False,False)])])
        originals = [{"accession":"PMC1", "job_id":"fetch","artifact_ref":"xml",
                      "sha256":"verified", "input_candidates":["1"],"read_receipts":3,"distinct_locations":2,"segment_ids":["p1","p2"]}]
        got = summarize([first,replay,later], originals)
        self.assertEqual(got["unique_publications"]["count"], 3)
        self.assertEqual(got["unique_publications"]["verified_full_text"], 1)
        self.assertEqual(got["unique_publications"]["abstract_only"], 1)
        self.assertEqual(got["unique_publications"]["unknown_or_metadata_only"], 1)
        self.assertEqual(got["unique_publications"]["located_full_text"], 1)
        self.assertEqual(got["unique_publications"]["located_read_receipts"], 3)
        self.assertEqual(got["unique_publications"]["distinct_original_locations"], 2)
        self.assertEqual(got["windows"][0]["original_job_ids_from_this_output"], ["fetch"])
        self.assertEqual(got["windows"][2]["original_job_ids_from_this_output"], [])
        self.assertEqual(got["live_search_jobs"], 2)
        self.assertEqual(got["replay_jobs"], 1)
        self.assertEqual(got["windows"][0]["sources"][1]["scientific_access"]["verified_full_text"], 1)

    def test_an_open_flag_with_no_full_text_keeps_metadata_unknown(self):
        first = page("1","q",1,[paper("10/a", records=[("b",False,True)])])
        counts = summarize([first], [])["unique_publications"]
        self.assertEqual((counts["marked_open_access"],counts["verified_full_text"],
                          counts["unknown_or_metadata_only"],counts["located_full_text"]), (1,0,1,0))

    def test_source_accession_remains_an_alias_when_doi_arrives_on_another_window(self):
        original = {"identifiers": {}, "records":[{"provider":"a","identifiers":{"source":"AGR","id":"local-5"}}]}
        linked = {"identifiers":{"doi":["10/local"]}, "records":[{"provider":"a","identifiers":{"source":"AGR","id":"local-5","doi":"10/local"}}]}
        got = summarize([page("p","q",1,[original]), page("n","q",2,[linked])],[])
        self.assertEqual(got["unique_publications"]["count"],1)
