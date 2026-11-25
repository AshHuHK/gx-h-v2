"""The same scientific endpoint bindings govern publication and resolution of a registered gap."""
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from acceptance.v621 import test_gap_resolution as gap_fixture
from acceptance.v62a.interleavings_fixture import Scenario, branch, draft
from acceptance.v62c import materials
from gx_core.errors import SchemaInvalid
from gx_core.p0 import v621_assignment, v6_goal

METHOD = "deposited-chain-geometry@2"


class DerivedEndpointGaps(unittest.TestCase):
    def test_parent_alias_and_distinct_siblings(self):
        self.run_gap_study()

    def test_replay_preserves_v623_material_binding(self):
        self.run_gap_study(historical=True)

    def run_gap_study(self, *, historical=False):
        work = Path(self.enterContext(TemporaryDirectory()))
        policy = {"evidence_policy": "gx.evidence/6.2.3", "historical_replay": True} if historical else {}
        s = self.enterContext(Scenario(work / "goal", spec_updates={**policy,
            "research_effort": "deep", "research_modules": ["data_inventory@2"],
            "plugins": ["org.gx.protein@0.3.0"], "capabilities": [METHOD],
            "input_files": [{"path": str(materials.PARENT), "role": "structure"}]}))
        def decide(key, decision):
            return s.call(s.lead, "gx_decide_revision", {"decision": decision}, key)

        acq = materials.parent_acquisition()
        parent = s.materials[0]
        v6_goal.record_observation(s.conn, s.run_id, parent, source="public-deposition", acquisition=acq)
        plan = decide("register-custody", draft())
        parent_pin = {"artifact_ref": parent, "sha256": materials.PARENT_SHA}
        oracle = materials.prepare(work / "custody")
        targets = {}
        for label in ("P4", "K7", "envelope"):
            if label == "envelope":
                material = work / "wrapped.cif"
                material.write_bytes(materials.PARENT.read_bytes() + b"# acquired with this audit note\n")
                change = {"processing": ["append-envelope-comment/v1"]}
            else:
                slot = work / "custody" / label
                material = slot / "specimen.cif"
                change = {"processing": ["coordinate-record withholding/" + oracle[label]["transform_sha256"]],
                          "transformation": json.loads((slot / "transform.json").read_text())}
            sha = hashlib.sha256(material.read_bytes()).hexdigest()
            meta = work / (label + ".json")
            meta.write_text(json.dumps(dict(acq, **change, derived_from=[parent_pin], sha256=sha)))
            outcome = v621_assignment.assign(s.conn, s.run_id, after_decision=plan["decision_ref"],
                source="assigned-" + label, path=material, sha256=sha, role="structure",
                metadata_path=meta, actor="operator", media_type="chemical/x-mmcif")
            targets[label] = outcome["artifact_ref"]

        actor = s.next_actor("run")
        args = s.open_args("register-gaps", inputs=list(targets.values()), caps=[])
        args["module_version"] = "2"
        opened = s.call(actor, "gx_open_module", args, "register-gaps")
        worker = s.bind(opened["task_id"])
        decide("yield-to-gap-report", draft())
        criteria = []
        for label, ref in targets.items():
            s.call(worker, "gx_read_artifact", {"ref": ref, "operation_key": "inspect-" + label}, "read-" + label)
            criteria.append(dict(question="What is coverage in " + label + "?", data_ref=ref,
                applicability=acq["applicability"], nature="analysis", method=METHOD,
                method_parameters={"mask": "common_backbone"}, output_name="geometry.json",
                measurement_schema="gx.deposited_chain_geometry/2", observable="/metrics/coverage", op="finite"))
        report = s.report(worker, targets["P4"], artifact_refs=list(targets.values()),
                          gaps=[g["question"] for g in criteria], gap_criteria=criteria)
        lead = s.next_actor("run")
        gaps = {g["criterion"]["data_ref"]: g["gap_ref"] for g in s.brief(lead)["research_progress"]["open_gaps"]}
        ops = []
        for key, ref in (("parent", parent), ("child", targets["P4"])):
            ops += [dict(branch(key), budget_limits={"job_submissions": 1, "cpu_seconds": 120}),
                    s.node(lead, "measure-" + key, ref, branch_id="local:" + key, capability=METHOD,
                           parameters={"mask": "common_backbone"})]
        nodes = ["local:measure-parent", "local:measure-child"]
        issued = decide("measure", draft(*ops, watches=[{"event": "job_settled", "subject_ref": n} for n in nodes]))
        jobs = [s.settle(issued["aliases"][n]) for n in nodes]
        refs = [j["result_ref"] for j in jobs]
        lead = s.next_actor("run")
        if not set(refs) <= set(s.brief(lead)["evidence_refs"]):
            decide("receive-second-reading", draft())
            lead = s.next_actor("run")
        self.assertTrue(set(refs) <= set(s.brief(lead)["evidence_refs"]))
        with self.assertRaises(SchemaInvalid) as exc:
            decide("try-all", {**draft(evidence=refs), "resolved_gaps": list(gaps.values())})
        result = {r["gap_ref"]: r for r in exc.exception.details["gap_results"]}
        for label, expected in (("P4", [refs[1]]), ("K7", []), ("envelope", [] if historical else [refs[0]])):
            row = result[gaps[targets[label]]]
            self.assertEqual([r["artifact_ref"] for r in row["matches"]], expected, row)
            self.assertTrue(all(r["reason"] == "different_material" for r in row["rejections"]), row)
            if historical:
                self.assertNotIn("material_identity", row["criterion"])
                continue
            self.assertEqual(row["criterion"]["material_identity"], "endpoint_verified")
            for matched in row["matches"]:
                self.assertEqual(matched["material"]["artifact_ref"], targets[label])
                self.assertEqual(matched["first_production_event"], matched["endpoint_first_available_event_id"])
                pub, = s.rows("SELECT event_id FROM events WHERE kind='artifact.published' AND subject_id=?",
                              matched["artifact_ref"])
                success, = s.rows("SELECT event_id FROM events WHERE kind='job.transition' AND subject_id=? "
                                  "AND new_state='SUCCEEDED'", matched["job_id"])
                self.assertEqual(matched["endpoint_first_available_event_id"], max(pub["event_id"], success["event_id"]))
        self.assertFalse(s.rows("SELECT event_id FROM events WHERE kind='v62.gap.resolved'"))
        permitted = ("P4",) if historical else ("P4", "envelope")
        allowed = [gaps[targets[k]] for k in permitted]
        request = {**draft(evidence=refs), "resolved_gaps": allowed}
        receipt = decide("apply-two", request)
        self.assertEqual(decide("apply-two", request), receipt)
        events = s.rows("SELECT event_id,payload_json FROM events WHERE kind='v62.gap.resolved'")
        self.assertEqual(len(events), len(permitted))
        s.stack.close()
        with Scenario(work / "goal", reopen=True) as cold:
            self.assertEqual({r["gap_id"]: r["status"] for r in cold.rows("SELECT gap_id,status FROM gaps")},
                             {gid: "RESOLVED" if gid in allowed else "OPEN" for gid in gaps.values()})
        if not historical and (path := os.getenv("GX_ENDPOINT_GAP_RECEIPT")):
            Path(path).write_text(json.dumps({"run_id": s.run_id, "report": report, "materials": targets,
                "jobs": jobs, "batch_results": result, "receipt": receipt, "closure_events": events,
                "paid_model_calls": 0}, indent=2) + "\n")


class ReimportedEndpointGap(unittest.TestCase):
    def test_new_readout_and_copy_cannot_reset_known_endpoint_on_same_material(self):
        fixture = gap_fixture.GapResolution()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        s = fixture.s
        old = fixture.record({"schema": "public.instrument/1", "drift": 0.3}, source="first-read")
        fixture.decide("read-existing-endpoint")
        s.next_actor("run")
        gaps = fixture.bench_gaps()
        # A reimport with different serialized bytes and a repeated measurement both
        # have fresh publication events. The same material's drift was already known.
        renamed = fixture.record({"drift": 0.3, "schema": "public.instrument/1"}, source="reformatted")
        repeat = fixture.record({"schema": "public.instrument/1", "drift": 0.31}, source="second-read")
        self.assertEqual(len({old, renamed, repeat}), 3)
        s.next_actor("run")
        with self.assertRaises(SchemaInvalid) as exc:
            fixture.decide("reused-endpoint", evidence_refs=[renamed, repeat], resolved_gaps=[gaps["Drift?"]])
        row, = exc.exception.details["gap_results"]
        self.assertEqual({r["artifact_ref"]: r["reason"] for r in row["rejections"]},
                         {renamed: "already_available", repeat: "already_available"})
        self.assertFalse(s.rows("SELECT event_id FROM events WHERE kind='v62.gap.resolved'"))


    def test_unattributed_readout_cannot_borrow_timing_from_verified_measurement(self):
        from gx_core.artifacts import ArtifactService

        fixture = gap_fixture.GapResolution()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        s = fixture.s
        gaps = fixture.bench_gaps()
        low = fixture.record({"schema": "public.instrument/1", "viscosity": 1}, source="documented")
        raw = ArtifactService(s.conn).publish_bytes(namespace=s.run_id + "/readings", rel_path="other.json",
            data=json.dumps({"schema": "public.instrument/1", "viscosity": 3}), kind="instrument_measurement",
            run_id=s.run_id, actor="operator", origin="INGEST")
        v6_goal.record_observation(s.conn, s.run_id, raw.artifact_ref, source="undocumented",
            acquisition={"source": "station", "applicability": "lab bench", "data_refs": [fixture.source]})
        s.next_actor("run")
        with self.assertRaises(SchemaInvalid) as exc:
            fixture.decide("unknown-reading", evidence_refs=[low, raw.artifact_ref],
                           resolved_gaps=[gaps["Reference viscosity?"]])
        row, = exc.exception.details["gap_results"]
        self.assertEqual({r["artifact_ref"]: r["reason"] for r in row["rejections"]},
                         {low: "threshold_not_met", raw.artifact_ref: "unknown_first_production"})
        self.assertFalse(s.rows("SELECT event_id FROM events WHERE kind='v62.gap.resolved'"))
