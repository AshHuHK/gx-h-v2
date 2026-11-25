"""Exercise real GX material handoff and a crash-resumable four-slot controller without provider turns."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from acceptance.v62a.interleavings_fixture import Scenario, draft
from acceptance.v622.public.enrollment import digest, read, save
from acceptance.v623.oauth import host, judge, pilot
from acceptance.v62c import materials, rehearse


class Controller(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.work = self.root/"cohort"
        self.p = pilot.protocol()

    def test_first_officially_accepted_plan_anchors_exact_assignment_and_recovery(self):
        slot = self.work/"01"
        self.work.mkdir()
        materials.prepare(self.work/"custody")
        spec = pilot.cohort_grant("single", self.p)
        with Scenario(slot, spec_updates={**spec, "historical_replay": True}) as scenario:
            (slot/"raw").mkdir()
            with (slot/".lock").open("a") as lock:
                cli = pilot.native.native_cli(slot,{**os.environ,"GX_CODEX_OFFLINE":"1"},lock.fileno())
                receipt = pilot.native.parent_observation(slot,scenario.run_id,scenario.materials[0],cli)
                self.assertEqual(receipt["artifact_ref"],scenario.materials[0])
                item = self.p["slots"][0]
                def handoff():
                    return pilot.reconcile_assignment(slot,self.work,item,scenario.run_id,cli,scenario.materials[0])
                self.assertFalse(handoff())
                old = scenario.call(scenario.lead,"gx_decide_revision",{"decision":draft()},"first")
                self.assertTrue(handoff())
                original = read(slot/"assignment.json")
                self.assertEqual(original["decision_ref"],old["decision_ref"])
                self.assertEqual(original["sha256"],self.p["materials"]["P4"]["sha256"])
                (slot/"assignment.json").unlink()  # GX committed, operator receipt lost.
                self.assertTrue(handoff())
                self.assertEqual(read(slot/"assignment.json"),original)
                self.assertEqual(scenario.conn.execute("SELECT count(*) FROM events WHERE kind='v621.material.assigned'").fetchone()[0], 1)
                self.assertFalse(pilot.active_provider_hold(slot,scenario.run_id))
                self.assertGreater(spec["run_budget"]["tokens"], 1_000_000)
                self.assertIsNone(self.p["token_cap_per_slot"])
                self.assertIsNone(self.p["token_cap_total"])

    def test_sequence_holds_unsettled_slot_or_judge_and_recovers_same_assignments(self):
        work = self.work
        work.mkdir()
        reg = {**self.p, "source_commit":"synthetic-offline", "approval_sha256":"synthetic",
               "official_binary_sha256":digest(Path(sys.executable)), "protocol_sha256":pilot.registration.sha(self.p)}
        save(work/"registration.json",reg)
        save(work/"result.json",{"registration_sha256":digest(work/"registration.json"),
                                 "cases":{i:{"status":"REGISTERED"} for i in pilot.SLOTS}})
        science, review, runs = {}, {}, []
        def operator(slot,work,item,reg,binary,fd,resume):
            num = slot.name
            self.assertFalse(resume)
            runs.append(num)
            save(slot/"summary.json",{"status":"CLOSED","run_id":"synthetic-"+num})
            science[num] = {"status":"PENDING","total_tokens":None}
            return read(slot/"summary.json")
        def check(num,verb):
            return pilot.process(work,num,verb,Path(sys.executable),None,"synthetic")
        def judge_state(work,num,p,**kw):
            return review.get(num,{"status":"UNKNOWN","total_tokens":None})
        with ExitStack() as stack:
            stack.enter_context(patch.object(pilot,"check_approval",return_value={"candidate":"synthetic-offline"}))
            stack.enter_context(patch.object(pilot,"ensure_ready",return_value={}))
            stack.enter_context(patch.object(pilot,"run_science",side_effect=operator))
            stack.enter_context(patch.object(host,"reconcile",side_effect=lambda slot,rid: science[slot.name]))
            stack.enter_context(patch.object(judge,"reconcile",side_effect=judge_state))
            stack.enter_context(patch.object(judge,"prepare"))
            stack.enter_context(patch.object(judge,"run",side_effect=judge_state))
            with self.assertRaisesRegex(ValueError,"Earlier original slot"):
                check("02","run")
            for num in pilot.SLOTS:
                self.assertEqual(check(num,"run")["status"],"HELD")
                self.assertEqual(check(num,"reconcile")["accounting"],"PENDING")
                science[num] = {"status":"SETTLED","total_tokens":18}
                self.assertEqual(check(num,"resume")["status"],"AWAITING_JUDGE")
                review[num] = {"status":"SETTLED","judgment_status":"REVIEWED","total_tokens":6}
                self.assertEqual(check(num,"reconcile")["total_tokens"],24)
            self.assertEqual(runs,list(pilot.SLOTS))
            science["01"] = {"status":"ADDITIONAL_CHARGE","total_tokens":None}
            with self.assertRaisesRegex(ValueError,"earlier slot"):
                check("02","resume")
            self.assertEqual(read(work/"result.json")["cases"]["01"]["status"],"HELD")

    def test_approval_cannot_bind_another_work_directory(self):
        approval = {"work":str(self.work),"series":self.p["series"],"candidate":"sha"}
        with patch.object(pilot.authorization,"verify",return_value=approval), patch.object(pilot,"check_candidate"):
            self.assertEqual(pilot.check_approval(self.work,"private-grant","pinned"),approval)
            with self.assertRaisesRegex(ValueError,"different cohort"):
                pilot.check_approval(self.root/"replacement","private-grant","pinned")


if __name__ == "__main__":
    unittest.main()
