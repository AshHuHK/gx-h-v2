"""Exercise the normal checkpoint/Job path for a confirmed changed objective in the same project."""
import tempfile
import unittest
from pathlib import Path

from gx_core.artifacts import ArtifactService
from gx_core.errors import DependencyUnmet
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.p0 import cli, intent, replan
from gx_core.p0.driver import context
from gx_core.persistence import store


class ChangedObjective(unittest.TestCase):
    def test_requires_matching_committed_plan_and_reenters_once(self):
        with tempfile.TemporaryDirectory() as tmp, native_state(Path(tmp)) as state:
            conn = store.open_store(state.db_path)
            self.addCleanup(conn.close)
            spec = cli.demo_spec()
            def approve(rid, message, action):
                intent.stage_input(conn,rid,message,action)
                return intent.accept_input(conn,rid,"main",message+"-turn",message)
            def bootstrap(rid):
                intent.bind_main(conn,rid,"main")
                approve(rid,"initialize",cli.initial_action(spec))
                intent.bind_dependencies(conn,rid,"workflow",rid,{"objective":"context","evaluation":"evaluation"})
            gx = context(conn)
            run = service.run_start(gx, {"objective":spec["objective"],"project_ref":"replan",
                "budget_profile":"p0","workflow":{"id":"science_grid","version":"1"},
                "inputs":{"proposal":spec["proposal"],"max_rounds":3,"p0":True},"idempotency_key":"start"},
                prepare_p0=bootstrap)["run_id"]
            def row():
                return conn.execute("SELECT * FROM runs WHERE run_id=?",(run,)).fetchone()
            self.assertFalse(replan.prepare_replan(conn,run))
            originals = [r[0] for r in conn.execute("SELECT job_id FROM jobs")]
            for job in originals:
                JobService(conn).wait(job,timeout_seconds=15)
            reactor.lazy(gx,run)
            reactor.drive_science(gx,run)
            assets = ArtifactService(conn)
            previous = store.latest_checkpoint(conn,run)
            previous_ref = store.loads(previous,"state_json")["outputs"]["proposal"]["proposal_ref"]
            cli.change_state(conn,row(),"pause")
            self.assertFalse(replan.prepare_replan(conn,run))
            result = approve(run,"objective-change",{"kind":"change","items":[{"item_id":"objective",
                "item_type":"objective","expected_version":1,"value":"Test the score at ten."}]})
            approve(run,"objective-confirm",{"kind":"confirm","change_id":result["change_id"],
                                             "preview_sha256":result["preview_sha256"]})
            with self.assertRaises(DependencyUnmet):
                replan.prepare_replan(conn,run)
            self.assertEqual(row()["status"],"PAUSED")
            plan = {"kind":"replacement_proposal","objective":{"item_id":"objective","version":2},
                    "evaluation":{"item_id":"evaluation","version":1}, "proposal":{"threshold":-0.25,"paths":[
                        {"id":"ten", "prediction":"The sampled score at ten reaches the cutoff.",
                         "grid":{"synthetic":True,"method_id":"fixture-grid","method_version":"1",
                                 "candidates":[{"candidate_id":"ten","x":10}]}}]}}
            approve(run,"replacement",{"kind":"change","items":[{"item_id":"plan","item_type":"data_source",
                        "expected_version":0,"value":plan}]})
            self.assertTrue(replan.prepare_replan(conn,run))
            self.assertFalse(replan.prepare_replan(conn,run))
            cli.change_state(conn,row(),"continue")
            self.assertEqual(conn.execute("SELECT count(*) FROM events WHERE kind='p0.objective_replanned'").fetchone()[0],1)
            ckpt = store.loads(store.latest_checkpoint(conn,run),"state_json")
            self.assertEqual(ckpt["round"],1)
            proposal = assets.read_json(ckpt["outputs"]["proposal"]["proposal_ref"])
            self.assertEqual(proposal["objective"],"Test the score at ten.")
            self.assertEqual(proposal["objective_item"],plan["objective"])
            self.assertEqual([path["id"] for path in proposal["paths"]],["ten"])
            old = assets.read_json(previous_ref)
            self.assertEqual(old["objective_item"]["version"],1)
            self.assertEqual(len(old["paths"]),2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],len(originals)+1)
            self.assertTrue(all(JobService(conn).status(job)["status"]=="SUCCEEDED" for job in originals))
            for job, in conn.execute("SELECT job_id FROM jobs"):
                JobService(conn).wait(job,timeout_seconds=15)
