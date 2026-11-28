"""Exercise mid-adoption process death and permanent upstream loss through the research CLI."""
import json
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from acceptance.harness_refactor.macro import test_macro_recipe as prior
from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.p0 import research_joins

QUESTION = "Can an independent reading support the archive chronology?"


def seed(work, entry):
    rid = entry._start(work, "first-study", "The dated clock note records a clock correction.")
    with Scenario(work, reopen=True) as s:
        entry._bind(s, rid)
        ref, = s.materials
        modules = []
        for slot in ["clock", "audit"]:
            args = dict(module_id="ad_hoc", module_version="1", round=len(modules) + 1, operation_key=slot,
                        focus="Read the supplied archive.", expected_delivery="Report the clock evidence.",
                        input_refs=[ref], capabilities=["artifact-analysis@1"], method_choices=[],
                        tools=["gx_decide_revision","gx_read_artifact","gx_submit_module_report"],
                        sub_budget={"api_calls":2, "tokens":2000, "worker_tasks":2})
            lead = s.lead if not modules else s.next_actor("run")
            opened = s.call(lead, "gx_open_module", args, "delegate-" + slot)
            mid = opened["module_instance_id"]
            modules.append(dict(slot=slot, module_instance_id=mid, question="Inspect this archive.",
                                expected_delivery="State whether its evidence supports the current chronology."))
            if slot == "audit":
                s.call(lead,"gx_watch_join",dict(key="gate", question=QUESTION, mode="all",
                       ports=[{"slot":"audit", "alternatives":[{"module_instance_id":mid,"output_name":"report"}]}]),"gate")
            s.call(lead, "gx_decide_revision", {"decision":draft()}, "await-" + slot)
            worker = s.bind(opened["task_id"])
            s.call(worker, "gx_read_artifact", {"ref":ref}, "inspect-" + slot)
            ref = s.report(worker, ref, key=slot, text="The shared archive documents the clock reading.")["report_ref"]
        lead = s.next_actor("run")
        return s.call(lead, "gx_save_recipe", dict(recipe_id="archive-review", version="1", title="Archive chronology",
             nodes=[], inputs=[{"name":"log", "artifact_ref":s.materials[0]}], id_alignment="station",
             units="minutes", missing_values="unresolved", macro={"modules":modules,"join_key":"gate"}), "save")


def cancel_in_process(work, destination, explicit):
    entry = prior.NativeMacro()
    saved = seed(work, entry)
    rid = entry._start(work, "next-study", "The current station log records no clock correction.")
    with Scenario(work, reopen=True) as study:
        entry._bind(study, rid)
        request = dict(recipe=saved["reference"],digest=saved["digest"],prefix="station",
                       branch="local:archives",inputs={"log":study.materials[0]})
        expanded = study.call(study.lead,"gx_instantiate_recipe",request,"preview")
        if research_joins.view(study.conn,rid):
            raise AssertionError("An unadopted preview registered a scientific dependency")
        if explicit:
            study.call(study.lead,"gx_instantiate_recipe",dict(request,adopt=True),"adopt")
            if len(research_joins.view(study.conn,rid)) != 1:
                raise AssertionError("A committed instance must have its dependency before either module is born")
        upstream, = expanded["module_calls"]
        opened = study.call(study.lead, upstream["tool"], upstream["arguments"], "open")
        cancelled = study.call(study.lead, "gx_cancel_module", dict(module_instance_id=opened["module_instance_id"],
                  operation_key="cancel-clock",reason="The archive commission was withdrawn."), "stop")
        for n in range(2):
            study.call(study.lead,"gx_instantiate_recipe",request,"preview-again-"+str(n))
        study.call(study.lead, "gx_decide_revision", {"decision":draft(watches=[
            {"event": "goal_changed", "subject_ref": "run"}])}, "await-macro-delivery")
        rows = research_joins.view(study.conn,rid)
        destination.write_text(json.dumps(dict(run_id=rid, rows=rows, upstream=opened["module_instance_id"],
                                cancelled=cancelled["status"], recipe=saved["reference"],digest=saved["digest"])))
        os.kill(os.getpid(), signal.SIGKILL)


class AdoptedRecipe(unittest.TestCase):
    def verify_restart(self, explicit):
        with tempfile.TemporaryDirectory() as dirname:
            work = Path(dirname)
            receipt = work/"before-restart.json"
            proc=subprocess.run([sys.executable,"-B",__file__,"--crash",dirname,str(receipt),str(int(explicit))],
                                capture_output=True,text=True)
            self.assertEqual(proc.returncode,-signal.SIGKILL,proc.stderr+proc.stdout)
            before=json.loads(receipt.read_text())
            self.assertEqual(len(before["rows"]),1,"Accepted recipe lost its logical Join before the new controller opened")
            rid=before["run_id"]
            with Scenario(work,reopen=True) as s:
                s.run_id=rid
                research_joins.tick(s.conn,rid)
                row, = research_joins.view(s.conn,rid)
                self.assertEqual(row["state"],"UNREACHABLE")
                audit, = row["outcome"]["ports"]
                self.assertEqual(audit["slot"],"audit")
                self.assertIn(before["upstream"],json.dumps(audit))
                self.assertIn("module_closed_without_report",json.dumps(audit))
                for _ in range(2):
                    self.assertEqual(research_joins.tick(s.conn,rid),[])
                events = dict(s.conn.execute("SELECT kind,COUNT(*) FROM events WHERE run_id=? AND subject_kind='research_join' GROUP BY kind",(rid,)))
                self.assertEqual(events,{research_joins.REGISTERED:1,research_joins.READY:1})
                self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM v62_modules WHERE run_id=?",(rid,)).fetchone()[0],1)
                self.assertEqual(s.conn.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?",(rid,)).fetchone()[0],0)
                prior.NativeMacro()._bind(s, rid)
                delivered, = s.brief(s.lead)["research_joins"]
                self.assertEqual(delivered["outcome_event"], row["outcome_event"])
                self.assertEqual(delivered["outcome"]["ports"], row["outcome"]["ports"])
                if loc:=os.environ.get("GX_ADOPTION_RECEIPT"):
                    Path(loc+"-"+str(int(explicit))+".json").write_text(json.dumps(dict(transport="SCRIPTED_CALLBACK",
                           controller_exit=proc.returncode,run_id=rid,source=before["recipe"],digest=before["digest"],
                           events=events,outcome=row["outcome"],target_modules=1,target_jobs=0,
                           receiving_lead_task=s.lead.task_id,delivered_event=delivered["outcome_event"]),indent=2)+"\n")

    def test_first_actual_commission_registers_gate_before_crash(self):
        self.verify_restart(False)

    def test_explicit_logical_adoption_precedes_any_physical_commission(self):
        self.verify_restart(True)


if __name__ == "__main__" and "--crash" in sys.argv:
    cancel_in_process(Path(sys.argv[2]),Path(sys.argv[3]),bool(int(sys.argv[4])))
elif __name__ == "__main__":
    unittest.main()
