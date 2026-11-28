"""Bounded native Lead delivery with simultaneous hydrometry endpoints, instructions and messages."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[3]
FIELD = ROOT / "acceptance/harness_refactor/field/hydro"
METHOD = "discharge-marginal@1"


def study(folder, release):
    # Bootstrap the isolated product before loading shared acceptance transport helpers.
    sys.path.insert(0, str(release / "gx-h"))
    from gx_core import config
    from gx_core.domain import ids
    from gx_core.p0 import research_joins, v6_jobs, v6_native, v623_endpoints
    from acceptance.v622.test_close import SurveyedStudy, actual_packet
    assert config.HARNESS_ROOT == release / "gx-h"
    inputs = [{"role": "record", "path": str(FIELD / name / "discharge.json")} for name in ("a", "b")]
    grant = {"capabilities": [METHOD], "plugins": ["org.gx.hydro-marginal@1.0.0"], "research_effort": "deep",
             "module_policy": {"brief_bytes": 4096}, "research_modules": ["ad_hoc@1"], "input_files": inputs}
    path = folder / "grant.json"
    path.write_text(json.dumps(grant))
    outcome = subprocess.run([sys.executable, str(release/"bin/gx-p0"), "research", "--state", str(folder/"state"),
        "--run-key", "combined-brief", "--goal", "Check comparability of the archived station readings.",
        "--goal-config", str(path), "--timeout", "0.001"], capture_output=True, text=True,
        env={**os.environ, "GX_CODEX_OFFLINE": "1"})
    assert outcome.returncode == 75, outcome.stderr
    with SurveyedStudy(folder, reopen=True) as s:
        lead = s.next_actor("run")
        a, b = s.materials
        branch = {"op":"open_branch", "branch":"local:volumes", "hypothesis":"The measured support is equal.",
                  "alternative":"The records have uneven support.", "method_family":"archive summaries",
                  "discriminating_observation":"Read both station windows.", "stop_condition":"Compare durations.",
                  "budget_limits":{"job_submissions":8, "cpu_seconds":400}}
        measure = {"op":"add_node", "node":"local:original", "branch":"local:volumes",
                   "capability":METHOD, "inputs":[a], "question":"What duration is recorded?"}
        register = {"branch":"local:volumes", "material_role":"record",
                    "method":{"capability":METHOD, "parameters":{}, "output_name":"marginal.json",
                              "metric_id":"right_minutes", "unit":"min"}}
        original = s.call(lead, v6_native.DECIDE, {"decision":{"rationale":"Compare the recorded right-bank duration.",
            "ops":[branch, measure], "comparison_plan":register}}, "measure-first")
        first = original["aliases"]["local:original"]
        branch_id = original["aliases"]["local:volumes"]
        s.settle(first)
        lead = s.next_actor("run")
        recipe = {"recipe_id":"marginal-duration", "version":"1", "title":"Archived station windows",
                  "id_alignment":"station and period", "units":"minutes and litres", "missing_values":"label unknown",
                  "nodes":[{"slot":"measure","node_id":first}], "inputs":[{"name":"record","artifact_ref":a}],
                  "decision_point":{"step":"measure","output":"marginal.json","question":"Compare the time windows.",
                                    "actions":["retain","investigate"],"stop_condition":"Duration is known."}}
        card = s.call(lead,"gx_save_recipe",recipe,"save")
        # This task's tool menu was fixed before the first recipe existed.
        s.call(lead,v6_native.DECIDE,{"decision":{"rationale":"Apply the newly saved method.",
                "ops":[{"op":"open_intent","intent":"local:compare","question":"How do the two records compare?"}]}},
               "compare-archives")
        lead = s.next_actor("run")
        ops = []
        for prefix, material in (("east",b),("west",a)):
            args = {"recipe":card["reference"],"digest":card["digest"],"prefix":prefix,"branch":branch_id,
                    "inputs":{"record":material}}
            ops += s.call(lead,"gx_instantiate_recipe",args,"instantiate-"+prefix)["ops"]
        members = []
        for name in ("clock","recorder"):
            request = {"module_id":"ad_hoc","module_version":"1","focus":"station clock records",
                "round":1,"operation_key":name,"input_refs":[a],"capabilities":[], "method_choices":[],
                "expected_delivery":"Describe archive coverage and its limitations.",
                "sub_budget":{"worker_tasks":2,"api_calls":2}}
            receipt = s.call(lead,"gx_open_module",request,"open-"+name)
            members.append((receipt["module_instance_id"], s.bind(receipt["task_id"])))
        mid, left = members[0]
        _, right = members[1]
        topic = "gauge chronology"
        def hint(actor, action, key, **fields):
            return s.call(actor, "gx_send_research_hint", {"topic":topic,"action":action,
                          "operation_key":key,**fields},key)
        source = hint(left,"publish","east-root",interpretation="The station clock is local.",source_refs=[a])
        hint(right,"subscribe","subscribe")
        other = hint(right,"publish","west-root",question="Which observations overlap?")
        wording = "两份档案的时间支持范围必须分别核对，才能解释流量差。" * 12
        combined = hint(right,"publish","synthesis",interpretation=wording,
                        parent_hint_ids=[source["hint_id"],other["hint_id"]])
        keys = [f"timing-{i}" for i in range(12)]
        for key in keys:
            s.call(lead,"gx_watch_join",{"key":key,"mode":"all",
                "question":"核對這一路的原始時段記錄，交接後仍需保留原件以供複查。" * 5,
                "ports":[{"slot":"clock","alternatives":[{"module_instance_id":mid,"output_name":"report"}]}]},key)
        update = s.call(lead,v6_native.DECIDE,{"decision":{"ops":ops, "rationale":"Read both station windows."}},
                        "read-both")
        for index, (_, member) in enumerate(members):
            s.call(member,"gx_read_artifact",{"ref":a},"read")
            s.report(member,a,key="report",text="The archived station date was inspected.")
        nodes = [update["aliases"][op["node"]] for op in ops if op["op"]=="add_node"]
        limit = time.monotonic() + 25
        while time.monotonic() < limit:
            v6_jobs.advance(s.conn,s.run_id)
            done = s.rows("SELECT node_id,result_ref,status,job_id FROM v6_nodes WHERE run_id=?",s.run_id)
            if all(row["status"] in v6_jobs.NODE_TERMINAL for row in done):
                break
            time.sleep(.03)
        assert all(n["status"] in ("SUCCEEDED","REUSED") for n in done), done
        research_joins.tick(s.conn,s.run_id)
        lead = s.next_actor("run")
        wire = actual_packet(s,lead)["packet"]
        frozen = s.call(lead,"gx_read_artifact",{"ref":wire["full_brief_ref"]},"read-full")
        page = s.call(lead,"gx_task_context",{"research_topic":topic},"topic-originals")
        live_message = next(x for x in page["research_topic_findings"] if x["hint_id"] == combined["hint_id"])
        native_names = lambda brief: {tool["name"] for tool in v6_native.tool_specs(
            [], brief, conn=s.conn, run_id=s.run_id)}
        payload_bytes = len((v6_native.PREFIX + ids.canonical_json(wire)).encode("utf-8"))
        assert len(frozen["research_joins"]) == len(keys) and all(j["state"]=="SATISFIED" for j in frozen["research_joins"])
        assert {p["node_id"] for p in frozen["recipe_decisions"]} == set(nodes)
        message = next(x for x in frozen["research_topic_findings"] if x["hint_id"]==combined["hint_id"])
        assert live_message == message
        assert message["interpretation"] == wording
        assert set(message["origin_hint_ids"]) == {source["hint_id"],other["hint_id"]}
        assert message["originals"][0]["artifact_ref"] == a
        originals = {row["result_ref"] for row in done if row["node_id"] in nodes}
        expected = {ref:next(row for row in v623_endpoints.page(s.conn,s.run_id,ref,3)["endpoint_rows"]
                             if row["id"]=="right_minutes") for ref in originals}
        observed = {o["artifact_ref"]:o.get("endpoint_rows",[]) for o in wire["observations"] if o.get("endpoint_total")}
        result = dict(run=s.run_id, producer_jobs=done, packet_bytes=payload_bytes, full_ref=wire["full_brief_ref"],
                      tool_delta=sorted(native_names(frozen) ^ native_names(wire)),
                      counts=[len(frozen[k]) for k in ("research_joins","recipe_decisions","research_topic_findings")],
                      actual_rows=observed,expected_primary=expected,topic_message=message["hint_id"],
                      origin_hint_ids=message["origin_hint_ids"],verified_raw_wording=True)
        print(json.dumps(result,ensure_ascii=False,indent=2),flush=True)
        assert native_names(frozen) == native_names(wire), "Paging changed the eligible Lead tool names"
        assert all(wire[k+"_available"] == len(frozen[k]) for k in ("research_joins", "recipe_decisions", "research_topic_findings"))
        assert payload_bytes <= 4096, f"Native delivery exceeded 4096 bytes ({payload_bytes})"
        assert all(observed.get(ref, [])[:1] == [row] for ref,row in expected.items()), "Registered rows missing from native first page"


class CompactLead(unittest.TestCase):
    def test_truncated_state_flags_follow_the_pinned_run_scope(self):
        from gx_core.p0 import harness_policy, v621_policy, v6_native
        def names(**state):
            brief = {"scope":"run", "evidence_policy":v621_policy.VERSION, **state}
            return {tool["name"] for tool in v6_native.tool_specs([],brief)}
        flags = {"recipe_source_available":True, "open_branch_available":True}
        actions = {"gx_save_recipe", "gx_delegate_branch"}
        self.assertFalse(names(**flags) & actions)
        self.assertFalse(names(harness_policy=harness_policy.VERSION) & actions)
        self.assertEqual(names(harness_policy=harness_policy.VERSION, **flags) & actions, actions)
        self.assertEqual(names(harness_policy=harness_policy.VERSION, branch_delegation=False, **flags) & actions,
                         {"gx_save_recipe"})
        self.assertEqual(names(nodes=[{"result_ref":"output"}], branches=[{"status":"OPEN"}]) & actions, actions)

    def test_simultaneous_inputs_prioritize_named_rows_and_retain_lineage(self):
        from acceptance.harness_refactor.field.run import hydro_release
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            release = hydro_release(base)
            run = subprocess.run([sys.executable,"-B",str(Path(__file__).resolve()),str(base),str(release)],
                                 capture_output=True,text=True,timeout=75)
            if file := os.environ.get("GX_COMPACT_RECEIPT"):
                Path(file).write_text(run.stdout)
            self.assertEqual(run.returncode,0,run.stderr + run.stdout)


if __name__ == "__main__":
    study(Path(sys.argv[1]),Path(sys.argv[2]))
