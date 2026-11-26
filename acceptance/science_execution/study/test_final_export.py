"""Two real CPU Jobs through a local scripted Goal. No provider or network is used."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from acceptance.science_execution.study import operate, protocol, receipt, synthetic
from acceptance.v622.public import batch, funding
from acceptance.v622.public.enrollment import read, save
from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import draft
from gx_core.p0 import v621_policy

RECHECK = "Measure angular change in a second interval before recommending a reporting rule."
FINAL = ("Restrict the RMSD recommendation to interval 21–40. For angular change in 101–120, "
         "all three pairs are within the reporting limit.")


class ScriptedStudy(CloseStudy):
    """Record the scripted transport's actual GX tool exchange in the rollout wire format."""

    def frame(self, actor, kind, payload):
        directory = self.directory / "codex/sessions"
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / (actor.thread + ".jsonl")).open("a") as stream:
            stamp = datetime.now(timezone.utc).isoformat()
            stream.write(json.dumps({"type": kind, "timestamp": stamp, "payload": payload}) + "\n")

    def bind(self, task_id):
        actor = super().bind(task_id)
        brief, = self.conn.execute("SELECT brief FROM p0_roots WHERE thread_id=?", (actor.thread,)).fetchone()
        self.frame(actor, "session_meta", {"id": actor.thread, "model_provider": "gx_loopback"})
        self.frame(actor, "turn_context", {"model": "fixture", "effort": "high", "turn_id": actor.turn})
        self.frame(actor, "response_item", {"role": "user", "content": [{"type": "input_text", "text": brief}]})
        return actor

    def call(self, actor, tool, args, key):
        self.frame(actor, "response_item", {"type": "function_call", "name": tool, "call_id": key,
                                           "arguments": json.dumps(args)})
        result = super().call(actor, tool, args, key)
        self.frame(actor, "response_item", {"type": "function_call_output", "call_id": key,
                                           "output": json.dumps(result)})
        return result


def make_study(work):
    vault, slot = work / "custody", work / "lead"
    vault.mkdir(parents=True)
    (slot / "raw").mkdir(parents=True)
    (slot / "raw/requests.jsonl").touch()
    specimen = operate.fixture(vault)
    card = protocol.method_card(specimen["lead_packet"], operate.METHOD_PATH.read_text())
    save(slot / "method-card.json", card)
    save(vault / "companion.json", {"source": "fixture-mapping", "path": str(vault / "mapping.json"),
         "role": "mapping", "sha256": specimen["mapping_sha256"], "media_type": "application/json",
         "metadata_path": str(vault / "acquisition.json")})
    save(work / "seal.json", {"series": "local-export-regression", "qualification": "ELIGIBLE",
         "synthetic": True, "blind_id": card["blind_id"], "model": "fixture", "effort": "high"})
    grant = {"evidence_policy": v621_policy.VERSION, "research_effort": "deep", "research_modules": [],
             "plugins": [protocol.PLUGIN], "capabilities": [protocol.METHOD], "initial_job_limit": 2,
             "input_files": [{"role": "workflow_spec", "path": str(slot / "method-card.json")}]}
    with ScriptedStudy(slot, spec_updates=grant) as s:
        save(slot / "first-attempt.json", {"series": "local-export-regression", "run_id": s.run_id,
             "research_window": {}})
        card_ref, = s.materials
        tool, args = synthetic.step(s.brief(s.lead), {("gx_read_artifact", card_ref): card})
        s.call(s.lead, tool, args, "preregister")
        with (work / ".local-cli.lock").open("w") as lock:
            cli = batch.native_cli(slot, {**os.environ, "GX_CODEX_OFFLINE": "1"}, lock.fileno())
            operate.open_once(work, s.run_id, cli)

        actor = s.next_actor("run")
        tool, args = synthetic.step(s.brief(actor), {})
        primary = s.call(actor, tool, args, "primary")["aliases"]["local:primary"]
        first = s.settle(primary)["result_ref"]
        actor = s.next_actor("run")
        s.call(actor, "gx_read_artifact", {"ref": first}, "read-primary")
        op = dict(args["decision"]["ops"][0], node="local:recheck",
                  parameters={**args["decision"]["ops"][0]["parameters"], "interval": "101-120",
                              "endpoint": "angle_deg"})
        followup = draft(op, evidence=[first], watches=[{"event": "job_settled", "subject_ref": op["node"]}])
        followup["rationale"] = RECHECK
        check = s.call(actor, tool, {"decision": followup}, "request-recheck")["aliases"]["local:recheck"]
        second = s.settle(check)["result_ref"]
        actor = s.next_actor("run")
        s.call(actor, "gx_read_artifact", {"ref": second}, "read-recheck")
        close = draft({"op": "close_run", "claim_refs": [], "reason": FINAL,
                       "unresolved": ["Other intervals remain unmeasured."]}, evidence=[second])
        close["rationale"] = FINAL
        close["metric_refs"] = [m["metric_ref"] for o in s.brief(actor)["observations"]
                                if o["artifact_ref"] == second for m in o["verified_metrics"]
                                if m["metric_id"] == "median_delta_angle_deg"]
        closed = s.call(actor, tool, {"decision": close}, "final-close")
        s.advance()
        return s.run_id, closed["decision_ref"], (first, second)


class FinalExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if target := os.environ.get("GX_EXPORT_TEST_WORK"):
            cls.work = Path(target).resolve()
            cls.work.mkdir(exist_ok=False, parents=True)
        else:
            directory = tempfile.TemporaryDirectory()
            cls.addClassCleanup(directory.cleanup)
            cls.work = Path(directory.name)
        cls.rid, cls.final_ref, cls.outputs = make_study(cls.work)

    def export(self, work):
        reg = read(work / "seal.json")
        ledger = funding.BatchFunding(work / "custody/account.sqlite", series=reg["series"],
                                     slots=["lead"], batch_cap=1_000_000)
        return receipt.export(work, self.rid, reg, operate.rehearse.state_hash(work / "lead", self.rid), ledger)

    def test_changed_final_conclusion_and_both_measurements(self):
        result = self.export(self.work)
        self.assertEqual(result["qc_recommendation"], FINAL)
        self.assertEqual(result["final_decision"]["decision_ref"], self.final_ref)
        self.assertGreater(result["closure"]["closed_event"], result["final_decision"]["event_id"])
        primary, recheck = result["jobs"]
        self.assertEqual(primary["table"]["interpretation"], "MEDIAN_ALSO_OVER_LIMIT")
        self.assertEqual(recheck["table"]["interpretation"], "REPRESENTATIVE_WITHIN_LIMIT")
        self.assertIn(recheck["commission"], primary["followup_refs"])
        self.assertIn(self.final_ref, recheck["followup_refs"])
        self.assertEqual([j["output"]["artifact_ref"] for j in result["jobs"]], list(self.outputs))
        self.assertEqual([j["independent"]["status"] for j in result["jobs"]], ["PASS", "PASS"])
        report = (self.work / "report.md").read_text()
        self.assertIn(FINAL, report)
        for job in result["jobs"]:
            table = job["table"]
            self.assertIn(table["interval"], report)
            self.assertIn(str(table["rows"]["median"][table["endpoint"]]["delta"]), report)
        self.assertEqual(self.export(self.work), result)

    def test_missing_close_wrong_version_or_undelivered_recheck(self):
        for fault in ("close-event", "goal-version", "read-output"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temporary:
                work = Path(temporary) / "damaged"
                shutil.copytree(self.work, work, ignore=shutil.ignore_patterns("study-receipt.json", "report.md"))
                if fault == "read-output":
                    for path in (work / "lead/codex/sessions").glob("*.jsonl"):
                        lines = [json.loads(line) for line in path.read_text().splitlines()]
                        kept = [line for line in lines if not (line["payload"].get("call_id") == "read-recheck"
                                and line["payload"].get("type") == "function_call_output")]
                        path.write_text("".join(json.dumps(line) + "\n" for line in kept))
                else:
                    with closing(sqlite3.connect(work / "lead/state/gx.db")) as db:
                        if fault == "close-event":
                            db.execute("DELETE FROM events WHERE run_id=? AND kind='v6.run.closed'", (self.rid,))
                        else:
                            db.execute("UPDATE v6_goals SET goal_version=goal_version+1 WHERE run_id=?", (self.rid,))
                        db.commit()
                with self.assertRaises(ValueError):
                    self.export(work)
                self.assertFalse((work / "study-receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
