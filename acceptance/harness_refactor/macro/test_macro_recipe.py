"""Exercise immutable research strategy separately from a past answer."""
import json
import os
from pathlib import Path
from unittest import TestCase

from gx_core import contracts
from gx_core.p0 import v6_recipes


class MacroWire(TestCase):
    def test_join_is_registered_and_save_can_select_real_modules(self):
        self.assertIn("gx_watch_join", contracts.native_goal_tool_names())
        schema = contracts.native_goal_tool_spec(v6_recipes.SAVE)["inputSchema"]
        self.assertIn("macro", schema["properties"])
        self.assertIn("join_key", schema["properties"]["macro"]["required"])


class NativeMacro(TestCase):
    """Two ordinary CLI Goals, the same workspace, new archives, scripted official callbacks."""

    def _start(self, root, key, reading, *, allow_analysis=True):
        import subprocess
        import sys
        from acceptance.v62a.interleavings_fixture import PRODUCT
        self.material = root / (key + ".txt")
        self.material.write_text(reading)
        budget = {"job_submissions": 2, "api_calls": 12, "tokens": 12000, "cpu_seconds": 120,
                  "gpu_jobs": 0, "gpu_minutes": 0, "worker_tasks": 12, "wall_hours": 1}
        cfg = root / (key + ".json")
        cfg.write_text(json.dumps({
            "project": "gauge-archive", "capabilities": ["artifact-analysis@1"] if allow_analysis else [],
            "research_modules": ["ad_hoc@1"],
            "plugins": [], "input_files": [{"role": "station", "path": str(self.material)}],
            "turn_token_budget": 1000, "run_budget": budget,
            "workspace_budget": {k: v * 4 for k, v in budget.items()}
        }))
        command = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "research", "--state", str(root/"state"),
                   "--run-key", key, "--goal", "What does the archived gauge record establish?",
                   "--research-effort", "deep", "--goal-config", str(cfg), "--timeout", "0.000001"]
        start = subprocess.run(command, cwd=root, capture_output=True, text=True,
                               env={**os.environ, "GX_CODEX_OFFLINE": "1"})
        self.assertEqual(start.returncode, 75, start.stderr + start.stdout)
        self.assertEqual(json.loads(start.stdout)["driver"], "time_limit")
        return json.loads(start.stdout)["run_id"]

    def _bind(self, s, rid):
        from gx_core.p0.roots import Roots
        s.run_id = rid
        client = s.roots.client
        s.roots = Roots(s.conn, run_id=rid, cwd=s.state, token_budget=10)
        s.roots.client = client
        materials = s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (rid,)).fetchone()[0]
        s.materials = [m["artifact_ref"] for m in json.loads(materials)["files"]]
        s.lead = s.next_actor("run")
        overview = {"kind":"all"}
        if "pi_method_scope" in s.brief(s.lead):
            overview["pi_method_scope"] = []
        s.call(s.lead, "gx_query_catalog", overview, "overview")

    def test_native_commission_save_and_rebind(self):
        from tempfile import TemporaryDirectory
        from acceptance.v62a.interleavings_fixture import Scenario, draft
        from gx_core.errors import ScopeDenied
        from gx_core.p0 import research_joins
        work = Path(self.enterContext(TemporaryDirectory()))
        question = "Do the station records distinguish a timing offset from a flow change?"
        run_a = self._start(work, "reach-a", "The station clock offset is 42 minutes.")
        with Scenario(work, reopen=True) as a:
            self._bind(a, run_a)
            source_input = a.materials[0]
            args = {"module_id": "ad_hoc", "module_version": "1", "focus": "Inspect the archived station log.",
                    "expected_delivery": "Document clock annotations.", "operation_key": "archive", "round": 1,
                    "input_refs": [source_input], "capabilities": ["artifact-analysis@1"], "method_choices": [],
                    "tools": ["gx_decide_revision", "gx_read_artifact", "gx_submit_module_report"],
                    "sub_budget": {"api_calls":2, "tokens":2000, "worker_tasks":2}}
            opened = a.call(a.lead, "gx_open_module", args, "commission")
            mid = opened["module_instance_id"]
            join = {"key":"align", "question":question, "mode":"all", "ports":[{"slot":"clock",
                    "alternatives":[{"module_instance_id":mid,"output_name":"report"}]}]}
            self.assertEqual(a.call(a.lead, "gx_watch_join", join, "watch")["state"], "WAITING")
            a.call(a.lead, "gx_decide_revision", {"decision":draft()}, "wait")
            worker = a.bind(opened["task_id"])
            a.call(worker, "gx_read_artifact", {"ref":source_input}, "look")
            old_report = a.report(worker, source_input, text="The station clock had a material documented offset.")["report_ref"]
            lead = a.next_actor("run")
            self.assertEqual(research_joins.lookup(a.conn, run_a, "align")["state"], "SATISFIED")
            audit = a.call(lead, "gx_open_module", dict(args, operation_key="read-again", round=2,
                focus="Audit the report against its sources.", input_refs=[old_report]), "read-again")
            audit_id = audit["module_instance_id"]
            gate = dict(join, key="both", ports=[*join["ports"], {"slot":"audit",
                "alternatives":[{"module_instance_id":audit_id,"output_name":"report"}]}])
            a.call(lead, "gx_watch_join", gate, "both")
            a.call(lead, "gx_decide_revision", {"decision":draft()}, "wait-for-audit")
            auditor = a.bind(audit["task_id"])
            a.call(auditor, "gx_read_artifact", {"ref":old_report}, "read-upstream")
            audit_a = a.report(auditor, old_report, text="The station report documents a timing issue.")["report_ref"]
            lead = a.next_actor("run")
            self.assertEqual(research_joins.lookup(a.conn,run_a,"both")["state"], "SATISFIED")
            save = {"recipe_id":"clock-study", "version":"1", "title":"Check archived gauge chronology",
                    "nodes":[], "inputs":[{"name":"log","artifact_ref":source_input}],
                    "id_alignment":"station and date", "units":"minutes", "missing_values":"unresolved",
                    "macro":{"modules":[{"slot":"clock", "module_instance_id":mid, "question":"Read the station log.",
                             "expected_delivery":"Quote the local clock annotations."},
                             {"slot":"audit", "module_instance_id":audit_id, "question":"Review the clock report.",
                              "expected_delivery":"Assess whether its source supports the recorded clock condition."}],
                             "join_key":"both"}}
            receipt = a.call(lead, "gx_save_recipe", save, "save")
            self.assertEqual(receipt, a.call(lead, "gx_save_recipe", save, "save"))
            definition, provenance = a.conn.execute("SELECT definition_json,sources_json FROM v6_saved_recipes").fetchone()
            for secret in (old_report, audit_a, audit_id, mid, run_a, source_input, "42 minutes", "material documented offset"):
                self.assertNotIn(secret, definition)
            self.assertIn(old_report, provenance)
            self.assertEqual(json.loads(definition)["schema"], "gx.research-recipe/3")
            from types import SimpleNamespace
            for finished in (worker, auditor):
                a.roots._observe(SimpleNamespace(message={"method":"turn/completed", "params":{
                    "threadId":finished.thread, "turn":{"id":finished.turn, "status":"completed"}}}))

        run_b = self._start(work, "reach-b", "Clock inspection found no offset.")
        with Scenario(work, reopen=True) as b:
            self._bind(b, run_b)
            current_input = b.materials[0]
            found = b.call(b.lead, "gx_query_catalog", {"kind":"recipe", "reference":receipt["reference"]}, "inspect")
            self.assertEqual(found["spec"]["status"], "available")
            replay = {"recipe":receipt["reference"], "digest":receipt["digest"], "prefix":"station-b",
                      "branch":"local:analysis", "inputs":{"log":current_input}}
            with self.assertRaises(ScopeDenied):
                b.call(b.lead, "gx_instantiate_recipe", {**replay,"inputs":{"log":source_input}}, "old-input")
            instance = b.call(b.lead, "gx_instantiate_recipe", replay, "rebind")
            module, = instance["module_calls"]
            self.assertEqual(module["arguments"]["input_refs"], [current_input])
            self.assertNotEqual(mid, module["module_instance_id"])
            self.assertEqual(instance["decision_point"]["question"], question)
            opened = b.call(b.lead, module["tool"], module["arguments"], "open")
            self.assertEqual(opened["module_instance_id"], module["module_instance_id"])
            self.assertEqual(instance["join_pending_modules"], ["audit"])
            self.assertNotIn("join_call", instance)
            b.call(b.lead, "gx_decide_revision", {"decision":draft()}, "wait")
            worker = b.bind(opened["task_id"])
            b.call(worker, "gx_read_artifact", {"ref":current_input}, "read-current")
            current_report = b.report(worker, current_input, text="This log records no clock offset.")["report_ref"]
            lead = b.next_actor("run")
            resumed = b.call(lead, "gx_instantiate_recipe", replay, "after-reading")
            offered = {call["slot"]:call for call in resumed["module_calls"]}
            self.assertTrue(offered["clock"]["opened"])
            audit = offered["audit"]
            self.assertEqual(audit["arguments"]["input_refs"], [current_report])
            child = b.call(lead, audit["tool"], audit["arguments"], "new-audit")
            watch = resumed["join_call"]
            self.assertEqual(b.call(lead, watch["tool"], watch["arguments"], "watch")["state"], "WAITING")
            b.call(lead, "gx_decide_revision", {"decision":draft()}, "await-audit")
            auditor = b.bind(child["task_id"])
            b.call(auditor, "gx_read_artifact", {"ref":current_report}, "audit-current")
            audit_b = b.report(auditor, current_report, text="The current report documents no clock offset.")["report_ref"]
            lead = b.next_actor("run")
            result = research_joins.lookup(b.conn, run_b, watch["arguments"]["key"])
            self.assertIn(audit_b, json.dumps(result["outcome"]))
            self.assertIn(current_report, json.dumps(result["outcome"]))
            self.assertNotIn(old_report, json.dumps(result))
            self.assertTrue(all(c["opened"] for c in b.call(lead, "gx_instantiate_recipe", replay, "resume")["module_calls"]))
            self.assertEqual(b.conn.execute("SELECT COUNT(*) FROM v62_modules WHERE run_id=?", (run_b,)).fetchone()[0], 2)
            b.call(lead, "gx_read_artifact", {"ref":current_report}, "read-report")
            route = {"op":"open_branch", "branch":"local:flow", "method_family":"archival comparison",
                     "hypothesis":"A flow change could explain the discrepancy.",
                     "alternative":"Station timing could still differ outside this window.",
                     "discriminating_observation":"Compare the overlapping calibrated gauge readings.",
                     "stop_condition":"End with a bound on overlap.",
                     "budget_limits":{"job_submissions":1,"cpu_seconds":10}}
            decision = draft(route, evidence=[current_report])
            decision["rationale"] = "This station log records no timing offset. Compare the contemporaneous flows."
            accepted = b.call(lead, "gx_decide_revision", {"decision":decision}, "follow-current-log")
            self.assertTrue(b.conn.execute("SELECT 1 FROM v6_branches WHERE run_id=? AND branch_id=?",
                                           (run_b, accepted["aliases"]["local:flow"])).fetchone())
            if target_path := os.environ.get("GX_ACCEPTANCE_MACRO_NATIVE_RECEIPT"):
                Path(target_path).write_text(json.dumps({
                    "transport":"SCRIPTED_CALLBACK", "entry":"bin/gx-p0 research", "live_model_calls":0,
                    "source":{"run_id":run_a, "material":source_input, "module":mid, "report":old_report,
                              "provenance":json.loads(provenance)},
                    "recipe":receipt, "target":{"run_id":run_b, "material":current_input, "module":module["module_instance_id"],
                    "report":current_report, "audit_report":audit_b, "audit_module":child["module_instance_id"], "join":result, "scientific_decision":decision,
                    "accepted_decision":accepted["decision_ref"], "committed_branch":accepted["aliases"]["local:flow"]}
                    }, indent=2)+"\n")

        restricted = self._start(work, "read-only", "Local notes only.", allow_analysis=False)
        with Scenario(work, reopen=True) as c:
            self._bind(c, restricted)
            found = c.call(c.lead, "gx_query_catalog", {"kind":"recipe", "reference":receipt["reference"]}, "inspect")
            self.assertEqual(found["spec"]["status"], "requires_method")
            with self.assertRaises(ScopeDenied):
                c.call(c.lead, "gx_instantiate_recipe", dict(replay,inputs={"log":c.materials[0]}), "outside-grant")
            self.assertEqual(c.conn.execute("SELECT COUNT(*) FROM v62_modules WHERE run_id=?", (restricted,)).fetchone()[0], 0)


class NamedPortMacro(TestCase):
    def test_resume_joins_the_bound_target_graph_and_never_resubmits_accepted_steps(self):
        from contextlib import closing
        from tests import test_v6_named_ports as named
        from tests.test_v6_decisions import bind_native_fixture, branch, draft
        from gx_core.p0 import research_joins, v6_decisions, v6_goal
        from gx_core.persistence import store

        from unittest.mock import patch
        from gx_core.p0 import harness_policy
        case = named.NamedOutputs()
        start = v6_goal.start
        with patch.object(v6_goal, "start", side_effect=lambda conn, spec, **kw:
                          start(conn, {**spec, "harness_policy":harness_policy.VERSION}, **kw)):
            case.setUp()
        self.addCleanup(case.doCleanups)
        db, run_a = case.conn, case.run
        receipt = v6_decisions.adopt(db, **case.call, decision=case.graph([("low", "next.json")]))
        slots = [{"slot": slot, "node_id": receipt["aliases"]["local:" + slot]} for slot in ("split", "low")]
        case.settle([s["node_id"] for s in slots], db)
        gate = {"key":"scale", "mode":"all", "ports":[{"slot":"result",
                "alternatives":[{"node_id":slots[1]["node_id"], "output_name":"mean.json"}]}],
                "question":"Are the gauge statistics consistent with the archives?"}
        self.assertEqual(research_joins.register(db, run_a, gate)["state"], "SATISFIED")
        receipt = v6_recipes.save(db, run_a, {"recipe_id":"gauge-check", "version":"1", "title":"Archived gauges",
                        "nodes":slots, "inputs":[{"name":"series", "artifact_ref":case.raw}],
                        "units":"normalized scale", "missing_values":"unresolved", "id_alignment":"site",
                        "macro":{"modules":[], "join_key":"scale"}})

        target_data = case.tmp / "reach-b.json"
        target_data.write_text('{"readings":[3,13,8],"schema":"instrument/1"}')
        target = {"harness_policy":harness_policy.VERSION, "project":"output-types", "mode":"goal", "objective":"Summarize the observed series and estimate the next reading",
                  "capabilities":[named.series.METHOD], "plugins":["org.example.instrument-series@1.0.0"],
                  "research_effort":"quick", "input_files":[{"role":"readings","path":str(target_data)}]}
        run = case.run = v6_goal.start(db, target, run_key="second-gauge", current=None)["run_id"]
        source = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()[0])["files"][0]["artifact_ref"]
        arguments = {"recipe":receipt["reference"], "digest":receipt["digest"], "prefix":"new",
                     "branch":"local:sample", "inputs":{"series":source}}
        expansion = v6_recipes.instantiate(db, run, arguments)
        self.assertEqual(expansion["join_pending_nodes"], ["low"])
        by_alias = {op["node"]:op for op in expansion["ops"]}
        opened = v6_decisions.adopt(db, **bind_native_fixture(db, run, case.tmp),
                     decision=draft({**branch(), "budget_limits":{"job_submissions":3, "cpu_seconds":60}},
                                    by_alias["local:new-split"]))
        upstream = opened["aliases"]["local:new-split"]
        case.settle([upstream], db)
        with closing(store.open_store(case.db)) as recovered:
            pending = v6_recipes.instantiate(recovered, run, arguments)
            self.assertNotIn("submission", pending)
            low, = pending["ops"]
            self.assertEqual(low["depends_on"], [upstream])
            self.assertEqual(low["input_ports"], [{"index":0, "output":"next.json"}])
            v6_goal.ensure_task(recovered, run)
            accepted = v6_decisions.adopt(recovered, **bind_native_fixture(recovered, run, case.tmp), decision=draft(low))
            real_node = accepted["aliases"]["local:new-low"]
            bound = v6_recipes.instantiate(recovered, run, arguments)
            self.assertEqual(bound["ops"], [])
            gate = bound["join_call"]["arguments"]
            self.assertEqual(gate["ports"][0]["alternatives"], [{"node_id":real_node,"output_name":"mean.json"}])
            self.assertEqual(research_joins.register(recovered, run, gate)["state"], "WAITING")
            case.settle([real_node], recovered)
            research_joins.tick(recovered, run)
            observed = research_joins.lookup(recovered, run, gate["key"])["outcome"]["ports"][0]["evidence"][0]
            self.assertEqual(observed["node_id"], real_node)
            self.assertEqual(recovered.execute("SELECT COUNT(*) FROM jobs WHERE run_id=?", (run,)).fetchone()[0], 2)
            from gx_core.artifacts import ArtifactService
            evidence = ArtifactService(recovered).read_json(observed["artifact_ref"])
            self.assertEqual(evidence["readings"], [3])
            if target_path := os.environ.get("GX_ACCEPTANCE_MACRO_CPU_RECEIPT"):
                Path(target_path).write_text(json.dumps({"transport":"SCRIPTED_CALLBACK", "real_cpu_jobs":True,
                    "source_run":run_a, "target_run":run, "target_input":source,
                    "recipe":receipt["reference"], "recipe_digest":receipt["digest"],
                    "bound_after_restart":bound["bound_steps"], "target_jobs":[dict(r) for r in recovered.execute(
                    "SELECT node_id,job_id,result_ref,status FROM v6_nodes WHERE run_id=? ORDER BY rowid",(run,))],
                    "outcome":research_joins.lookup(recovered,run,gate["key"]), "counted_readings":evidence
                    }, indent=2)+"\n")

            # Isolate the recovery lookup between a formal Goal version change and the next graph.
            with store.transaction(recovered, transaction_owner="MacroFaultTest", operation="advance_goal_version"):
                recovered.execute("UPDATE v6_goals SET goal_version=goal_version+1 WHERE run_id=?", (run,))
            revised = v6_recipes.instantiate(recovered, run, arguments)
            self.assertEqual(len(revised["ops"]), 2)
            self.assertNotIn("bound_steps", revised)
