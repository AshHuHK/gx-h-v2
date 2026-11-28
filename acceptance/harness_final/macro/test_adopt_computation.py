"""A real target CPU node commits the dependency without a separate adopt or watch call."""
import json
import os
from pathlib import Path
import unittest

from acceptance.harness_final.macro import test_portable_stages as fixtures
from acceptance.v62a.interleavings_fixture import Scenario, branch, draft
from gx_core.errors import SchemaInvalid
from gx_core.p0 import research_joins


class AdoptedComputation(unittest.TestCase):
    def test_first_graph_adopts_then_delivers_its_own_named_output(self):
        setup = fixtures.PortableStages()
        setup.setUp()
        self.addCleanup(setup.doCleanups)
        original = setup.start("performed", 62)
        with Scenario(setup.folder, reopen=True) as s:
            setup.bind(s, original)
            open_branch = dict(branch("station"), budget_limits={"job_submissions":1, "cpu_seconds":60})
            step = {"op":"add_node","node":"local:survey","branch":"local:station","capability":setup.method,
                    "depends_on":[],"inputs":[s.materials[0]],"parameters":{},"question":"What was the monitoring duration?"}
            started = s.call(s.lead, "gx_decide_revision", {"decision":draft(open_branch, step,
                      watches=[{"event":"job_settled","subject_ref":"local:survey"}])}, "source-measure")
            node = started["aliases"]["local:survey"]
            old = s.settle(node)["result_ref"]
            lead = s.next_actor("run")
            s.call(lead, "gx_watch_join", {"key":"raw","question":"What can the present monitoring cover?",
                   "mode":"all","ports":[{"slot":"observation","alternatives":[{"node_id":node,
                   "output_name":"marginal.json"}]}]}, "watch")
            s.call(lead,"gx_decide_revision",{"decision":draft(evidence=[old])},"await-source-join")
            save = {"recipe_id":"survey","version":"1","title":"Monitor a new station",
                    "nodes":[{"slot":"sample","node_id":node}],
                    "inputs":[{"name":"archive","artifact_ref":s.materials[0]}],
                    "id_alignment":"station","units":"minutes","missing_values":"unknown",
                    "macro":{"modules":[],"join_key":"raw"}}
            saved = s.call(s.next_actor("run"), "gx_save_recipe",save,"save")
        target = setup.start("field",45)
        with Scenario(setup.folder, reopen=True) as s:
            setup.bind(s, target)
            request = {"recipe":saved["reference"],"digest":saved["digest"],"prefix":"survey",
                       "branch":"local:station","inputs":{"archive":s.materials[0]}}
            expansion = s.call(s.lead,"gx_instantiate_recipe",request,"preview")
            self.assertEqual(research_joins.view(s.conn,target),[])
            op, = expansion["ops"]
            with self.assertRaisesRegex(SchemaInvalid, "differs from the proposed recipe stage"):
                s.call(s.lead, "gx_decide_revision", {"decision":draft(open_branch,dict(op,inputs=[s.materials[1]]))},
                       "changed-input")
            self.assertEqual(research_joins.view(s.conn,target),[])
            op["question"] = "How long was this new field station monitored?"
            decision = {"decision":draft(open_branch,op,watches=[{"event":"job_settled","subject_ref":op["node"]}])}
            accepted = s.call(s.lead,"gx_decide_revision",decision,"accept")
            watch, = research_joins.view(s.conn,target)
            self.assertEqual(watch["state"],"WAITING")
            self.assertEqual(watch["instance"]["branch_id"],accepted["aliases"]["local:station"])
            current_node = accepted["aliases"][op["node"]]
            delivery = s.settle(current_node)
            read = s.brief(s.next_actor("run"))["research_joins"][0]
            proof, = read["outcome"]["ports"][0]["evidence"]
            self.assertEqual((read["state"], proof["node_id"], proof["artifact_ref"], proof["job_id"]),
                             ("SATISFIED",current_node,delivery["result_ref"],delivery["job_id"]))
            self.assertNotIn(old,json.dumps(read))
            self.assertEqual(s.rows("SELECT count(*) AS n FROM jobs WHERE run_id=?",target),[{"n":1}])
            if destination := os.getenv("GX_MACRO_CPU_PROOF"):
                Path(destination).write_text(json.dumps({"transport":"SCRIPTED_CALLBACK","actual_cpu":True,
                    "recipe":saved["reference"],"digest":saved["digest"],"source_run":original,"source_output":old,
                    "target_run":target,"input":s.materials[0],"registration":watch["registration_event"],
                    "ready":read["outcome_event"],"watch":read["key"],"delivery":proof,"target_jobs":1},indent=2)+"\n")
