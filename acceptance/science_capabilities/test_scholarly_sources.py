"""Regression tests for public discovery and byte-preserving, accession-bound paper handoff."""
import copy
import hashlib
import json
from pathlib import Path
import runpy
import unittest

METHODS = Path(__file__).resolve().parents[2] / "gx-h/plugins/installed/org.gx.scholarly-sources/1.0.0/methods"
api = runpy.run_path(str(METHODS / "scholarly.py"))
REQUEST = {"schema": api["REQUEST"]}
QUERY = "yeast trehalase"


def call(r1=(), r2=()):
    pmc = {"hitCount": len(r1), "resultList": {"result": list(r1)}}
    crossref = {"message": {"total-results": len(r2), "items": list(r2)}}
    return lambda url: json.dumps(pmc if url.startswith(api["EPMC"]) else crossref).encode()


PAPER = dict(doi="10.1234/ABC", pmcid="PMC120", id="125", source="MED", title="Trehalase",
             inEPMC="Y", isOpenAccess="Y")
JATS = b'<article><front><article-meta><article-id pub-id-type="pmcid">PMC120</article-id></article-meta></front><body><p>Body text.</p></body></article>'


class Scholarly(unittest.TestCase):
    def test_cross_provider_dedup_and_network_free_replay(self):
        c, snapshot = api["discover"](REQUEST, QUERY, call([PAPER], [
            {"DOI": "10.1234/abc", "title": ["Different catalogue spelling"], "type": "journal-article"}]))
        self.assertEqual(c["returned_before_dedup"], 2)
        self.assertEqual(c["unique_count"], 1)
        self.assertEqual(c["candidates"][0]["fetchable_pmcids"], ["PMC120"])
        self.assertCountEqual([r["provider"] for r in c["candidates"][0]["records"]], api["SOURCES"])
        replay, frozen = api["discover"](snapshot, QUERY, lambda url: self.fail("offline replay made network call"))
        self.assertEqual(frozen, snapshot)
        self.assertEqual(replay, {**c, "source_mode": "snapshot_replay"})
        bad_snapshot = copy.deepcopy(snapshot)
        bad_snapshot["calls"][0]["sha256"] = "a" * 64
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            api["discover"](bad_snapshot, QUERY)
        with self.assertRaisesRegex(ValueError, "original query"):
            api["discover"](snapshot, QUERY + " altered")

    def test_partial_provider_failure_empty_and_malformed_results(self):
        def first_offline(url):
            if url.startswith(api["EPMC"]):
                raise api["UpstreamUnavailable"]("service unavailable")
            return call()(url)
        c, snap = api["discover"](REQUEST, QUERY, first_offline)
        self.assertEqual(c["unique_count"], 0)
        self.assertEqual([s["status"] for s in c["sources"]], ["unavailable", "empty"])
        self.assertEqual(api["discover"](snap, QUERY)[0]["sources"], c["sources"])
        c, _ = api["discover"](REQUEST, QUERY, lambda url: b'{}')
        self.assertEqual([s["status"] for s in c["sources"]], ["unavailable", "unavailable"])
        calls = []
        def no_proxy(url):
            calls.append(url)
            raise api["ProxyUnavailable"]("local proxy unavailable")
        with self.assertRaises(api["ProxyUnavailable"]):
            api["discover"](REQUEST, QUERY, no_proxy)
        self.assertEqual(len(calls), 1)

    def test_fetch_original_revision_replacement_and_invalid_article(self):
        candidates, _ = api["discover"](REQUEST, QUERY, call([PAPER]))
        for raw in (JATS, JATS.replace(b'Body text.', b'Corrected text.')):
            body, meta = api["fetch"](candidates, "pmc120", lambda url: raw)
            self.assertEqual(body, raw)
            self.assertEqual(meta["external_source"]["version"], "sha256:" + hashlib.sha256(raw).hexdigest())
        for body, error in ((JATS.replace(b'PMC120', b'PMC121'), "accession differs"),
                            (JATS.replace(b'body', b'abstract'), "no JATS article body"),
                            (b'<html>paywall</html>', "no JATS article body")):
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                api["fetch"](candidates, "PMC120", lambda url: body)

    def test_fetch_only_open_selected_original(self):
        c, _ = api["discover"](REQUEST, QUERY, call([{**PAPER, "isOpenAccess": "N"}]))
        for pmcid in ("PMC120", "PMC121", "../../foo"):
            with self.assertRaises(ValueError):
                api["fetch"](c, pmcid, lambda url: self.fail("unavailable or unselected paper should not download"))

    def test_public_method_sources_match_hashes(self):
        for name in ("social", "fetch", "search"):
            manifest = json.loads((METHODS / (name + ".json")).read_bytes())["manifest"]
            source = METHODS / Path(manifest["source"]["path"]).name
            self.assertEqual(manifest["source"]["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
