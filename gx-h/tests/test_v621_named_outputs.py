"""A real module publishes selected named Job products across cold recovery."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import ArtifactHashMismatch, ScopeDenied
from gx_core.native_release import native_state
from gx_core.p0 import manifest, v6_native
from gx_core.persistence import store
from tests import test_v62_outputs as series


def module_fixture(directory):
    """Extend the public two-output instrument locally with a third output and one optional non-output."""
    source = directory / "fixture"
    shutil.copytree(series.FIXTURE, source)
    plugin = source / "gx_plugin.json"
    spec = json.loads(plugin.read_bytes())
    spec["extensions"].append({"id": "series_inventory", "contract": "research_module", "version": "1"})
    plugin.write_text(json.dumps(spec))
    with (source / "plugin.py").open("a") as out:
        out.write("""
    ctx.research_modules.register(
        module_id='series_inventory', version='1', efforts=['deep'], focuses=['data'],
        purpose='Inspect instrument samples', instructions='Inspect the registered series.',
        output='module_report_bundle', cost_class='local_cpu', capabilities=['instrument-series@1'],
        tools=['gx_read_artifact','gx_decide_revision','gx_submit_module_report'], input_roles=['readings'])
""")
    card = source / "series.json"
    spec = json.loads(card.read_bytes())
    spec["manifest"]["outputs"].append({"name": "range.json", "kind": "file:series_range", "required": True,
                                        "nature": "analysis", "source_roles": ["readings"]})
    card.write_text(json.dumps(spec))
    with (source / "measure.py").open("a") as out:
        out.write("""
if __name__ == "__main__":
    samples = json.loads(Path(sys.argv[1]).read_bytes())['readings']
    (Path(sys.argv[2])/'range.json').write_text(json.dumps({'schema':'range/1','value':max(samples)-min(samples)}))
""")
    return source


class NamedModuleOutputs(unittest.TestCase):
    def test_commission_read_publish_and_resume_named_outputs(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        state = tmp / "study/state"
        source = module_fixture(tmp)
        with native_state(state), closing(store.open_store(state / "gx.db")) as conn:
            with patch.object(series, "FIXTURE", source):
                run, raw = series.start_series_goal(self, conn, tmp, effort="deep")

        def open_worker(world, key):
            args = world.open_args(key, caps=[series.METHOD], inputs=[raw],
                                   limits={"worker_tasks": 3, "cpu_seconds": 30,
                                           "api_calls": 3, "tokens": 3000, "job_submissions": 2})
            args.update(module_id="series_inventory", focus="data")
            return world.call(world.lead, "gx_open_module", args, key)

        def read(world, actor, ref):
            answer = world.call(actor, "gx_read_artifact", {"ref": ref}, "read-" + ref)
            return json.loads(answer["text"]) if "text" in answer else answer

        def ledger(world):
            balances = world.rows("SELECT budget_ref,spent_json,reserved_json FROM budgets WHERE run_id=? ORDER BY budget_ref",run)
            reservations = world.rows("SELECT r.* FROM budget_reservations r JOIN budgets b USING(budget_ref) "
                                      "WHERE b.run_id=? ORDER BY reservation_ref",run)
            return balances, reservations

        with Scenario(tmp / "study", reopen=True) as s:
            s.next_actor("run")
            lead = s.lead
            first = open_worker(s, "first")
            peer = open_worker(s, "peer")
            worker = s.next_actor(first["scope"])
            neighbor = s.next_actor(peer["scope"])
            s.call(lead, "gx_decide_revision", {"decision": draft()}, "seal")
            decision = s.submit_node(worker, "sample", raw, capability=series.METHOD)
            node = s.settle(decision["aliases"]["local:sample"])
            job = node["job_id"]
            record = manifest.read_manifest(s.conn, job)
            completed = {item["output_name"]: item for item in record["completion"]["outputs"]}
            self.assertEqual(set(completed), {"mean.json", "next.json", "range.json"})
            prediction, spread = (completed[n]["artifact_ref"] for n in ("next.json", "range.json"))
            self.assertEqual(node["result_ref"], completed["mean.json"]["artifact_ref"])
            with self.assertRaises(ScopeDenied):
                read(s, neighbor, prediction)
            # Record a real peer report to allow batched release after the owning module publishes.
            read(s, neighbor, raw)
            s.report(neighbor, raw)

        with Scenario(tmp / "study", reopen=True) as s:
            s.advance()
            collector = s.next_actor(first["scope"])
            packet = v6_native.brief(s.roots._binding(collector.thread))
            self.assertEqual(set(packet["evidence_refs"]) - {raw}, {r["artifact_ref"] for r in completed.values()})
            [proof] = packet["verified_jobs"]
            cards = {card["output_name"]: card for card in proof["outputs"]}
            self.assertEqual(set(cards), set(completed))
            self.assertEqual(proof["job_id"], job)
            for name, card in cards.items():
                self.assertEqual((card["artifact_ref"], card["sha256"], card["nature"]),
                                 tuple(completed[name][k] for k in ("artifact_ref", "sha256", "nature")))
            self.assertEqual((read(s, collector, prediction)["value"], read(s, collector, spread)["value"]), (7, 4))
            # A second authorized commission on the same inputs can reuse the sealed multi-output Job.
            rerun = s.submit_node(collector, "repeat", raw, capability=series.METHOD)
            reused = s.settle(rerun["aliases"]["local:repeat"])
            self.assertEqual((reused["status"], reused["job_id"], reused["result_ref"]),
                             ("REUSED", job, node["result_ref"]))
            collector = s.next_actor(first["scope"])
            packet = v6_native.brief(s.roots._binding(collector.thread))
            self.assertEqual({p["job_id"] for p in packet["verified_jobs"]}, {job})
            self.assertEqual({p["node_id"] for p in packet["verified_jobs"]}, {node["node_id"], reused["node_id"]})
            self.assertTrue(all(p["outputs"] == proof["outputs"] for p in packet["verified_jobs"]))
            # Exchanging the stored bytes cannot turn the prediction into the measured range.
            assets = ArtifactService(s.conn)
            meta = assets.describe(spread)
            path = assets.resolve_path(meta["namespace"], meta["rel_path"])
            original = path.read_bytes()
            try:
                path.write_bytes(assets.read_bytes(prediction))
                with self.assertRaises(ArtifactHashMismatch):
                    s.report(collector, spread, key="bad-bytes")
            finally:
                path.write_bytes(original)
            submit = {"operation_key":"final", "status":"COMPLETE", "gaps":[], "next_steps":[],
                      "artifact_refs":[spread,prediction],
                      "findings":[{"text":"The range and prediction are distinct.", "source_refs":[spread,prediction]}]}
            receipt = s.call(collector, "gx_submit_module_report", submit, "first-submission")
            self.assertNotIn(node["result_ref"], receipt["source_refs"])
            lead = s.next_actor("run")
            delivered = v6_native.brief(s.roots._binding(lead.thread))
            self.assertTrue({receipt["report_ref"], spread, prediction} <= set(delivered["evidence_refs"]))
            self.assertNotIn(node["result_ref"], delivered["evidence_refs"])
            with self.assertRaises(ScopeDenied):
                read(s, lead, node["result_ref"])
            self.assertEqual(read(s, lead, spread)["value"], 4)
            bundle = read(s, lead, receipt["report_ref"])
            proofs = {}
            for source in bundle["sources"]:
                attestations = [r for r in source["receipts"] if r.get("job_id") == job]
                self.assertEqual({r["node_id"] for r in attestations}, {node["node_id"], reused["node_id"]})
                attestation = attestations[0]
                proofs[attestation["output_name"]] = attestation["output_sha256"]
                self.assertEqual(attestation["manifest_sha256"], record["manifest_hash"])
                self.assertEqual(attestation["output_sha256"], source["sha256"])
            self.assertEqual(proofs, {n: completed[n]["sha256"] for n in ("range.json", "next.json")})
            s.advance()
            before = ledger(s)
        with Scenario(tmp / "study", reopen=True) as s:
            self.assertEqual(receipt, s.call(collector, "gx_submit_module_report", submit, "redelivery"))
            s.advance()
            self.assertEqual(before, ledger(s))
            self.assertEqual([job], [row["job_id"] for row in s.rows("SELECT job_id FROM jobs WHERE run_id=?",run)])
            self.assertEqual(2, len(s.rows("SELECT report_ref FROM v62_module_reports WHERE run_id=?",run)))
            if destination := os.environ.get("GX_NAMED_OUTPUT_RECEIPT"):
                Path(destination).write_text(json.dumps({
                    "case": "2C", "run_id": run, "job_id": job, "node_id": node["node_id"],
                    "module_instance_id": first["module_instance_id"], "original_lease": worker.lease_id,
                    "collector_lease": collector.lease_id, "lead_lease": lead.lease_id, "outputs": cards,
                    "publication": receipt, "admission_sha256": record["admission_hash"],
                    "execution_manifest_sha256": record["manifest_hash"], "published_output_digests": proofs,
                    "fresh_jobs": 1, "authorized_reuses": 1, "reused_node_id": reused["node_id"],
                    "additional_jobs_after_recovery": 0, "additional_spend_after_recovery": 0,
                    "cross_scope_read": "refused", "altered_output": "refused", "unpublished_primary": "refused",
                    "status": "PASS", "paid_model_calls": 0, "provider_tokens": None,
                }, indent=2) + "\n")


if __name__ == "__main__":
    unittest.main()
