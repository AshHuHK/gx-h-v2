"""Ordinary Goal, real transport envelopes at the controlled host boundary and a killed Driver."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest

from acceptance.harness_refactor.modules.research_study import ResearchStudy
from acceptance.v622.test_close import actual_packet
from gx_core import budgets
from gx_core.errors import GxError
from gx_core.persistence import store
from gx_core.p0 import metering, waits
from acceptance.v62a.interleavings_fixture import draft
from gx_core.p0 import conversation, intent, pi_materials, research_dialogue, v62_modules, v6_jobs
from gx_core.p0.driver import Driver

TEXT = "Use the dated field record to reassess the coverage gap."


def route(study, scope, mid, attachment):
    conversation.send(study.conn,study.run_id,TEXT,mid,branch_id=scope,attachments=[attachment])
    intent.bind_main(study.conn,study.run_id,"main")
    intent.stage_input(study.conn,study.run_id,mid,{"kind":"text","text":TEXT})
    intent.accept_input(study.conn,study.run_id,"main","turn-"+mid,mid)
    ref = pi_materials.for_message(study.conn,study.run_id,mid)[0]["artifact_ref"]
    driver = Driver(study.conn,study.state,study.run_id)
    try:
        response = driver.main_read("gx_route_pi_message",{"clauses":[
            {"kind":"material","quote":TEXT,"branch_id":scope,"artifact_refs":[ref]}]},
            {"thread_id":"main","turn_id":"turn-"+mid,"call_id":"route-"+mid})
    finally:
        driver.close()
    return ref,response["clauses"][0]


def researcher(study, key, methods=(), *, limits=None):
    args = study.open_args(key,caps=[],inputs=study.materials,limits=limits)
    args.update(module_id="ad_hoc",focus="Read the dated samples",communication="isolated",
                expected_delivery="State which dates are supported.",method_choices=list(methods))
    rec = study.call(study.lead,"gx_open_module",args,"open-"+key)
    return rec,study.next_actor(rec["scope"])


def owner_tasks(study, scope):
    return study.rows("SELECT t.task_id,t.attempt,t.attempts_consumed,t.status,t.lease_id FROM v6_decision_tasks d "
                      "JOIN tasks t USING(task_id) WHERE d.run_id=? AND d.scope=? ORDER BY d.rowid",
                      study.run_id,scope)


class DirectedLateMaterial(unittest.TestCase):
    def setUp(self):
        self.directory=Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.spec={"research_effort":"medium","research_modules":["ad_hoc@1"],"capabilities":[]}

    def test_cold_same_run_fences_stale_report_and_delivers_private_original_once(self):
        with ResearchStudy(self.directory,spec_updates=self.spec) as old:
            keeper,reader=researcher(old,"records",limits={"worker_tasks":1})
            control,peer=researcher(old,"independent")
            old.call(reader,"gx_read_artifact",{"ref":old.materials[0]},"initial-reading")
            previous=actual_packet(old,reader)
            original_peer=actual_packet(old,peer)
            original_counter=owner_tasks(old,keeper["scope"])[0]["attempts_consumed"]
            run=old.run_id
        proc=subprocess.run([sys.executable,"-B",__file__,"--route-and-die",str(self.directory),keeper["scope"]],
                            env={**os.environ,"GX_CODEX_OFFLINE":"1"},capture_output=True,timeout=20)
        self.assertEqual(proc.returncode,-signal.SIGKILL,proc.stderr)
        with ResearchStudy(self.directory,reopen=True) as new:
            self.assertEqual(new.run_id,run)
            ref=pi_materials.for_message(new.conn,run,"notebook")[0]["artifact_ref"]
            self.assertNotIn(ref,json.dumps(previous))
            self.assertEqual(actual_packet(new,peer),original_peer)
            new.roots.active_turns[reader.thread]=reader.turn
            new.roots.active_turns[peer.thread]=peer.turn
            with self.assertRaises(GxError):
                new.report(reader,new.materials[0],key="obsolete")
            with self.assertRaises(GxError):
                new.call(peer,"gx_read_artifact",{"ref":ref},"foreign")
            stopped=owner_tasks(new,keeper["scope"])[0]
            self.assertEqual((stopped["status"],stopped["attempts_consumed"]),("CANCELLED",original_counter))
            v62_modules.advance(new.conn,run)
            successor=new.next_actor(keeper["scope"])
            ack=actual_packet(new,successor)
            self.assertNotEqual(ack["text_sha256"],previous["text_sha256"])
            self.assertIn(ref,[m["artifact_ref"] for m in ack["packet"]["materials"]])
            self.assertIn(TEXT,json.dumps(ack["packet"]["pi_dialogue"]))
            dated=new.call(successor,"gx_read_artifact",{"ref":ref},"read-late")
            self.assertIn("west gauge log",json.dumps(dated))
            tasks=owner_tasks(new,keeper["scope"])
            first_feedback=research_dialogue.feedback_status(new.conn,run,"notebook")[0]["feedback_id"]
            # Retry the original Main callback, then send the same already visible original under another PI message.
            driver=Driver(new.conn,new.state,run)
            try:
                driver.main_read("gx_route_pi_message",{"clauses":[{"kind":"material","quote":TEXT,
                    "branch_id":keeper["scope"],"artifact_refs":[ref]}]},
                    {"thread_id":"main","turn_id":"turn-notebook","call_id":"route-notebook"})
            finally:driver.close()
            route(new,keeper["scope"],"repeat-original",{"artifact_ref":ref})
            v62_modules.advance(new.conn,run)
            self.assertEqual(owner_tasks(new,keeper["scope"]),tasks)
            self.assertEqual(len(owner_tasks(new,control["scope"])),1)
            new.call(successor,"gx_read_artifact",{"ref":new.materials[0]},"read-common")
            decision={"feedback_id":first_feedback,"outcome":"adopt","reason":"The dated record narrows the gap."}
            report=new.report(successor,new.materials[0],key="reconsidered",text="Limit conclusions to dated records.",
                              pi_dispositions=[decision])
            self.assertEqual(report["status"],"COMPLETE")
            handled=research_dialogue.feedback_status(new.conn,run,"notebook")[0]
            self.assertEqual((handled["status"],handled["disposition"]["outcome"]),("DISPOSED","adopt"))
            if dest := os.environ.get("GX_LATE_MATERIAL_PROOF"):
                Path(dest).write_text(json.dumps({"model_transport":"controlled native RPC","fault":"SIGKILL",
                    "run_id":run,"module":keeper["scope"],"previous_task":reader.task_id,
                    "new_task":successor.task_id,"second_original":ref,"prior_brief_sha256":previous["text_sha256"],
                    "new_brief_sha256":ack["text_sha256"],"report":report["report_ref"],
                    "feedback_id":first_feedback,"feedback_status":handled["status"],"outcome":handled["disposition"]["outcome"],
                    "failure_counter_change":stopped["attempts_consumed"]-original_counter,"sibling_tasks":1,"job_count":0},indent=2)+"\n")

    def test_one_worker_refresh_uses_only_remaining_model_allowance_then_new_work_stops(self):
        parent = {"job_submissions":10, "worker_tasks":12, "cpu_seconds":400, "api_calls":24,
                  "tokens":500000, "gpu_jobs":0, "gpu_minutes":0, "wall_hours":2}
        spec = {**self.spec, "run_budget":parent, "workspace_budget":parent, "turn_token_budget":65536}
        with ResearchStudy(self.directory, spec_updates=spec) as study:
            study.roots.token_budget = spec["turn_token_budget"]  # Match Driver config at the controlled host boundary.
            limits = {"worker_tasks":1, "api_calls":6, "tokens":131072}
            rec, reader = researcher(study, "one-worker", limits=limits)
            module = v62_modules._module(study.conn, study.run_id, rec["scope"])
            budget = module["budget_ref"]

            def terminal_receipt(actor, amount):
                reservation = study.rows("SELECT reservation_ref FROM p0_root_deliveries WHERE thread_id=? "
                                         "AND turn_id=?", actor.thread, actor.turn)[0]["reservation_ref"]
                with store.transaction(study.conn, transaction_owner="LateMaterialAcceptance",
                                       operation="settle_scripted_usage", run_id=study.run_id):
                    study.conn.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status,total_tokens) "
                                       "VALUES(?,?,'completed',?)", (actor.thread, actor.turn, amount))
                    waits.meter_pending(study.conn, actor.thread, actor.turn)
                return reservation

            def confirm(reservation, amount):
                with store.transaction(study.conn, transaction_owner="LateMaterialAcceptance",
                                       operation="apply_scripted_usage", run_id=study.run_id):
                    metering.confirm(study.conn, reservation, amount)

            initial_hold = terminal_receipt(reader, 125071)
            first, _ = route(study, rec["scope"], "gauge-b", {"name":"b.txt","text":"Second gauge."})
            confirm(initial_hold, 125071)
            prior_waits = study.rows("SELECT wait_id,root_key FROM p0_attempt_waits WHERE wait_key=?", initial_hold)
            self.assertEqual(len(prior_waits), 1)
            before = budgets.state(study.conn, budget)
            self.assertEqual((before["spent"]["worker_tasks"], before["available"]["tokens"]), (1,6001))
            v62_modules.advance(study.conn, study.run_id)
            successor = study.next_actor(rec["scope"])
            self.assertIn(first, json.dumps(actual_packet(study,successor)["packet"]["materials"]))
            admitted = budgets.state(study.conn, budget)
            self.assertEqual(admitted["limits"], before["limits"])
            self.assertEqual((admitted["spent"]["worker_tasks"], admitted["reserved"]["api_calls"],
                              admitted["reserved"]["tokens"]), (1,1,6001))
            self.assertEqual(len([r for r in admitted["reservations"] if r["subject_kind"]=="task"]),1)
            self.assertEqual(len(study.rows("SELECT event_id FROM events WHERE kind='v62.module.worker_task_continued' "
                                            "AND subject_id=?", successor.task_id)), 1)
            for _ in range(2):
                v62_modules.advance(study.conn, study.run_id)
            self.assertEqual(budgets.state(study.conn,budget), admitted)
            self.assertEqual(study.rows("SELECT wait_id,root_key FROM p0_attempt_waits WHERE wait_key=?", initial_hold),
                             prior_waits)
            self.assertFalse(study.rows("SELECT job_id FROM jobs WHERE run_id=?", study.run_id))

            # A completed scientific decision has used its slot; a genuinely new task needs a new one.
            study.call(successor, "gx_decide_revision", {"decision":draft()}, "completed-work")
            confirm(terminal_receipt(successor, 12), 12)
            v62_modules.advance(study.conn, study.run_id)
            self.assertEqual([r["status"] for r in owner_tasks(study, rec["scope"])], ["CANCELLED","ACCEPTED"])
            from gx_core.artifacts import ArtifactService
            ref, = study.conn.execute("SELECT report_ref FROM v62_module_reports WHERE module_instance_id=?",
                                      (rec["module_instance_id"],)).fetchone()
            report = ArtifactService(study.conn).read_json(ref)
            self.assertEqual(report["gaps"], ["module worker task allowance exhausted"])
            final = budgets.state(study.conn, budget)
            self.assertEqual((final["spent"]["worker_tasks"], final["spent"]["api_calls"],
                              final["spent"]["tokens"]), (1,2,125083))

    def test_new_original_wakes_while_prior_cpu_job_continues_and_other_report_is_fixed(self):
        root=Path(__file__).resolve().parents[3]
        method="archival-peptide-coverage@1"
        source=root/"acceptance/science_assets/modalities/peptide/materials/1UAO.cif"
        spec={**self.spec,"input_files":[{"role":"original","path":str(source)}],
              "capability_collections":["archived_structure"]}
        with ResearchStudy(self.directory,spec_updates=spec) as study:
            rec,reader=researcher(study,"processor",[method])
            _,peer=researcher(study,"other")
            study.call(peer,"gx_read_artifact",{"ref":study.materials[0]},"read-other")
            report=study.report(peer,study.materials[0],key="before-grant")
            study.call(reader,"gx_select_capability",{"method":method,"action":"enable","revision":0},"select")
            revision=study.submit_node(reader,"inventory",capability=method)
            node=revision["aliases"]["local:inventory"]
            v6_jobs.advance(study.conn,study.run_id)
            job=study.rows("SELECT job_id,status FROM v6_nodes WHERE node_id=?",node)[0]
            self.assertEqual(job["status"],"RUNNING")
            self.assertFalse(v62_modules.advance(study.conn,study.run_id))
            ref,_=route(study,rec["scope"],"new-dates",{"name":"dated.txt","text":"East gauge inspection."})
            self.assertTrue(v62_modules.advance(study.conn,study.run_id))
            next_reader=study.next_actor(rec["scope"])
            self.assertIn(ref,[m["artifact_ref"] for m in actual_packet(study,next_reader)["packet"]["materials"]])
            self.assertEqual(study.settle(node)["job_id"],job["job_id"])
            self.assertEqual(study.rows("SELECT job_id FROM jobs WHERE run_id=?",study.run_id),[{"job_id":job["job_id"]}])
            preserved,=study.rows("SELECT p.report_ref,a.sha256 AS report_sha256 FROM v62_module_reports p "
                                  "JOIN artifacts a ON a.artifact_ref=p.report_ref WHERE p.run_id=?",study.run_id)
            self.assertEqual(preserved,{"report_ref":report["report_ref"],"report_sha256":report["report_sha256"]})
            if dest := os.environ.get("GX_LATE_JOB_PROOF"):
                Path(dest).write_text(json.dumps({"native_transport":"controlled RPC", "cpu_job":"real",
                    "run_id":study.run_id,"job_id":job["job_id"],"status_when_granted":job["status"],
                    "settled":study.rows("SELECT status FROM jobs WHERE job_id=?",job["job_id"])[0]["status"],
                    "external_job_count":1,"preserved_sibling_report":preserved},indent=2)+"\n")

    def test_revocation_between_assignment_and_recovery_cannot_reopen_the_recipient(self):
        with ResearchStudy(self.directory,spec_updates=self.spec) as study:
            rec,old=researcher(study,"reader")
            ref,_=route(study,rec["scope"],"notice",{"name":"field.txt","text":"A dated field original."})
            study.call(study.lead,"gx_cancel_module",{"module_instance_id":rec["module_instance_id"],
                "reason":"The PI revoked this investigation.","operation_key":"cancel"},"cancel")
            run=study.run_id
        with ResearchStudy(self.directory,reopen=True) as cold:
            v62_modules.advance(cold.conn,run)
            self.assertEqual(len(owner_tasks(cold,rec["scope"])),1)
            cold.roots.active_turns[old.thread]=old.turn
            with self.assertRaises(GxError):
                cold.call(old,"gx_read_artifact",{"ref":ref},"after-cancel")


if __name__=="__main__" and len(sys.argv)>1 and sys.argv[1]=="--route-and-die":
    with ResearchStudy(Path(sys.argv[2]),reopen=True) as study:
        route(study,sys.argv[3],"notebook",{"name":"dated.txt","text":"The west gauge log covers Tuesday."})
        os.kill(os.getpid(),signal.SIGKILL)
