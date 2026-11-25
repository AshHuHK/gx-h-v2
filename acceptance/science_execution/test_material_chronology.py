"""Compare actual CPU publication, delivered values and per-input chronology across a custody chain."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import unittest

from acceptance.v621 import test_claim_publication as claims
from acceptance.v622 import test_source_identity as custody
from acceptance.v62a.interleavings_fixture import branch, draft
from acceptance.v62c import materials
from gx_core.artifacts import ArtifactService
from gx_core.p0 import v621_metrics, v622_measurement, v623_chronology, v623_endpoints, v62_maturity, v6_jobs, v6_native
from gx_core.persistence import store

METHOD = "deposited-chain-geometry@2"
MASK = "common_backbone"


class MaterialChronology(unittest.TestCase):
    setUp = custody.MaterialSource.setUp
    command = custody.MaterialSource.command

    def ingest(self, cmd, name, path, acq, **kwargs):
        rc, receipt = self.command(cmd, name, path, acq, **kwargs)
        self.assertEqual(rc, 0, self.out[-1]["error"])
        return {k: receipt[k] for k in ("artifact_ref", "sha256")}

    def register_and_measure(self, name, pins, measured=None, *, concordance_on=()):
        s = self.state
        actor = s.next_actor("run")
        refs = [p["artifact_ref"] for p in pins]
        prediction = {"target": "analysis", "observable": "/metrics/coverage", "operator": "gt", "threshold": 0.6,
                      "measurement_schema": "gx.deposited_chain_geometry/2", "data_refs": refs,
                      "method": {"capability": METHOD, "output": "geometry.json", "parameters": {"mask": MASK},
                                 "metric_id": "coverage", "unit": "1", "input_role": "structure"}}
        stage = s.call(actor, "gx_stage_claim", dict(claims.BASE, statement=name, scope=materials.SCOPE, level="open",
                       evidence_refs=refs, prediction=prediction), "stage-" + name)["claim_ref"]
        stages = [stage]
        if concordance_on:
            check = {**prediction, "data_refs": [p["artifact_ref"] for p in concordance_on], "threshold": 0.2}
            stages.append(s.call(actor, "gx_stage_claim", dict(claims.BASE, statement="Both materials exceed one fifth.",
                scope=materials.SCOPE, level="open", evidence_refs=check["data_refs"], prediction=check),
                "stage-concordance")["claim_ref"])
        ops, watches = [], None
        if measured:
            ops = [dict(branch(name), budget_limits={"job_submissions": 1, "cpu_seconds": 120}),
                   s.node(actor, name + "_job", measured["artifact_ref"], branch_id="local:" + name,
                          capability=METHOD, parameters={"mask": MASK})]
            watches = [{"event": "job_settled", "subject_ref": "local:" + name + "_job"}]
        decision = s.call(actor, "gx_decide_revision", {"decision": draft(*ops, watches=watches,
                          evidence=[*refs, *stages])}, "adopt-" + name)
        self.plan = decision["decision_ref"]
        claim = next(c for c in decision["claim_revisions"] if c["statement"] == name)
        job = None
        if measured:
            job = s.settle(decision["aliases"]["local:" + name + "_job"])
        return claim, job

    def test_parent_children_equivalent_reimports_unknown_and_restart(self):
        s = self.state
        original = {**materials.parent_acquisition(), "sample": ["shared-archive-specimen"]}
        parent = self.ingest("observe", "archive", self.src, original, existing_ref=s.materials[0])
        parent_claim, parent_job = self.register_and_measure("reference", [parent], parent)
        oracle = materials.prepare(self.work / "custody")

        def withhold(code, label, **extra):
            slot = self.work / "custody" / code
            descriptor = {**original, "derived_from": [parent], "data_refs": [parent["artifact_ref"]],
                          "processing": ["coordinate-record withholding/" + oracle[code]["transform_sha256"]],
                          "transformation": json.loads((slot / "transform.json").read_bytes()), **extra}
            return self.ingest("assign-material", label, slot / "specimen.cif", descriptor)

        def annotated(name, source, pin, processing="append-envelope-comment/v1"):
            path = self.work / (name + ".cif")
            path.write_bytes(source.read_bytes() + ("# " + name + "\n").encode())
            descriptor = {**original, "processing": [processing], "derived_from": [pin],
                          "data_refs": [parent["artifact_ref"]]}
            return self.ingest("assign-material", name, path, descriptor), path

        p4 = withhold("P4", "first-child")
        k7 = withhold("K7", "second-child")
        wrapper, wrapper_path = annotated("parent-envelope", self.src, parent)
        double, _ = annotated("two-envelopes", wrapper_path, wrapper)
        p4_copy, _ = annotated("child-envelope", self.work / "custody/P4/specimen.cif", p4)
        unknown, _ = annotated("undocumented", self.src, parent, processing="uncalibrated-process/4")
        pins = [p4, k7, wrapper, double, p4_copy, unknown]
        grouped, p4_job = self.register_and_measure("discriminate", pins, p4, concordance_on=[p4, k7])
        reg = grouped["maturity"]["registration"]
        self.assertEqual(reg["material_identity"], "endpoint_verified")
        initial = grouped["publication"]["material_coverage"]
        self.assertEqual([row["qualification"] for row in initial],
                         ["unmeasured", "unmeasured", "retrospective", "retrospective", "unmeasured", "timing_unknown"])
        parent_time = initial[2]["endpoint_first_available_event_id"]
        self.assertEqual(initial[3]["endpoint_first_available_event_id"], parent_time)
        self.assertLess(parent_time, reg["event_id"])
        self.assertEqual(initial[-1]["material_identity_uncertainty"], ["unverified_endpoint_transformation"])

        duplicate = withhold("P4", "reimport-with-new-label", acquisition=["lab-import:lot-46"], sample=["lab-import:remapped-sample"])
        replay_claim, k7_job = self.register_and_measure("replicate", [duplicate, k7], k7)
        dupe_timing, k7_timing = replay_claim["publication"]["material_coverage"]
        self.assertEqual(dupe_timing["qualification"], "retrospective")
        self.assertEqual(k7_timing["qualification"], "unmeasured")

        actor = s.next_actor("run")
        latest = next(c for c in s.brief(actor)["claims"] if c["hypothesis_id"] == grouped["hypothesis_id"])
        self.assertEqual(v62_maturity.recompute(s.conn, s.run_id, grouped["claim_revision_id"])["material_coverage"], initial)

        def cold_recompute(_):
            with closing(store.open_store(s.state / "gx.db")) as conn:
                v6_jobs.advance(conn, s.run_id)
                return v62_maturity.recompute(conn, s.run_id, latest["claim_revision_id"])
        with ThreadPoolExecutor(2) as pool:
            first, second = pool.map(cold_recompute, range(2))
        self.assertEqual(first, second)
        coverage = first["material_coverage"]
        self.assertEqual([row["qualification"] for row in coverage],
                         ["prospective", "prospective", "retrospective", "retrospective", "prospective", "timing_unknown"])
        p4_time, k7_time = (row["endpoint_first_available_event_id"] for row in coverage[:2])
        self.assertLess(reg["event_id"], p4_time)
        self.assertLess(p4_time, k7_time)
        self.assertEqual(p4_time, dupe_timing["endpoint_first_available_event_id"])
        self.assertEqual(p4_time, coverage[4]["endpoint_first_available_event_id"])
        self.assertEqual(initial[4]["raw_material_received_event_id"], coverage[4]["raw_material_received_event_id"])
        self.assertIsNone(coverage[5]["endpoint_first_available_event_id"])
        self.assertEqual(coverage[0]["supporting_refs"], [p4_job["result_ref"]])
        self.assertEqual(coverage[1]["opposing_refs"], [k7_job["result_ref"]])
        self.assertEqual(first["direction"], "opposed")
        concordance = next(c["maturity"] for c in s.brief(actor)["claims"] if c["statement"] == "Both materials exceed one fifth.")
        self.assertEqual((concordance["direction"], concordance["stage"]), ("supported", "tested"))
        pair, = concordance["replication"]
        self.assertEqual(pair["relation"], "same_data_method_consistency")
        self.assertEqual(s.conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (s.run_id,)).fetchone()[0], 3)
        outputs = []
        for job, expected in ((parent_job, (164,164)), (p4_job, (oracle["P4"]["selected"],164)),
                              (k7_job, (oracle["K7"]["selected"],164))):
            metric, = [m for m in v621_metrics.from_output(s.conn, s.run_id, job["result_ref"]) if m["metric_id"] == "coverage"]
            self.assertEqual((metric["numerator"], metric["denominator"]), expected)
            outputs.append(metric)
        root = s.conn.execute("SELECT * FROM p0_roots WHERE lease_id=?", (actor.lease_id,)).fetchone()
        brief, ack = v6_native.acknowledged_brief(s.conn, root, actor.turn)
        observation = next(item for item in brief["observations"] if item.get("artifact_ref") == k7_job["result_ref"])
        row, = v623_endpoints.compact_rows([outputs[-1]])
        self.assertIn(row, observation.get("endpoint_rows", []), observation)
        dependencies = [r["dependencies"] for r in first["results"]]
        self.assertTrue(set(dependencies[0]["source"]).intersection(dependencies[1]["source"]))
        # This method/parameter combination was never executed; a timing receipt must match it too.
        alt = dict(reg["prediction"], method={**reg["prediction"]["method"], "parameters": {"mask": "common_ca"}})
        pin = v622_measurement.pin(s.conn, s.run_id, alt, applicability=materials.SCOPE)
        cursor = s.conn.execute("SELECT MAX(event_id) FROM events").fetchone()[0]
        other = v623_chronology.availability(s.conn, {**reg, "prediction": alt, "method_pin": pin}, cursor)
        self.assertTrue(all(row["endpoint_first_available_event_id"] is None for row in other.values()))
        uncertain_claim, uncertain_job = self.register_and_measure("unknowntransform", [unknown], unknown)
        latest_actor = s.next_actor("run")
        assessed = next(c for c in s.brief(latest_actor)["claims"] if c["hypothesis_id"] == uncertain_claim["hypothesis_id"])
        uncertain_coverage, = assessed["maturity"]["material_coverage"]
        self.assertEqual(uncertain_coverage["qualification"], "timing_unknown")
        self.assertFalse(uncertain_coverage["supporting_refs"])
        self.assertEqual(uncertain_coverage["uncertain_refs"], [uncertain_job["result_ref"]])
        self.assertLess(assessed["maturity"]["registration"]["event_id"], uncertain_coverage["endpoint_first_available_event_id"])
        # Byte receipt is all a just-imported Goal input has, before a new custody stamp.
        bare = ArtifactService(s.conn).publish_bytes(namespace=s.run_id + "/incoming", rel_path="raw-copy.cif",
            kind="material", data=(self.work / "custody/P4/specimen.cif").read_bytes(), media_type="chemical/x-mmcif",
            origin="INGEST", run_id=s.run_id, actor="operator")
        bare_reg = {**reg, "data": [{"artifact_ref": bare.artifact_ref, "sha256": p4["sha256"]}]}
        cursor = s.conn.execute("SELECT MAX(event_id) FROM events").fetchone()[0]
        bare_time = v623_chronology.availability(s.conn, bare_reg, cursor)[p4["sha256"]]
        self.assertEqual(bare_time["endpoint_first_available_event_id"], p4_time)
        self.assertLess(p4_time, bare_time["raw_material_received_event_id"])
        if where := os.getenv("GX_SCIENCE_CHRONOLOGY_RECEIPT"):
            Path(where).write_text(json.dumps(dict(run_id=s.run_id, registration=reg, normal_publication=grouped["publication"],
                recomputed_coverage=coverage, historical_reimport=dupe_timing, restarts_agree=True,
                job_values=outputs, decision_delivery={"task_id": actor.task_id, "turn": ack["turn_id"],
                "event_id": ack["event_id"], "brief_sha256": ack["text_sha256"], "selected_row_visible": outputs[-1]["metric_ref"]},
                replication_assessment=pair, shared_dependencies=dependencies, unknown_transform_after_own_job=uncertain_coverage,
                fresh_raw_reimport=bare_time, real_cpu_jobs=4, scripted_decisions=True, model_calls=0), indent=2)+"\n")
