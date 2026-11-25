"""Test doubles only. The tests do not start GX, a model, or a scientific solver."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import verify as v


def write_ref(root, path, obj):
    data = obj if isinstance(obj, bytes) else v.stable(obj)
    (root / path).write_bytes(data)
    return {"file": path, "sha256": v.sha(data)}


class EvidenceTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "capture"
        self.root.mkdir()
        self.anchor = Path(tmp.name) / "anchor.json"
        self.data = self.create("controls")

    def example(self, name):
        return json.loads((v.HERE / "inputs" / (name + ".json")).read_text(encoding="utf-8"))

    def create(self, mode):
        runs = []
        goal = self.example("goal")
        initial = self.example("initial")
        for pid in (("p17", "p42", "p63") if mode == "controls" else ("structure",)):
            run = "run-" + pid
            def put(name, obj):
                return write_ref(self.root, pid + "-" + name, obj)
            intent = {"mode": "resolve" if pid == "p63" else "amend",
                      "rationale": "test fixture", "reconsider_when": [{"event": "evidence_added",
                      "subject_ref": "geometry"}], "ops": []}
            op = {"op": "add_node", "branch": "geometry", "node": "targeted-audit",
                  "capability": "demo@1", "parameters": {"pair": "A/B" if pid == "p17" else "A/C"},
                  "inputs": [], "depends_on": [], "question": "Synthetic arithmetic check"}
            if intent["mode"] == "amend":
                intent["ops"] = [op]
            oldjob = {"job_id": "existing", "operation_key": "key-0", "request_sha256": "req-0",
                      "status": "running", "branch": "citation-index"}
            if mode == "controls":
                packet = self.example(pid)
            else:
                coords = put("coordinates", b"unit-test coordinates")
                result = put("upstream", {"measurement": {"n": 164}})
                oldjob.update(status="completed", branch="geometry", input_sha256=coords["sha256"],
                              result=result)
                packet = {"packet_id": pid, "kind": "observed_structure", "coordinates": coords,
                          "base_digest": v.digest(initial), "observations": [{"id": "o1",
                          "source_job_id": "existing", "result_sha256": result["sha256"],
                          "pointer": "/measurement", "value": {"n": 164}}]}
            alias = {"gx-" + o["id"]: o["id"] for o in packet["observations"]}
            intent["evidence_refs"] = list(alias)
            view = {"evidence_map": alias, "base_digest": v.digest(initial), "goal_digest": v.digest(goal),
                    "packet_digest": v.digest(packet)}
            view["visible_observations"] = [{"evidence_ref": "gx-" + o["id"],
                "value": o.get("measurement", o.get("value"))} for o in packet["observations"]]
            before = {"capture_order": 1, "run_id": run, "head": "v1", "logical_head": "r1",
                      "base_digest": v.digest(initial), "boot_id": "one", "science":
                      {"level": "open", "chain_pairs": []}, "jobs": [oldjob], "submission_ledger": [
                      {"ledger_id": "l0", "job_id": "existing", "operation_key": "key-0", "units": 1}],
                      "spent_job_submissions": 1, "job_submission_limit": 4}
            turn = {"capture_order": 2, "run_id": run, "thread_id": "session-" + pid,
                    "turn_id": "turn-" + pid, "decision_id": "d-" + pid,
                    "tool_arguments": put("native-tool", {"payload": v.stable(intent).decode()}),
                    "presented_evidence": put("presented", view), "packet_digest": v.digest(packet),
                    "base_head": "v1"}
            turn["tool_arguments"]["pointer"] = "/payload"
            commit = {"capture_order": 3, "run_id": run, "base_head": "v1", "decision_id": turn["decision_id"],
                      "turn_id": turn["turn_id"], "result_head": "v2" if intent["ops"] else "v1",
                      "intent_digest": v.digest(intent), "dispatches": []}
            after = dict(copy.deepcopy(before), capture_order=4, decision_id=turn["decision_id"],
                         head=commit["result_head"])
            if intent["ops"]:
                req = put("request", op)
                dispatch = {"operation_key": "key-1", "branch": "geometry", "node": op["node"],
                            "node_digest": v.digest(op), "chain_pairs": [op["parameters"]["pair"]],
                            "pair_pointers": ["/parameters/pair"],
                            "request_sha256": req["sha256"]}
                commit["dispatches"].append(dispatch)
                after["jobs"].append({"job_id": "followup", "operation_key": "key-1", "status": "completed",
                                     "request": req, "request_sha256": req["sha256"], "branch": "geometry",
                                     "decision_id": turn["decision_id"], "result": put("result", {"n": 1})})
                after["submission_ledger"].append({"ledger_id": "l1", "job_id": "followup",
                                                  "operation_key": "key-1", "units": 1})
                after["spent_job_submissions"] = 2
            reopened = dict(copy.deepcopy(after), capture_order=5, boot_id="two")
            runs.append(dict(run_id=run, packet=put("packet", packet), initial=put("initial", initial),
                             goal=put("goal", goal), profile=put("profile", {"pinned": "model+budget"}),
                             before=put("before", before), turn=put("turn", turn), commit=put("commit", commit),
                             settled=put("settled", after), reopened=put("reopened", reopened)))
        return {"version": 6, "mode": mode, "runs": runs}

    def rewrite(self, row, field, mutate):
        ref = self.data["runs"][row][field]
        obj = v.document(self.root, ref)
        mutate(obj)
        self.data["runs"][row][field] = write_ref(self.root, ref["file"], obj)

    def execute(self):
        ref = write_ref(self.root, "bundle.json", self.data)
        self.anchor.write_bytes(v.stable({"bundle_sha256": ref["sha256"]}))
        return v.verify(self.root, self.anchor)

    def test_full_suite_same_verb_different_targets(self):
        self.assertEqual(self.execute(), ("controls", 3))

    def test_model_visible_measurements_match_packet(self):
        def alter(turn):
            view = v.document(self.root, turn["presented_evidence"])
            view["visible_observations"][0]["value"] = {"summary": "trust me"}
            turn["presented_evidence"] = write_ref(self.root, "invalid-visible.json", view)
        self.rewrite(0, "turn", alter)
        with self.assertRaisesRegex(ValueError, "model-visible measurement differs"):
            self.execute()

    def test_stale_or_foreign_turn(self):
        self.rewrite(1, "commit", lambda obj: obj.update(turn_id="turn-p17"))
        with self.assertRaisesRegex(ValueError, "lineage"):
            self.execute()

    def test_source_hash_tamper(self):
        self.execute()
        (self.root / "p42-native-tool").write_bytes(b"forged")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            v.verify(self.root, self.anchor)

    def test_anchor_external_and_pinned(self):
        self.execute()
        local = self.root / "anchor"
        local.write_bytes(self.anchor.read_bytes())
        with self.assertRaisesRegex(ValueError, "external anchor"):
            v.verify(self.root, local)
        (self.root / "bundle.json").write_bytes(b"{}")
        with self.assertRaisesRegex(ValueError, "detached anchor"):
            v.verify(self.root, self.anchor)

    def test_rejects_escape_and_duplicate_json_keys(self):
        ref = self.data["runs"][0]["packet"]
        ref["file"] = "../capture/" + ref["file"]
        with self.assertRaisesRegex(ValueError, "outside"):
            self.execute()
        with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
            v.parse(b'{"id":0,"id":1}')

    def test_missing_requeued_or_duplicate_charge(self):
        self.rewrite(0, "settled", lambda obj: obj["submission_ledger"].append(
            dict(obj["submission_ledger"][1], ledger_id="extra")))
        with self.assertRaisesRegex(ValueError, "duplicate job_id"):
            self.execute()

    def test_job_replaced_or_never_run(self):
        self.rewrite(0, "settled", lambda obj: obj["jobs"][1].update(status="queued"))
        with self.assertRaisesRegex(ValueError, "did not settle"):
            self.execute()

    def test_declared_operation_not_in_model_call(self):
        self.rewrite(0, "commit", lambda obj: obj["dispatches"][0].update(node="hard-coded-plan"))
        with self.assertRaisesRegex(ValueError, "native model call"):
            self.execute()

    def test_neutral_side_effect(self):
        def fabricate(obj):
            obj["science"]["level"] = "contested"
            obj["science_receipt"] = write_ref(self.root, "wrong-neutral-impact", {
                "decision_id": obj["decision_id"], "claim_revision_id": "x", "science": obj["science"]})
        self.rewrite(2, "settled", fabricate)
        with self.assertRaisesRegex(ValueError, "neutral metadata"):
            self.execute()

    def test_unchanged_real_branch_identity_but_cancelled(self):
        self.rewrite(1, "settled", lambda obj: obj["jobs"][0].update(status="cancelled"))
        with self.assertRaisesRegex(ValueError, "cancelled or rewound"):
            self.execute()

    def test_cold_boot_receipt_is_distinct_and_idempotent(self):
        self.rewrite(1, "reopened", lambda obj: obj.update(boot_id="one"))
        with self.assertRaisesRegex(ValueError, "same boot"):
            self.execute()

    def test_hard_cap(self):
        self.rewrite(1, "settled", lambda obj: obj.update(job_submission_limit=1))
        with self.assertRaisesRegex(ValueError, "budget mismatch"):
            self.execute()

    def test_target_projection_must_match_request(self):
        self.rewrite(1, "commit", lambda obj: obj["dispatches"][0].update(chain_pairs=["A/B"]))
        with self.assertRaisesRegex(ValueError, "scored target differs"):
            self.execute()

    def test_identical_realized_effects_do_not_pass_opposed_controls(self):
        # No new operations and no claim change in either opposed world.
        for i in (0, 1):
            row = self.data["runs"][i]
            turn = v.document(self.root, row["turn"])
            intent = v.parse(v.document(self.root, turn["tool_arguments"]))
            intent.update(mode="resolve", ops=[])
            turn["tool_arguments"] = write_ref(self.root, "alt-intent-" + str(i), intent)
            row["turn"] = write_ref(self.root, "alt-turn-" + str(i), turn)
            self.rewrite(i, "commit", lambda obj: obj.update(result_head="v1", dispatches=[],
                                                             intent_digest=v.digest(intent)))
            before = v.document(self.root, row["before"])
            for field in ("settled", "reopened"):
                self.rewrite(i, field, lambda obj: obj.update(head="v1", jobs=before["jobs"],
                    submission_ledger=before["submission_ledger"], spent_job_submissions=1))
        with self.assertRaisesRegex(ValueError, "equivalent measured effects"):
            self.execute()

    def test_inflight_sibling_can_progress_over_restart(self):
        self.rewrite(2, "reopened", lambda obj: obj["jobs"][0].update(status="completed"))
        self.execute()

    def test_session_leakage_and_changed_conditions(self):
        self.rewrite(1, "turn", lambda obj: obj.update(thread_id="session-p17"))
        with self.assertRaisesRegex(ValueError, "reused"):
            self.execute()
        self.rewrite(1, "profile", lambda obj: obj.update(pinned="different"))
        with self.assertRaisesRegex(ValueError, "different initial"):
            self.execute()

    def test_partial_packet_suite(self):
        self.data["runs"].pop()
        with self.assertRaisesRegex(ValueError, "three matched"):
            self.execute()

    def stop_witness(self, close=False):
        self.data = self.create("observed")
        row = self.data["runs"][0]
        turn = v.document(self.root, row["turn"])
        intent = v.parse(v.document(self.root, turn["tool_arguments"]))
        subject = row["run_id"] if close else "geometry"
        kind = "run" if close else "branch"
        target = kind + ":" + subject
        op = ({"op": "close_run", "reason": "limited evidence", "claim_refs": [], "unresolved": ["scope"]}
              if close else {"op": "stop_branch", "branch": subject, "reason": "limited evidence"})
        intent["ops"] = [op]
        turn["tool_arguments"] = write_ref(self.root, "stop-call", intent)
        row["turn"] = write_ref(self.root, "new-turn", turn)
        self.rewrite(0, "before", lambda obj: obj.update(scope_states={target: "active"}))
        self.rewrite(0, "commit", lambda obj: obj.update(intent_digest=v.digest(intent), dispatches=[]))
        before = v.document(self.root, row["before"])
        event = {"run_id": row["run_id"], "event_id": 2, "subject_kind": kind, "subject_id": subject,
                 "prev_state": "ACTIVE", "new_state": "COMPLETED" if close else "STOPPED",
                 "payload_json": v.stable({"decision_id": turn["decision_id"], "op_digest": v.digest(op)}).decode()}
        ref = write_ref(self.root, "stop-event", event)
        for field in ("settled", "reopened"):
            self.rewrite(0, field, lambda obj: obj.update(scope_states={target: event["new_state"].lower()},
                         stop_receipts=[ref], jobs=before["jobs"], submission_ledger=before["submission_ledger"],
                         spent_job_submissions=1))
        return target

    def test_observed_stop_and_close_without_a_new_job_or_claim(self):
        with patch.object(v, "PDB_SHA", v.sha(b"unit-test coordinates")):
            for close in (False, True):
                self.stop_witness(close)
                self.assertEqual(self.execute(), ("observed", 1))

    def test_stop_prose_without_native_receipt_fails(self):
        self.stop_witness()
        self.rewrite(0, "settled", lambda obj: obj.update(stop_receipts=[]))
        with patch.object(v, "PDB_SHA", v.sha(b"unit-test coordinates")), self.assertRaisesRegex(
                ValueError, "missing durable stop event"):
            self.execute()

    def test_stop_rollback_on_reopen_fails(self):
        target = self.stop_witness(close=True)
        self.rewrite(0, "reopened", lambda obj: obj["scope_states"].update({target: "active"}))
        with patch.object(v, "PDB_SHA", v.sha(b"unit-test coordinates")), self.assertRaisesRegex(
                ValueError, "durable across reopen"):
            self.execute()

    def test_foreign_stop_event_decision_fails(self):
        self.stop_witness()
        def rewrite_cause(obj):
            event = v.document(self.root, obj["stop_receipts"][0])
            wrong = json.loads(event["payload_json"])
            wrong["decision_id"] = "someone-else"
            event["payload_json"] = wrong
            obj["stop_receipts"] = [write_ref(self.root, "wrong-event", event)]
        self.rewrite(0, "settled", rewrite_cause)
        with patch.object(v, "PDB_SHA", v.sha(b"unit-test coordinates")), self.assertRaisesRegex(
                ValueError, "another decision"):
            self.execute()

    def test_observed_origin_checks_actual_job_and_coordinate_bytes(self):
        self.data = self.create("observed")
        with self.assertRaisesRegex(ValueError, "2RE9 source bytes"):
            self.execute()
        # Synthetic-only test of positive branch. Production has a frozen real digest.
        with patch.object(v, "PDB_SHA", v.sha(b"unit-test coordinates")):
            self.assertEqual(self.execute(), ("observed", 1))
            self.rewrite(0, "before", lambda obj: obj["jobs"][0].update(input_sha256="other"))
            with self.assertRaisesRegex(ValueError, "finished 2RE9 Job"):
                self.execute()


if __name__ == "__main__":
    unittest.main()
