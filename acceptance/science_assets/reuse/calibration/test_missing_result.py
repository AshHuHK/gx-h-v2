"""An absent published calibration cannot be counted as usable after cold recovery."""
from contextlib import closing
import unittest

from acceptance.science_plugin_blueprint import test_recipe_binding as provider
from gx_core.artifacts import ArtifactService
from gx_core.persistence import store
from gx_core.p0 import v6_decisions, v6_goal, v6_recipes, v62_outputs
from tests.test_v6_decisions import bind_native_fixture, branch, draft


class MissingResult(unittest.TestCase):
    def test_remeasure_then_independently_repeat_with_distinct_jobs(self):
        instrument = provider.RecipeProviders()
        self.addCleanup(instrument.doCleanups)
        instrument.setUp()
        conn = instrument.conn
        graph = instrument.graph([])
        graph["ops"].append(dict(instrument.op("quality", instrument.raw), independent_repeat=True))
        source = v6_decisions.adopt(conn, **instrument.call, decision=graph)["aliases"]
        instrument.settle([source["local:split"], source["local:quality"]], conn)
        card = v6_recipes.save(conn, instrument.run, {
            "recipe_id":"sensor-comparison", "version":"1", "title":"Qualify and read an instrument",
            "units":"counts", "id_alignment":"ordered readings", "missing_values":"retain all declared observations",
            "nodes":[{"slot":slot, "node_id":source["local:"+slot]} for slot in ("quality","split")],
            "inputs":[{"name":"series","artifact_ref":instrument.raw}],
            "calibration":{"step":"quality","scope":"reference instrument"}})
        def next_goal(db, key, independent=False):
            rid = v6_goal.start(db, instrument.first, run_key=key, current=None)["run_id"]
            raw, = db.execute("SELECT json_extract(inputs_json,'$.files[0].artifact_ref') FROM runs WHERE run_id=?",
                              (rid,)).fetchone()
            request = {"recipe":card["reference"], "digest":card["digest"], "branch":"local:sample",
                       "inputs":{"series":raw}, "prefix":"new", "calibration_scope":"reference instrument"}
            if independent:
                request["independent_repeat"] = True
            return rid, v6_recipes.instantiate(db, rid, request)
        original_job, = conn.execute("SELECT job_id FROM v6_nodes WHERE node_id=?", (source["local:quality"],)).fetchone()
        _, referenced = next_goal(conn, "intact")
        self.assertEqual("referenced", referenced["calibration"]["status"])
        ref = v62_outputs.published_outputs(conn, instrument.run, original_job)[0]["artifact_ref"]
        # The path is in the temporary fixture store. The old ledger is retained verbatim.
        location = ArtifactService(conn).path_of(ref)
        moved = location.with_suffix(".offline")
        location.rename(moved)
        database = conn.execute("PRAGMA database_list").fetchone()[2]
        conn.close()
        with closing(store.open_store(database)) as reopened:
            rid, remeasure = next_goal(reopened, "missing")
            self.assertEqual("run_on_target", remeasure["calibration"]["status"])
            self.assertEqual(2, len(remeasure["ops"]))
            ids = []
            for key in ("missing", "independent"):
                if key == "independent":
                    moved.rename(location)
                    rid, expansion = next_goal(reopened, key, True)
                else:
                    expansion = remeasure
                decision = draft(dict(branch(jobs=2), budget_limits={"job_submissions":2,"cpu_seconds":60}),
                                 *expansion["ops"])
                actor = bind_native_fixture(reopened, rid, instrument.tmp)
                aliases = v6_decisions.adopt(reopened, **actor, decision=decision)["aliases"]
                instrument.run = rid
                nodes = [aliases[op["node"]] for op in expansion["ops"]]
                settled = instrument.settle(nodes, reopened)
                self.assertTrue(all(settled[node]["status"] == "SUCCEEDED" for node in nodes))
                ids.extend(reopened.execute("SELECT job_id FROM v6_nodes WHERE node_id=?", (node,)).fetchone()[0]
                           for node in nodes)
            self.assertEqual(4, len(set(ids)))
            self.assertNotIn(original_job, ids)
            self.assertTrue(ArtifactService(reopened).verify(ref)["ok"])
