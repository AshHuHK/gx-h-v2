"""Rebind reviewed numerical filters in ordinary research with real CPU outputs."""
from contextlib import ExitStack, contextmanager
from dataclasses import asdict
from copy import deepcopy
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from acceptance.harness_refactor.field.run import hydro_release
from acceptance.harness_refactor.macro import test_macro_recipe as macro_fixture
from acceptance.v62a.interleavings_fixture import Actor, Scenario, draft
from gx_core import config, contracts
from gx_core.capabilities import manifest as capabilities
from gx_core.errors import SchemaInvalid, ScopeDenied
from gx_core.p0 import v6_recipes
from tests.test_v6_decisions import branch


def record(path, minutes, volume):
    path.write_text(json.dumps({"schema":"gx.hydrometry-summary/1", "trial":path.stem,
        "channels":["a","b"], "window_minutes":[0,120],
        "summaries":{"a":{"observed_minutes":minutes,"volume_L":volume},
                     "b":{"observed_minutes":minutes,"volume_L":volume+1}}}))


@contextmanager
def deployment(release):
    with ExitStack() as stack:
        root = release / "gx-h"
        for owner, attr, val in ((config,"HARNESS_ROOT",root),(contracts,"HARNESS_ROOT",root),
                (contracts,"CONFIG_DIR",root/"config"),(config,"CONFIG_DIR",root/"config"),
                (capabilities,"HARNESS_ROOT",root)):
            stack.enter_context(patch.object(owner,attr,val))
        yield


def attach(s, run_id):
    from gx_core.p0.roots import Roots
    s.run_id = run_id
    old = s.roots
    s.roots = Roots(s.conn,run_id=run_id,cwd=s.state,token_budget=10)
    s.roots.client = old.client
    files = s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?",(run_id,)).fetchone()
    s.materials = [f["artifact_ref"] for f in json.loads(files[0])["files"]]


def killed_controller(instructions):
    job = json.loads(Path(instructions).read_bytes())
    work = Path(job["work"])
    with deployment(work/"release"), Scenario(work,reopen=True) as s:
        attach(s,job["run_id"])
        actor = Actor(**job["actor"])
        s.roots.active_turns[actor.thread] = actor.turn  # Scripted host redelivers this acknowledged turn.
        result = s.call(actor,"gx_decide_revision",{"decision":job["decision"]},job["key"])
        print(json.dumps(result),flush=True)
        os.kill(os.getpid(),signal.SIGKILL)


class PortableStages(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.release = hydro_release(self.folder)
        self.enterContext(deployment(self.release))
        self.method = "discharge-marginal@1"
        self.points = [{"step":"screen","output":"marginal.json","question":"Do the observed durations support proceeding?"},
                       {"step":"evaluate","output":"marginal.json","question":"What is supported by the independent record?"}]

    def start(self, case, minutes):
        materials=[]
        for role, duration, volume in (("development",minutes,11),("holdout",62,87)):
            path = self.folder / (case+"-"+role+".json")
            record(path,duration,volume)
            if case=="short" and role=="holdout":
                path.write_bytes((self.folder/"source-holdout.json").read_bytes())
            materials.append({"role":role,"path":str(path)})
        budget={"job_submissions":8,"cpu_seconds":480,"api_calls":20,"tokens":40000,"worker_tasks":20,"wall_hours":1}
        cfg=self.folder/(case+".json")
        cfg.write_text(json.dumps({"project":"instrument-observations","capabilities":[self.method],
            "plugins":["org.gx.hydro-marginal@1.0.0"],"research_modules":[], "input_files":materials,
            "turn_token_budget":1000,"run_budget":budget,"workspace_budget":{k:v*4 for k,v in budget.items()}}))
        result=subprocess.run([sys.executable,"-B",str(self.release/"bin/gx-p0"),"research","--state",
            str(self.folder/"state"),"--goal","Do the recorded sampling durations permit independent evaluation?",
            "--run-key",case,"--goal-config",str(cfg),"--research-effort","deep","--timeout","0.00001"],
            cwd=self.folder,text=True,capture_output=True,env={**os.environ,"GX_CODEX_OFFLINE":"1"})
        self.assertEqual(result.returncode,75,result.stderr+result.stdout)
        return json.loads(result.stdout)["run_id"]

    def bind(self, scenario, rid):
        macro_fixture.NativeMacro._bind(self, scenario, rid)

    def save_arguments(self, s, ids):
        return {"recipe_id":"sampling-review","version":"1","title":"Evaluate independent sampling records",
            "nodes":[{"slot":k,"node_id":v} for k,v in ids.items()],
            "inputs":[{"name":k,"artifact_ref":v} for k,v in zip(("development","holdout"),s.materials)],
            "units":"minutes","id_alignment":"station pair and period","missing_values":"unresolved",
            "decision_points":self.points,"evaluation":{"step":"evaluate","holdout_input":"holdout"}}

    def test_two_new_materials_filter_decisions_no_old_approval(self):
        rid=self.start("source",60)
        with Scenario(self.folder,reopen=True) as s:
            self.bind(s,rid)
            source,holdout=s.materials
            def op(slot,material):
                return {"op":"add_node","node":"local:"+slot,"branch":"local:station","capability":self.method,
                    "question":"What is the observed duration?","depends_on":[],"inputs":[material],"parameters":{}}
            gate={"producer_node":"local:screen","producer_version":1,"source_ref":source,
                "pointer":"/left_minutes","equals":60,"require_lead_review":True}
            evaluation=dict(op("evaluate",holdout),depends_on=["local:screen"],evidence_gate=gate)
            def graph(*ops):
                return draft(dict(branch("station",4),budget_limits={"job_submissions":4,"cpu_seconds":240}),*ops,
                    watches=[{"event":"job_settled","subject_ref":ops[0]["node"]}])
            opening=s.call(s.lead,"gx_decide_revision",{"decision":graph(op("screen",source),evaluation)},"graph")
            ids={slot:opening["aliases"]["local:"+slot] for slot in ("screen","evaluate")}
            read=s.settle(ids["screen"])["result_ref"]
            decision=draft(evidence=[read],watches=[{"event":"job_settled","subject_ref":ids["evaluate"]}])
            decision["approve_nodes"]=[ids["evaluate"]]
            s.call(s.next_actor("run"),"gx_decide_revision",{"decision":decision},"approve")
            s.settle(ids["evaluate"])
            args=self.save_arguments(s,ids)
            lead = s.next_actor("run")
            saved=s.call(lead,"gx_save_recipe",args,"save")
            child = {"recipe":saved["reference"], "digest":saved["digest"],
                     "steps":{"screen":"screen","evaluate":"evaluate"},
                     "inputs":{"development":"development","holdout":"holdout"}}
            linked = dict(args, recipe_id="linked-evaluation", children=[child])
            del linked["decision_points"], linked["evaluation"]
            with self.assertRaisesRegex(SchemaInvalid,"pinned digest"):
                s.call(lead,"gx_save_recipe",dict(linked,children=[dict(child,digest="f"*64)]),"wrong-digest")
            with self.assertRaisesRegex(SchemaInvalid,"acyclic"):
                s.call(lead,"gx_save_recipe",dict(linked,recipe_id=args["recipe_id"]),"cycle")
            with self.assertRaisesRegex(SchemaInvalid,"pinned procedure"):
                s.call(lead,"gx_save_recipe",dict(linked,children=[dict(child,steps={"screen":"evaluate","evaluate":"screen"})]),"bad-map")
            # The wrapper inherits both required decisions and the exclusive evaluation.
            saved=s.call(lead,"gx_save_recipe",linked,"compose-pinned-child")
            self.assertEqual(len(saved["decision_points"]),2)
            definition=json.loads(s.conn.execute("SELECT definition_json FROM v6_saved_recipes WHERE reference=?",(saved["reference"],)).fetchone()[0])
            for ephemeral in [*s.materials,*ids.values(),read]:self.assertNotIn(ephemeral,json.dumps(definition))
            portable_gate=next(st for st in definition["steps"] if st["slot"]=="evaluate")["evidence_gate"]
            self.assertEqual((portable_gate["producer_step"],portable_gate["output_name"],portable_gate["source_input"]),
                             ("screen","marginal.json","development"))
        traces=[]
        for case,minutes in (("other",60),("short",45)):
            target=self.start(case,minutes)
            with Scenario(self.folder,reopen=True) as s:
                self.bind(s,target)
                replay={"recipe":saved["reference"],"digest":saved["digest"],"prefix":"fresh","branch":"local:station",
                    "inputs":dict(zip(("development","holdout"),s.materials))}
                with self.assertRaisesRegex(SchemaInvalid,"distinct bytes"):
                    s.call(s.lead,"gx_instantiate_recipe",dict(replay,inputs={"development":s.materials[0],"holdout":s.materials[0]}),"contaminated")
                preview=s.call(s.lead,"gx_instantiate_recipe",replay,"expand")
                expected_history = "same_as_source_evaluation" if case=="short" else "differs_from_source_evaluation"
                self.assertEqual(preview["evaluation"]["material_history"], expected_history)
                self.assertFalse(s.rows("SELECT job_id FROM jobs WHERE run_id=?",target))
                by_slot={item["recipe_decision"]["step"]:item for item in preview["ops"]}
                self.assertEqual(by_slot["evaluate"]["evidence_gate"]["source_role"], "record")
                changed=deepcopy(by_slot["evaluate"])
                changed["evidence_gate"]["equals"]=True
                with self.assertRaisesRegex(SchemaInvalid,"typed value"):
                    s.call(s.lead,"gx_decide_revision",{"decision":graph(by_slot["screen"],changed)},"changed-value")
                accepted=s.call(s.lead,"gx_decide_revision",{"decision":graph(by_slot["screen"],by_slot["evaluate"])},"adopt")
                ids={slot:accepted["aliases"]["local:fresh-"+slot] for slot in by_slot}
                observation=s.settle(ids["screen"])["result_ref"]
                self.assertNotEqual(observation,read)
                lead=s.next_actor("run")
                points=s.brief(lead)["recipe_decisions"]
                self.assertTrue(any(q["evidence"]["artifact_ref"]==observation and q["step"]=="screen" for q in points if q.get("evidence")))
                decision=draft(evidence=[observation],watches=[{"event":"job_settled","subject_ref":ids["evaluate"]}])
                if minutes==60:
                    decision["approve_nodes"]=[ids["evaluate"]]
                before = s.rows("SELECT job_id FROM jobs WHERE run_id=?",target)
                self.assertEqual(len(before), 1)
                key = "use-this-observation"
                if minutes==60:
                    path=self.folder/"controller-input.json"
                    path.write_text(json.dumps({"work":str(self.folder),"run_id":target,"actor":asdict(lead),"decision":decision,"key":key}))
                    owner=s
                    owner.stack.close()
                    killed=subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),str(path)],cwd=self.folder,
                                          text=True,capture_output=True,timeout=30)
                    self.assertEqual(killed.returncode,-signal.SIGKILL,killed.stderr)
                    first=json.loads(killed.stdout)
                    s=owner.stack.enter_context(Scenario(self.folder,reopen=True))
                    attach(s,target)
                    self.assertEqual(s.rows("SELECT job_id FROM jobs WHERE run_id=?",target),before)
                    persisted=s.rows("SELECT receipt_json FROM v6_decisions WHERE run_id=? AND decision_ref=?",target,first["decision_ref"])
                    self.assertEqual(json.loads(persisted[0]["receipt_json"])["decision_id"],first["decision_id"])
                    resumed=s.call(lead,"gx_decide_revision",{"decision":decision},key)
                    self.assertEqual(first["decision_id"],resumed["decision_id"])
                else:
                    first=s.call(lead,"gx_decide_revision",{"decision":decision},key)
                if minutes==60:
                    progress=s.settle(ids["evaluate"])
                    self.assertIsNotNone(progress["job_id"])
                    last=s.next_actor("run")
                    self.assertTrue(any(q.get("evidence",{}).get("artifact_ref")==progress["result_ref"]
                        for q in s.brief(last)["recipe_decisions"] if q.get("evidence")))
                    final=s.call(last,"gx_decide_revision",{"decision":draft(evidence=[progress["result_ref"]])},"interpret-independent")
                    self.assertNotEqual(first["decision_id"],final["decision_id"])
                else:
                    s.advance()
                    self.assertEqual(s.rows("SELECT status,job_id FROM v6_nodes WHERE node_id=?",ids["evaluate"]),
                                     [{"status":"BLOCKED","job_id":None}])
                traces.append({"run_id":target,"condition_minutes":minutes,"jobs":s.rows("SELECT job_id,status FROM jobs WHERE run_id=?",target),
                    "recovery_after_first_decision":"SIGKILL/reopen/same-receipt" if minutes==60 else None,
                    "evaluation_material_history":expected_history,"observed":observation,"inputs":s.materials,"first_decision":first["decision_id"],"second_decision":final["decision_id"] if minutes==60 else None})
        if path:=os.getenv("GX_PORTABLE_STAGES_PROOF"):
            Path(path).write_text(json.dumps({"source_run":rid,"recipe":saved["reference"],"digest":saved["digest"],"target_runs":traces},indent=2)+"\n")


if __name__ == "__main__":
    killed_controller(sys.argv[1])
