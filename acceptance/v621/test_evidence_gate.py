"""Exercise fixed graph evidence conditions through normal Goal adoption and CPU dispatch."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import time
from threading import Barrier
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest, v6_goal, v6_jobs, v62_evidence, v62_rounds
from gx_core.persistence import store
from tests.test_v6_decisions import branch


def node(name, ref, depends=(), gate=None, *, target="local:study"):
    result = {"op": "add_node", "node": "local:" + name, "branch": target, "inputs": [ref],
              "capability": "deposited-chain-geometry@1", "parameters": {"mask": "common_ca"},
              "depends_on": list(depends), "question": "Measure coordinate coverage."}
    if gate is not None:
        result["evidence_gate"] = gate
    return result


def condition(ref, *, producer="local:inventory", equals=164, review=False):
    return {"producer_node": producer, "producer_version": 1, "source_ref": ref,
            "pointer": "/mapping/common_backbone_residues", "equals": equals,
            "require_lead_review": review}


def open_route(s, *extras):
    b = branch("study", jobs=8)
    b["budget_limits"]["cpu_seconds"] = 300
    producer = {**node("inventory", s.materials[0]), "capability": "tl1a-coordinate-quality@1", "parameters": {}}
    dec = draft(b, producer, *extras, watches=[{"event": "job_settled", "subject_ref": "local:inventory"}])
    return s.call(s.lead, "gx_decide_revision", {"decision": dec}, "open")["aliases"]


def account(s):
    return s.rows("SELECT job_id,status,reservation_ref,measured_json FROM jobs WHERE run_id=? ORDER BY rowid", s.run_id)


def job_reservations(s):
    return s.rows("""SELECT reservation_ref,subject_id,status,amounts_json FROM budget_reservations
                     WHERE subject_kind='job' ORDER BY rowid""")


def event(s, node_id, kind="v6.node.settled"):
    return [{"event_id": row["event_id"], **json.loads(row["payload_json"])} for row in s.rows(
        "SELECT event_id,payload_json FROM events WHERE run_id=? AND subject_id=? AND kind=? ORDER BY event_id",
        s.run_id, node_id, kind)]


def wait(s, node_id):
    row = s.conn.execute("SELECT wait_json FROM v6_node_waits WHERE node_id=?", (node_id,)).fetchone()
    return json.loads(row[0]) if row else None


def race_dispatch(s, target):
    """Two driver connections concurrently check the same pending gate."""
    barrier = Barrier(2)
    verify = v62_evidence.require_ready

    def interleave(conn, item, spec):
        if item["node_id"] == target and "branch_status" in item.keys():
            barrier.wait(timeout=8)
        return verify(conn, item, spec)

    def operate(_):
        with closing(store.open_store(s.state / "gx.db")) as conn:
            return v6_jobs.advance(conn, s.run_id)

    with patch.object(v62_evidence, "require_ready", interleave), ThreadPoolExecutor(2) as pool:
        list(pool.map(operate, range(2)))


class EvidenceGates(unittest.TestCase):
    def test_fixed_negative_settles_once_blocks_dependents_and_last_route_closes(self):
        study = Path(self.enterContext(tempfile.TemporaryDirectory())) / "gate"
        with Scenario(study) as s:
            refs = open_route(s)
            inv = refs["local:inventory"]
            producer = s.settle(inv)
            proof = manifest.read_manifest(s.conn, producer["job_id"])
            lead = s.next_actor("run")
            false = node("unpromising", s.materials[0], [inv], condition(s.materials[0], producer=inv, equals=165),
                         target=refs["local:study"])
            downstream = node("downstream", s.materials[0], ["local:unpromising"], target=refs["local:study"])
            dec = draft(false, downstream, watches=[{"event":"job_settled","subject_ref":"local:unpromising"}])
            aliases = s.call(lead, "gx_decide_revision", {"decision": dec}, "conditional")["aliases"]
            target, dependent = aliases["local:unpromising"], aliases["local:downstream"]
            initial = account(s)
            charged = job_reservations(s)
            self.assertEqual(len(charged), len(initial))
            self.assertEqual(len(initial), 1)
            race_dispatch(s, target)
        with Scenario(study, reopen=True) as s:
            s.advance()
            s.advance()
            states = s.rows("SELECT status,job_id FROM v6_nodes WHERE node_id IN (?,?)", target, dependent)
            self.assertEqual(states, [{"status":"BLOCKED","job_id":None}] * 2)
            rejection, = event(s, target)
            self.assertEqual("evidence_condition_false", rejection["condition"]["reason"])
            self.assertEqual(164, rejection["condition"]["observed"])
            self.assertEqual(producer["result_ref"], rejection["condition"]["result_ref"])
            self.assertEqual(proof["manifest_hash"], rejection["condition"]["completion_sha256"])
            downstream, = event(s, dependent)
            self.assertEqual(downstream["dependency"]["settlement_event"], rejection["event_id"])
            facts, failures = v62_rounds._event_facts(s.conn, s.run_id, rejection["event_id"]-1, downstream["event_id"])
            self.assertEqual(["candidate_excluded"], [item["kind"] for item in facts.values()])
            self.assertEqual([], failures)
            self.assertEqual(account(s), initial)
            self.assertEqual(job_reservations(s), charged)
            close = {"op":"close_run","reason":"The remaining route was eliminated by its fixed observation.",
                     "claim_refs":[],"unresolved":["No claim about unobserved coordinates."]}
            lead = s.next_actor("run")
            self.assertTrue(any(n["node_id"] == target and n["status"] == "BLOCKED" for n in s.brief(lead)["nodes"]))
            s.call(lead, "gx_decide_revision", {"decision": draft(close)}, "close")
            s.advance()
            self.assertEqual("CLOSED", s.conn.execute("SELECT status FROM v6_goals WHERE run_id=?", (s.run_id,)).fetchone()[0])
            out = os.environ.get("GX_GATE_TRACE")
            if out:
                Path(out).write_text(json.dumps({"case":"fixed_negative","run_id":s.run_id,"paid_model_calls":0,
                    "accepted_job":initial[0],"unchanged_job_reservations":charged,"admission_sha256":proof["admission_hash"],"manifest_sha256":proof["manifest_hash"],
                    "result_ref":producer["result_ref"],"gate_event":rejection,"dependent_event":event(s, dependent)[0],
                    "close_event":event(s, s.run_id, "v6.run.closed")[0],"additional_jobs":0}, indent=2)+"\n")

    def test_private_producer_cannot_leak_its_scalar_to_another_branch(self):
        study = Path(self.enterContext(tempfile.TemporaryDirectory())) / "private"
        with Scenario(study) as s:
            src = s.materials[0]
            opened = s.open("worker", inputs=[src])
            # Release the opening Lead before the worker creates its private node.
            # The observation below can then wake a fresh Lead at that graph revision.
            s.call(s.lead, "gx_decide_revision", {"decision": draft()}, "await-private-producer")
            worker = s.bind(opened["task_id"])
            produced = s.submit_node(worker, "measurement", src)
            inv = produced["aliases"]["local:measurement"]
            v6_goal.record_observation(s.conn, s.run_id, s.materials[-1], source="unrelated")
            founder = s.next_actor("run")
            b = branch("study", jobs=1)
            b["budget_limits"]["cpu_seconds"] = 120
            op = node("conditional", src, [inv], condition(src, producer=inv, equals=165))
            receipt = s.call(founder, "gx_decide_revision", {"decision":draft(b, op)}, "preplan")
            target = receipt["aliases"]["local:conditional"]
            producer = s.settle(inv)
            result = producer["result_ref"]
            self.assertEqual([], event(s, target))
            reason = wait(s, target)
            self.assertEqual("awaiting publication of the scoped producer result", reason["details"]["condition"])
            self.assertNotIn(result, json.dumps(reason))
            self.assertEqual(len(account(s)), 1)
            ready = s.next_actor(opened["scope"])
            s.call(ready, "gx_read_artifact", {"ref": result, "operation_key":"read"}, "read")
            s.report(ready, result)
            s.advance()
            settled, = event(s, target)
            self.assertEqual(result, settled["condition"]["result_ref"])
            self.assertEqual("evidence_condition_false", settled["condition"]["reason"])
            self.assertEqual(len(account(s)), 1)

    def test_original_policy_reopens_with_its_original_wait_behavior(self):
        study = Path(self.enterContext(tempfile.TemporaryDirectory())) / "legacy"
        with Scenario(study, spec_updates={"evidence_policy":"gx.evidence/6.2"}) as s:
            false = node("false", s.materials[0], ["local:inventory"], condition(s.materials[0], equals=165))
            aliases = open_route(s, false)
            s.settle(aliases["local:inventory"])
        with Scenario(study, reopen=True) as s:
            s.advance()
            self.assertEqual("PENDING", s.conn.execute("SELECT status FROM v6_nodes WHERE node_id=?",
                                                       (aliases["local:false"],)).fetchone()[0])
            self.assertEqual(event(s, aliases["local:false"]), [])
            self.assertIn("observed value", wait(s, aliases["local:false"])["message"])
            self.assertEqual(len(account(s)), 1)

    def test_unreadable_producer_before_first_wake_yields_actionable_brief(self):
        study = Path(self.enterContext(tempfile.TemporaryDirectory())) / "damaged"
        with Scenario(study) as s:
            true = node("candidate", s.materials[0], ["local:inventory"], condition(s.materials[0], review=True))
            aliases = open_route(s, true)
            until = time.monotonic() + 20
            while True:
                v6_jobs.advance(s.conn, s.run_id)
                row = s.conn.execute("SELECT status,result_ref FROM v6_nodes WHERE node_id=?",
                                     (aliases["local:inventory"],)).fetchone()
                if row["status"] == "SUCCEEDED":
                    break
                self.assertLess(time.monotonic(), until)
                time.sleep(0.04)
            service = ArtifactService(s.conn)
            ref = row["result_ref"]
            meta = service.describe(ref)
            path = service.resolve_path(meta["namespace"], meta["rel_path"])
            path.write_bytes(b'{"fabricated": 812}')
            v6_jobs.advance(s.conn, s.run_id)
            lead = s.next_actor("run")
            observations = [o for o in s.brief(lead)["observations"] if o["artifact_ref"] == ref]
            self.assertEqual(len(observations), 1)
            self.assertIn("unavailable", observations[0])
            self.assertNotIn("observation", observations[0])
            self.assertNotIn("fabricated", json.dumps(s.brief(lead)))
            self.assertEqual(wait(s, aliases["local:candidate"])["details"]["reason"], "evidence_invalid")
            self.assertEqual(len(account(s)), 1)

    def test_wait_damage_repair_cited_review_and_once_only_dispatch(self):
        study = Path(self.enterContext(tempfile.TemporaryDirectory())) / "gate"
        with Scenario(study) as s:
            src = s.materials[0]
            true = node("candidate", src, ["local:inventory"], condition(src, review=True))
            wrong = node("wrong-version", src, ["local:inventory"], {**condition(src), "producer_version":2})
            source = node("wrong-source", src, ["local:inventory"], condition(s.materials[1]))
            pointer = node("missing-field", src, ["local:inventory"], {**condition(src), "pointer":"/absent"})
            aliases = open_route(s, true, wrong, source, pointer)
            target = aliases["local:candidate"]
            v6_jobs.advance(s.conn, s.run_id)
            self.assertEqual("evidence_pending", event(s, target, "v6.node.waiting")[-1]["details"]["reason"])
            producer = s.settle(aliases["local:inventory"])
            self.assertEqual("awaiting an evidence-citing Lead decision", wait(s, target)["details"]["condition"])
            result = producer["result_ref"]
            service = ArtifactService(s.conn)
            meta = service.describe(result)
            path = service.resolve_path(meta["namespace"], meta["rel_path"])
            original = path.read_bytes()
            for alias in ("wrong-version","wrong-source","missing-field"):
                self.assertEqual("evidence_invalid", wait(s, aliases["local:" + alias])["details"]["reason"])
            path.write_bytes(b"{}")
            v6_jobs.advance(s.conn, s.run_id)
            self.assertEqual("evidence_invalid", wait(s, target)["details"]["reason"])
            self.assertIn("recovery", wait(s, target)["details"])
            self.assertEqual(len(account(s)), 1)
        with Scenario(study, reopen=True) as s:
            path.write_bytes(original)
            s.advance()
            self.assertEqual("evidence_pending", wait(s, target)["details"]["reason"])
            review = draft(evidence=[result], watches=[{"event":"job_settled","subject_ref":target}])
            review["approve_nodes"] = [target]
            approved = s.call(s.next_actor("run"), "gx_decide_revision", {"decision":review}, "accept")
            race_dispatch(s, target)
            follow = s.settle(target)
            self.assertEqual(follow["status"],"SUCCEEDED")
            s.advance()
            s.advance()
            self.assertEqual(len(account(s)),2)
            self.assertEqual(len(event(s, target)),1)
            out = os.environ.get("GX_GATE_TRACE")
            if out:
                proof = manifest.read_manifest(s.conn, follow["job_id"])
                Path(out).with_suffix(".review.json").write_text(json.dumps({
                    "case": "wait_invalid_repair_and_true", "run_id": s.run_id,
                    "producer_result": result, "lead_approval": approved["decision_ref"],
                    "review_event": event(s, target, "v62.node.evidence_approved")[0],
                    "candidate_waits": event(s, target, "v6.node.waiting"),
                    "still_unavailable": {key:wait(s,aliases["local:"+key]) for key in
                                          ("wrong-version", "wrong-source", "missing-field")},
                    "jobs": account(s), "followup_manifest_sha256": proof["manifest_hash"],
                    "paid_model_calls": 0, "provider_tokens": None}, indent=2)+"\n")
