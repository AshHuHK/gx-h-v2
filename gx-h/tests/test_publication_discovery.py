"""The search entry consumes a query; continuation and acquisition consume verified outputs."""
import tempfile
from pathlib import Path
import unittest

from gx_core import config
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import capability_views, catalog_index
from gx_core.plugins import loader


class PublicationDiscovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        installed = loader.bootstrap(selected=[("org.gx.publication-discovery", "1.0.0")])
        assert not installed.failures, installed.failures
        package = next(p for p in installed.plugins.values() if p.pin.id == "org.gx.publication-discovery")
        cls.methods = package.contributions.goal_methods

    def test_question_alone_is_discoverable_and_binds_an_empty_input_manifest(self):
        methods = {r["capability_version"]: r for r in catalog_index.installed_methods()
                   if r.get("provider") == "org.gx.publication-discovery@1.0.0"}
        scopes = capability_views.freeze_scope({"discovery_policy":capability_views.DISCOVERY_POLICY})
        self.assertLessEqual(set(methods), {p["method"] for p in scopes["capability_options"]})
        self.assertEqual(methods["literature-query@1"]["inputs"], [])
        self.assertEqual(methods["literature-page@1"]["inputs"], ["snapshot"])
        self.assertEqual(methods["literature-original@1"]["inputs"], ["candidates"])
        first = self.methods["literature-query@1"]
        args = {"query":"rain gauges"}
        self.assertEqual(first.bind(None, "literature-query@1", [], args), ({}, args))
        for refs, query in ((["request.json"], "rain gauges"), ([], ""), ([], "x" * 601)):
            with self.assertRaises(ValueError):
                first.validate(None, "literature-query@1", refs, {"query":query}, producers={})

    def test_missing_and_unrelated_inputs_cannot_continue_or_fetch(self):
        class Input:
            def job_output(self, ref, method, name):
                if ref == "foreign":
                    raise ScopeDenied("Outside this assignment")
                raise SchemaInvalid("Unverified source")
        for method, args in (("literature-page@1", {"action":"next"}), ("literature-original@1", {"pmcid":"PMC42"})):
            bound = self.methods[method]
            for supplied, graph in (([], {}), (["forged"], {}), (["producer"], {"producer":"wrong@1"})):
                with self.assertRaises(ValueError):
                    bound.validate(Input(), method, supplied, args, producers=graph)
            with self.assertRaises(ScopeDenied):
                bound.validate(Input(), method, ["foreign"], args, producers={})
            bound.validate(Input(), method, ["previous"], args, producers={"previous":"scholarly-page-search@1"})

    def test_worker_checks_reader_bytes_before_loading_and_replay_stays_offline(self):
        import base64
        from hashlib import sha256
        import json
        import runpy

        installed = config.HARNESS_ROOT / "plugins/installed"
        pkg = installed / "org.gx.publication-discovery/1.0.0/methods/publications.py"
        wrapper = runpy.run_path(str(pkg))
        base = wrapper["load_reader"](installed / "org.gx.scholarly-sources/1.0.0/methods/scholarly.py", wrapper["SOURCES_SHA"])
        pages = wrapper["load_reader"](installed / "org.gx.scholarly-pages/1.0.0/methods/windows.py", wrapper["PAGES_SHA"])
        with tempfile.TemporaryDirectory() as tmp:
            changed = Path(tmp) / "reader.py"
            changed.write_text("raise AssertionError('changed reader executed')")
            with self.assertRaisesRegex(ValueError, "differs from its pinned version"):
                wrapper["load_reader"](changed, wrapper["PAGES_SHA"])
        bodies = ({"hitCount":0,"resultList":{}}, {"message":{"items":[],"total-results":0}})
        query = "rain gauges"
        calls = []
        for (provider,url), body in zip(base["search_urls"](query).items(), bodies, strict=True):
            raw = json.dumps(body).encode()
            calls.append(dict(provider=provider, url=pages["page_url"](url, "*"), cursor="*", page=1,
                              offset=0, retrieved_at="2026-11-27T00:00:00Z", sha256=sha256(raw).hexdigest(),
                              raw_b64=base64.b64encode(raw).decode("ascii")))
        snapshot = dict(schema=pages["SNAPSHOT"], query=query, calls=calls)
        def offline(url):
            self.fail("Replay attempted to contact " + url)
        candidates, replay = pages["discover"](base, snapshot, query, "replay", download=offline)
        self.assertEqual(replay, snapshot)
        self.assertEqual(candidates["unique_count"], 0)
        self.assertEqual([s["coverage"] for s in candidates["sources"]], ["exhausted", "exhausted"])
