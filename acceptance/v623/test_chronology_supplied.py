"""Independent equal bytes, unknown original identity, same-store history and concurrent supply."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from tempfile import TemporaryDirectory
from acceptance.v62a.interleavings_fixture import GOAL, Scenario

from acceptance.v621 import test_claim_publication as original
from acceptance.v623.test_chronology import observe
from gx_core.p0.roots import Roots
from gx_core.p0 import v62_maturity, v623_chronology
from gx_core.persistence import store
from contextlib import closing

READING = b'{"schema":"public.thermometer.celsius/1","measurement":{"value":7}}'


def case_receipt(name, **facts):
    if dest := os.environ.get("GX_V623_CHRONOLOGY_DIR"):
        folder = Path(dest)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (name + ".json")).write_text(json.dumps(dict(external_paid_calls=0, **facts), indent=2) + "\n")


class SuppliedAvailability(unittest.TestCase):
    def setUp(self):
        self.f = original.ClaimPublication()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        f.folder = Path(self.enterContext(TemporaryDirectory()))
        plan = f.folder / "plan.json"
        plan.write_text('{"site":"north","visit":"morning"}')
        # Both Runs share this workspace; reserve only the slots used by this offline experiment.
        limits = dict(job_submissions=2, worker_tasks=12, cpu_seconds=60, api_calls=12,
                      tokens=30000, gpu_jobs=0, gpu_minutes=0, wall_hours=1)
        cfg = dict(research_effort="deep", research_modules=[], plugins=[], capabilities=[],
                   input_files=[{"path": str(plan), "role": "material"}], run_budget=limits)
        f.gx = self.enterContext(Scenario(f.folder / "goal", spec_updates=cfg))
        f.rule = dict(data_refs=f.gx.materials, target="experimental_observation", measurement_schema=original.METRIC,
                      observable="/measurement/value", operator="gt", threshold=10)
        f.initial = f.adopt(f.gx.lead, "registration", f.gx.materials, "open", prediction=f.rule)

    def test_two_independent_aliquots_equal_bytes_unknown_identity_and_parallel_resupply(self):
        f = self.f
        meta = lambda k: dict(data="aliquot-file-" + k, sample="independent-aliquot-" + k, acquisition="visit-" + k,
                              sampling="independent-visit-" + k)
        samples = [observe(f, "sample-" + k, original.SCOPE, b'{"sample_type":"morning aliquot"}', data=[],
                           acquisition=meta(k)) for k in "AB"]
        prior = observe(f, "known-A", original.SCOPE, READING, data=samples[:1], acquisition=meta("A"))
        actor = f.gx.next_actor("run")
        rule = dict(f.rule, data_refs=samples, threshold=5)
        initial = f.adopt(actor, "two-equally-encoded-samples", [prior], "open", prediction=rule)
        first, second = initial["publication"]["material_coverage"]
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertNotEqual(first["material_key"], second["material_key"])
        self.assertEqual([first["qualification"], second["qualification"]], ["retrospective", "unmeasured"])
        fresh = observe(f, "new-B", original.SCOPE, READING, data=samples[1:], acquisition=meta("B"))
        def send(_):
            return f.cli("observe", "--source", "new-B", "--input", f.folder / "new-B.json", "--acquisition",
                         f.folder / "new-B-acquisition.json")["event_id"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            e1, e2 = pool.map(send, range(2))
        self.assertEqual(e1, e2)
        actor = f.gx.next_actor("run")
        before = next(c for c in f.cli("status")["claims"] if c["hypothesis_id"] == initial["hypothesis_id"])
        revised = f.adopt(actor, "report-two-samples", [fresh], "limited_support", previous_claim_ref=before["claim_revision_id"])
        p = revised["publication"]
        self.assertEqual((p["retrospective_data_refs"], p["prospective_data_refs"], p["unmeasured_data_refs"]),
                         (samples[:1], samples[1:], []))
        self.assertEqual(p["missing_prospective_data_refs"], samples[:1])
        a, b = p["material_coverage"]
        self.assertLess(a["endpoint_first_available_event_id"], initial["maturity"]["registration"]["event_id"])
        self.assertGreater(b["endpoint_first_available_event_id"], initial["maturity"]["registration"]["event_id"])
        with closing(store.open_store(f.gx.state / "gx.db")) as cold:
            self.assertEqual(p["material_coverage"], v62_maturity.recompute(cold, f.gx.run_id,
                             revised["claim_revision_id"])["material_coverage"])
        observe(f, "round-two", original.SCOPE, b'{"task":"new criteria"}', data=[])
        actor = f.gx.next_actor("run")
        both_known = f.adopt(actor, "two-known", [fresh], "open", prediction=dict(rule, threshold=9))["publication"]
        self.assertEqual(both_known["retrospective_data_refs"], samples)
        self.assertEqual(both_known["missing_prospective_data_refs"], samples)
        self.assertEqual(both_known["unmeasured_data_refs"], [])
        material = observe(f, "unsigned-lot", original.SCOPE, b'{"survey":"unidentified"}', data=[], acquisition=meta("U"))
        actor = f.gx.next_actor("run")
        reg = f.adopt(actor, "unknown-registration", [material], "open", prediction=dict(rule, data_refs=[material]))
        blind = observe(f, "unattributed", original.SCOPE, READING, data=[material],
                        acquisition={"acquisition": None, "sample": None, "source": "manual register"})
        actor = f.gx.next_actor("run")
        curr = next(c for c in f.cli("status")["claims"] if c["hypothesis_id"] == reg["hypothesis_id"])
        final = f.adopt(actor, "read-unknown", [blind], "limited_support", previous_claim_ref=curr["claim_revision_id"])
        self.assertEqual(final["publication"]["timing_unknown_data_refs"], [material])
        self.assertEqual(final["publication"]["unmeasured_data_refs"], [])
        self.assertEqual(final["level"], "insufficient")
        case_receipt("supplied", run_id=f.gx.run_id, repeat_event_ids=[e1,e2], historical_and_prospective=p,
                     unknown=final["publication"], initial=initial["publication"], both_retrospective=both_known)

    def test_raw_intrinsic_same_boundary_and_distinct_typed_endpoint(self):
        f = self.f
        typed = {"instrument":"thermistor", "selection":"north", "unit":"C"}
        raw = observe(f, "intrinsic", original.SCOPE, json.dumps(dict(schema=original.METRIC,
                      measurement={"value":7}, endpoint=typed)).encode(), data=[])
        actor = f.gx.next_actor("run")
        rule = dict(f.rule, threshold=5, data_refs=[raw], external_endpoint=typed)
        initial = f.adopt(actor, "preknown-raw", [raw], "open", prediction=rule)
        cov, = initial["publication"]["material_coverage"]
        self.assertEqual(cov["qualification"], "retrospective")
        self.assertEqual(cov["raw_material_received_event_id"], cov["endpoint_first_available_event_id"])
        pin = initial["maturity"]["registration"]
        self.assertEqual(v623_chronology.availability(f.gx.conn, pin, cov["raw_material_received_event_id"])
                         [cov["sha256"]]["endpoint_first_available_event_id"], cov["raw_material_received_event_id"])
        observe(f, "schedule", original.SCOPE, b'{"action":"plan second meter"}', data=[])
        actor = f.gx.next_actor("run")
        other_rule = dict(rule, external_endpoint=dict(typed, unit="K"))
        other = f.adopt(actor, "kelvin-registration", [raw], "open", prediction=other_rule)
        self.assertEqual(other["publication"]["unmeasured_data_refs"], [raw])
        self.assertNotEqual(other["maturity"]["registration"]["measurement_spec_sha256"], pin["measurement_spec_sha256"])
        unused = observe(f, "wrong-applicability", "Other station", json.dumps(dict(schema=original.METRIC,
               measurement={"value":7}, endpoint=other_rule["external_endpoint"])).encode(), data=[raw])
        actor = f.gx.next_actor("run")
        measured = next(c for c in f.cli("status")["claims"] if c["hypothesis_id"] == other["hypothesis_id"])
        self.assertEqual(measured["publication"]["measured_unqualified_data_refs"], [raw])
        self.assertEqual(measured["publication"]["unmeasured_data_refs"], [])
        self.assertFalse(measured["publication"]["supporting_refs"])
        self.assertTrue(any(g.get("artifact_ref") == unused for g in measured["maturity"]["gaps"]))
        case_receipt("intrinsic", original=initial["publication"], kelvin=other["publication"],
                     measured_but_inapplicable=measured["publication"])

    def test_cross_run_reimport_discloses_chronology_without_foreign_reading(self):
        f = self.f
        old = observe(f, "private-measurement", original.SCOPE, READING)
        _, prior, _ = f.delivered_claim()
        old_time = prior["maturity"]["material_coverage"][0]["endpoint_first_available_event_id"]
        s = f.gx
        # Admit a second actual Goal in the same durable store, with a different authorized actor.
        args = [sys.executable, "-B", str(original.ROOT / "bin/gx-p0"), "start", "--state", str(s.state),
                "--run-key", "shared-archive-second", "--goal", GOAL,
                "--goal-config", str(s.directory / "grant.json")]
        started = json.loads(subprocess.check_output(args, env=dict(os.environ, GX_CODEX_OFFLINE="1")))
        if started.get("status") == "queued_for_active_driver":
            from gx_core.p0 import coordinator, workspace
            coordinator.drain_starts(s.conn, s.state)
            new_run = workspace.find_run(s.conn, run_key="shared-archive-second")
            self.assertIsNotNone(new_run, s.conn.execute("SELECT last_error FROM outbox WHERE outbox_id=?",
                                 (started["outbox_id"],)).fetchone()[0])
            run = new_run["run_id"]
        else:
            run = started["run_id"]
        self.assertNotEqual(s.run_id, run)
        s.run_id = run
        info = s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()
        s.materials = [i["artifact_ref"] for i in json.loads(info[0])["files"]]
        previous_transport = s.roots.client
        s.roots = Roots(s.conn, run_id=run, cwd=s.state, token_budget=10)
        s.roots.client = previous_transport
        actor = s.next_actor("run")
        scoped = original.SCOPE + "。"
        second = f.adopt(actor, "different-authorized-run", s.materials[:1], "open", scope=scoped,
                         prediction=dict(f.rule, data_refs=s.materials[:1], threshold=5))
        cov, = second["publication"]["material_coverage"]
        self.assertEqual((cov["qualification"], cov["endpoint_first_available_event_id"]), ("retrospective", old_time))
        self.assertNotIn(old, json.dumps(second))
        reimport = observe(f, "re-import", scoped, b'{"measurement":{"value":7.0},"schema":"public.thermometer.celsius/1"}')
        actor, current, _ = f.delivered_claim()
        published = f.adopt(actor, "conclusion-second-run", [reimport], "limited_support", scope=scoped,
                            previous_claim_ref=current["claim_revision_id"])
        self.assertEqual(published["level"], "insufficient")
        self.assertNotIn(old, json.dumps(published))
        self.assertEqual(cov["endpoint_first_available_event_id"], published["publication"]["material_coverage"][0]["endpoint_first_available_event_id"])
        case_receipt("cross-run", receipt=published, private_ref_excluded=True)
