"""Backwards-compatible review limitations, unambiguous aliases and exact source navigation in geometry v2."""
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.p0 import cli
from gx_core.p0.driver import context
from gx_core.persistence import store
from tests.test_tl1a_geometry import OfflineIngress, product_for, submit_peer
from workflows.tl1a_geometry_v2 import normalize_review


class ReviewContract(unittest.TestCase):
    def test_reject_ambiguous_repeated_missing_and_empty_content(self):
        plan = {"hypotheses": [{"id": "a", "hypothesis_id": "h1"}, {"id": "b", "hypothesis_id": "h2"}]}
        for limitations in ([], [" "], ["scope", {}], "  ", 4, None):
            with self.subTest(limitations=limitations), self.assertRaises(SchemaInvalid):
                normalize_review(plan, {"limitations": limitations, "findings": {"a": 1, "b": 2}})
        for findings in ({"x":1,"b":2}, {"a":1,"h1":2}, {"a":1}, {"a":1,"h1":1,"b":2}, []):
            with self.subTest(findings=findings), self.assertRaises(SchemaInvalid):
                normalize_review(plan, {"limitations": "single crystal", "findings": findings})
        for keys in (["a", "b"], ["h1","h2"], ["a","h2"]):
            submitted = {"limitations": ["single crystal", "one model"], "findings": dict(zip(keys, [1,2]))}
            self.assertEqual(normalize_review(plan, submitted)["findings"], {"a":1, "b":2})
            self.assertEqual(submitted["limitations"], ["single crystal", "one model"])

    def test_canonical_ids_settle_under_original_submission_and_v1_pin_survives(self):
        old_pin = "bd01ffd53b2f9148c868e7e699fda32149acd0734b60841534fa404e5c47d455"
        self.assertEqual(service.default_registry().get("tl1a_geometry", "1").digest(), old_pin)
        with tempfile.TemporaryDirectory() as tmp, native_state(Path(tmp)) as state, closing(store.open_store(state.db_path)) as conn:
            with patch.object(cli, "Driver", OfflineIngress), patch("gx_core.p0.roots.tempfile.gettempdir", return_value=tmp):
                rid = cli.start(conn, Path(tmp), {**cli.geometry_spec(), "max_jobs": 3})["run_id"]
                assets = ArtifactService(conn)
                def author(roots, task, inputs, tid):
                    self.assertIn("product_schema", inputs)
                    guide = inputs["source_guide"]
                    raw = assets.read_bytes(guide["structure_ref"])
                    self.assertEqual(len(raw), guide["bytes"])
                    self.assertGreaterEqual(len(guide["locators"]), 7)
                    for loc in guide["locators"]:
                        self.assertTrue(raw[loc["offset"]:].startswith(loc["excerpt"].encode("ascii")))
                    out = product_for(roots, task, inputs, tid)
                    if inputs["output_kind"] == "geometry_assessment":
                        plan = assets.read_json(inputs["proposal_ref"])
                        out["findings"] = {h["hypothesis_id"]:out["findings"][h["id"]] for h in plan["hypotheses"]}
                        out["limitations"] = ["one crystal", "missing residues"]
                        props = inputs["product_schema"]["properties"]
                        self.assertIn("oneOf", props["limitations"])
                        self.assertEqual(set(props["findings"]["properties"]),
                              {h[key] for h in plan["hypotheses"] for key in ("id", "hypothesis_id")})
                    return out
                with patch("tests.test_tl1a_geometry.product_for", side_effect=author):
                    for _ in range(10):
                        for (jid,) in conn.execute("SELECT job_id FROM jobs WHERE run_id=?", (rid,)):
                            JobService(conn).wait(jid, timeout_seconds=15)
                        reactor.lazy(context(conn), rid)
                        reactor.drive_science(context(conn), rid)
                        if task := conn.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY'", (rid,)).fetchone():
                            submit_peer(conn, tmp, rid, task)
                        elif cli.snapshot(conn)["status"] == "COMPLETED":
                            break
                result = cli.snapshot(conn)
                self.assertEqual(result["status"], "COMPLETED")
                self.assertEqual(conn.execute("SELECT workflow_version FROM runs WHERE run_id=?", (rid,)).fetchone()[0], "2")
                r = result["rounds"][0]
                review = assets.read_json(r["assessment_ref"])
                self.assertEqual(review["limitations"], ["one crystal", "missing residues"])
                claims = assets.read_json(r["proposal_ref"])["hypotheses"]
                rows = conn.execute("SELECT s.path_id,s.hypothesis_id,s.direction,e.source_locator_json "
                        "FROM science_settlements s JOIN evidence e USING(evidence_id) WHERE s.run_id=?", (rid,)).fetchall()
                self.assertEqual({(v["path_id"], v["hypothesis_id"]) for v in rows},
                                 {(h["id"], h["hypothesis_id"]) for h in claims})
                for row in rows:
                    locator = json.loads(row["source_locator_json"])
                    self.assertEqual(locator["review_ref"], r["assessment_ref"])
                    projected = assets.read_json(locator["normalized_findings_ref"])
                    self.assertEqual(projected["original_review_ref"], r["assessment_ref"])
                    self.assertEqual(projected["findings"][row["path_id"]], review["findings"][row["hypothesis_id"]])
                    self.assertEqual(row["direction"] == "supports", review["findings"][row["hypothesis_id"]]["supported"])
