"""Versioned arXiv metadata, original transfer and read-only search replay."""
import copy
import hashlib
import runpy
from pathlib import Path
import unittest

DIR = Path(__file__).resolve().parents[2] / "gx-h/plugins/installed/org.gx.arxiv-sources/1.0.0"
api = runpy.run_path(str(DIR / "methods/arxiv.py"))
QUERY = 'ti:"Heat diffusion"'
REQUEST = {"schema": api["REQUEST"]}
FEED = '''<feed xmlns="http://www.w3.org/2005/Atom" xmlns:o="http://a9.com/-/spec/opensearch/1.1/">
<o:totalResults>1</o:totalResults><entry><id>http://arxiv.org/abs/cond-mat/0001001v2</id>
<title>Heat diffusion</title><link type="application/pdf" href="http://arxiv.org/pdf/cond-mat/0001001v2.pdf"/>
</entry></feed>'''.encode()


class ArxivSources(unittest.TestCase):
    def test_atom_legacy_accession_snapshot_and_original(self):
        c, snap = api["discover"](REQUEST, QUERY, lambda url: FEED)
        rec, = c["candidates"]
        self.assertIsNone(rec["doi"])
        self.assertIsNone(rec["published"])
        self.assertEqual(rec["arxiv_id"], "cond-mat/0001001v2")
        calls = []
        def pdf(url):
            calls.append(url)
            return b'%PDF-1.5\ntest header'
        content, meta = api["fetch"](c, rec["arxiv_id"], pdf)
        self.assertEqual(calls, ["https://arxiv.org/pdf/cond-mat/0001001v2"])
        self.assertEqual(meta["external_source"]["version"], "sha256:" + hashlib.sha256(content).hexdigest())
        def no_io(url):
            self.fail("Unexpected network call: " + url)
        for other in ("cond-mat/0001001v1", "cond-mat/0001001"):
            with self.assertRaises(ValueError):
                api["fetch"](c, other, no_io)
        c2, snap2 = api["discover"](snap, QUERY, no_io)
        self.assertEqual(c2, {**c, "source_mode": "snapshot_replay"})
        self.assertEqual(snap2, snap)
        altered = copy.deepcopy(snap)
        altered["calls"][0]["sha256"] = "a"*64
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            api["discover"](altered, QUERY, no_io)
        with self.assertRaisesRegex(ValueError, "original arXiv search query"):
            api["discover"](snap, "another query", no_io)

    def test_missing_pdf_metadata_empty_and_upstream_are_distinct(self):
        unversioned = FEED.replace(b'0001001v2', b'0001001')
        mismatch = FEED.replace(b'0001001v2.pdf', b'0001001v3.pdf')
        for partial in (unversioned, mismatch):
            c, _ = api["discover"](REQUEST, QUERY, lambda _: partial)
            self.assertEqual(c["sources"][0]["status"], "hits")
            self.assertIsNone(c["candidates"][0]["pdf_url"])
            self.assertEqual(c["candidates"][0]["availability"], "metadata_only")
        empty = FEED.split(b"<entry>")[0].replace(b">1<", b">0<")+b"</feed>"
        for body, expected in ((empty, "empty"), (b"not XML", "unavailable"),
                               (FEED.replace(b"/abs/cond-mat/0001001v2", b"/api/errors"), "unavailable")):
            c, _ = api["discover"](REQUEST, QUERY, lambda _: body)
            self.assertEqual(c["sources"][0]["status"], expected)
        def unavailable(url):
            raise api["UpstreamUnavailable"]("HTTP 503")
        c, snap = api["discover"](REQUEST, QUERY, unavailable)
        self.assertEqual(c["sources"][0]["status"], "unavailable")
        self.assertIsNone(c["sources"][0]["provider_hits"])
        self.assertEqual(api["discover"](snap, QUERY)[0]["sources"], c["sources"])

    def test_reject_html_in_place_of_selected_pdf(self):
        c, _ = api["discover"](REQUEST, QUERY, lambda _: FEED)
        with self.assertRaisesRegex(ValueError, "did not return a PDF"):
            api["fetch"](c, "cond-mat/0001001v2", lambda _: b'<html>rate limited</html>')
