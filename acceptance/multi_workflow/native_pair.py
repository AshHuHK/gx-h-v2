"""Exercise two investigation roots on the official host and a scripted localhost peer."""
import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sys
from threading import Barrier, Event
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v622.public import batch, funding, offline
from acceptance.v62c import pilot_peer as science

ROUTES = ("Measure ordered backbone coverage.", "Measure shared backbone coverage.")
FOLLOWUP = "Verify the second deposition with the selection warranted by the first report."
GOAL = "Compare coverage of the archived coordinates under two atom selections, and check the second deposition using the first route's evidence."


def material(brief, name):
    return next(m["artifact_ref"] for m in brief["materials"] if m["name"] == name)


def commission(brief, i):
    return "gx_open_module", {
        "module_id": "investigation", "module_version": "1", "round": 1,
        "focus": ROUTES[i], "expected_delivery": "Measured coverage, with its source and limit.",
        "capabilities": [science.METHOD], "input_refs": [material(brief, ("2RE9.cif", "2O0O.cif")[i])],
        "sub_budget": {"job_submissions": 2, "api_calls": 6, "cpu_seconds": 90, "tokens": 250000, "worker_tasks": 6},
        "operation_key": ("ordered-measurement", "shared-measurement")[i],
        "wake_on": ["counterevidence"] if i == 0 else [],
    }


class Pair:
    def __init__(self):
        self.active = Barrier(2, timeout=30)
        self.followup_started = Event()

    def module(self, brief, replies):
        route = ROUTES.index(brief["module"]["focus"])
        ref = brief["materials"][0]["artifact_ref"]
        if not brief["nodes"]:
            if ("gx_query_catalog", None) not in replies:
                self.active.wait()
                return "gx_query_catalog", {"kind": "method", "reference": science.METHOD}
            if ("gx_read_artifact", ref) not in replies:
                return science.op_read(ref)
            mask = ("ordered_backbone", "common_backbone")[route]
            op = science.measure(brief["module"]["branch_id"], ref, "coverage", mask)
            return science.decision([op], "Commission the selected measurement on the assigned deposition.",
                                    event="job_settled", subject=op["node"])
        node, = brief["nodes"]
        result = node.get("result_ref")
        if not result:
            return science.decision([], "Wait for the admitted measurement.", event="job_settled", subject=node["node_id"])
        if ("gx_read_artifact", result) not in replies:
            return science.op_read(result)
        reading = replies["gx_read_artifact", result]
        partial = reading["selected_residues"] < reading["observed_union_residues"]
        if route == 0:
            assert partial, "The published calibration control should show incomplete ordered coverage"
        elif not self.followup_started.wait(35):
            raise RuntimeError("Lead did not act on the early counterevidence")
        return "gx_submit_module_report", {
            "operation_key": "publish-coverage", "status": "COMPLETE",
            "findings": [{"text": "Coverage under the assigned atom selection is {{metric:coverage}}.",
                          "kind": "counterevidence" if route == 0 and partial else "observation", "source_refs": [result]}],
            "artifact_refs": [result], "gaps": [], "next_steps": [],
        }

    def lead(self, brief, replies, opened):
        reports = brief.get("module_reports", [])
        anchor = next((b for b in brief.get("branches", []) if b["spec"].get("method_family") == "two-module coverage comparison"), None)
        if not anchor:
            if ("gx_query_catalog", None) not in replies:
                return "gx_query_catalog", {"kind": "module", "reference": "investigation@1"}
            last = replies.get(("gx_open_module", None))
            if not last:
                return commission(brief, 0)
            if opened == 1:
                return commission(brief, 1)
            return science.decision([{**science.branch(), "method_family":"two-module coverage comparison"}], "Hold the reference comparison until coverage evidence arrives.")
        followup = next((n for n in brief["nodes"] if n.get("branch_id") == anchor["branch_id"]), None)
        if not followup and reports:
            first = next(r for r in reports if r["counterevidence"])
            metric, = [m for m in first["verified_metrics"] if m["metric_id"] == "coverage"]
            assert 0 < metric["numerator"] < metric["denominator"]
            op = science.measure(anchor["branch_id"], material(brief, "2O0O.cif"), "follow-up", "common_ca")
            op["question"] = FOLLOWUP
            name, args = science.decision([op], "Incomplete ordered coverage warrants a shared C-alpha check on the second deposition.",
                                              [first["report_ref"]], event="job_settled", subject=op["node"])
            args["decision"]["metric_refs"] = [metric["metric_ref"]]
            return name, args
        if followup:
            self.followup_started.set()
            result = followup.get("result_ref")
            if result and len(reports) == 2:
                if ("gx_read_artifact", result) not in replies:
                    return "gx_read_artifact", {"ref": result, "endpoint_offset": 0}
                read = replies["gx_read_artifact", result]
                refs = [result, *[r["report_ref"] for r in reports]]
                why = "The second deposition was checked under shared C-alpha selection in response to the published coverage loss."
                name, args = science.decision([{"op":"close_run","reason":why,"claim_refs":[],
                                          "unresolved":["The conclusion concerns the selected archived deposits."]}], why, refs)
                args["decision"]["metric_refs"] = [m["metric_ref"] for source in [*reports,read] for m in source["verified_metrics"]]
                return name, args
        return science.decision([], "Wait for the commissioned work and the remaining report.")

    def action(self, original, *args, **kwargs):
        body = args[2]
        opened = sum(i.get("name") == "gx_open_module" for i in body["input"])
        kwargs["step"] = lambda b, replies: self.module(b,replies) if b.get("module") else self.lead(b,replies,opened)
        return original(*args, **kwargs)


def verify(work, rid, reopened):
    exported = json.loads(Path(reopened["result"]).read_text())
    with closing(batch.old.db_open(work)) as db:
        def rows(query, params=()):
            return [dict(r) for r in db.execute(query, params)]
        modules = rows("SELECT module_instance_id,scope,branch_id,round,status,parent_task_id FROM v62_modules WHERE run_id=? ORDER BY rowid", (rid,))
        assert len(modules) == 2 and {(m["round"], m["parent_task_id"]) for m in modules} == {(1, modules[0]["parent_task_id"])}
        assert all(m["status"] == "COMPLETE" for m in modules)
        turns = rows("""SELECT t.scope,r.thread_id,u.turn_id FROM p0_roots r JOIN v6_decision_tasks t USING(task_id)
                        JOIN p0_root_turn_usage u USING(thread_id) WHERE r.run_id=? AND t.scope LIKE 'module:%'""", (rid,))
        idx = {}
        for path in (work / "codex/sessions").rglob("rollout-*.jsonl"):
            with path.open() as stream:
                for line in stream:
                    event = json.loads(line)
                    kind = event.get("payload", {}).get("type")
                    if event["type"] == "event_msg" and kind in ("task_started", "task_complete", "turn_aborted"):
                        key = (path.stem[-36:], event["payload"]["turn_id"])
                        idx.setdefault(key, {})["start" if kind == "task_started" else "end"] = event["timestamp"]
        for turn in turns:
            turn.update(idx[turn["thread_id"],turn["turn_id"]])
        overlaps = [(datetime.fromisoformat(min(a["end"],b["end"]))-datetime.fromisoformat(max(a["start"],b["start"]))).total_seconds()
                    for a in turns for b in turns if a["scope"] < b["scope"]]
        assert max(overlaps) > 0
        nodes = rows("SELECT branch_id,job_id,result_ref,status,spec_json FROM v6_nodes WHERE run_id=? ORDER BY rowid", (rid,))
        for n in nodes:
            n["question"] = json.loads(n.pop("spec_json"))["question"]
        assert len(nodes) == 3 and len({n["job_id"] for n in nodes}) == 3
        assert all(n["status"] == "SUCCEEDED" and n["result_ref"] for n in nodes)
        reports = rows("SELECT module_instance_id,report_ref FROM v62_module_reports WHERE run_id=? ORDER BY rowid", (rid,))
        wakes = rows("SELECT event_id,wake_key FROM v62_module_wakes WHERE run_id=? ORDER BY event_id", (rid,))
        decisions = rows("SELECT task_id,request_json,receipt_json FROM v6_decisions WHERE run_id=?", (rid,))
        adopted = [(json.loads(d["request_json"])["decision"],json.loads(d["receipt_json"])) for d in decisions]
        tool, receipt = next((b,r) for b,r in adopted if any(op.get("question") == FOLLOWUP for op in b["ops"]))
        early_wake, = [w for w in wakes if w["wake_key"] == f"early:{reports[0]['module_instance_id']}"]
        round_wake, = [w for w in wakes if w["wake_key"] == "round:1"]
        assert reports[0]["report_ref"] in tool["evidence_refs"]
        assert early_wake["event_id"] < receipt["event_id"] < round_wake["event_id"]
        assert any(op.get("op") == "close_run" for b, _ in adopted for op in b["ops"])
        followup, = [n for n in nodes if n["question"] == FOLLOWUP]
        handoff, = [item for item in exported["routes"]["handoffs"]
                    if item.get("decision_event") == receipt["event_id"] and item["status"] == "ENACTED"]
        assert handoff["report_ref"] == reports[0]["report_ref"]
        assert any(e["kind"] == "MEASUREMENT_COMPLETED" and e["job_id"] == followup["job_id"]
                   for e in handoff["effects"])
        assert not handoff["real_model_handoff"] and not exported["routes"]["real_turn_overlaps"]
        endpoints = exported["final_decision"]["used_endpoints"]
        assert {r["source_ref"] for r in endpoints} == {followup["result_ref"], *(r["report_ref"] for r in reports)}
        return {"run_id":rid,"paid_model_calls":0,"transport":"official host and scripted localhost Responses",
                          "modules":modules,"turns":turns,"maximum_turn_overlap_seconds":max(overlaps),"nodes":nodes,
                          "reports":reports,"wake":wakes,"followup_decision_event":receipt["event_id"],
                          "closing_readings":[{"source_ref":r["source_ref"],"display":r["reading"]["display"]} for r in endpoints],
                          "reopened":reopened["invocation"]["added"]}


def run(work):
    work.mkdir(parents=True, exist_ok=False)
    source = ROOT / "examples/v62a-data-inventory/grant.json"
    grant = json.loads(source.read_text())
    for row in grant["input_files"]:
        row["path"] = str((source.parent / row["path"]).resolve())
    grant.update(capabilities=[science.METHOD], research_modules=["investigation@1"],
                 plugins=["org.gx.protein@0.3.0","org.gx.investigation@1.0.0"], max_parallel_roots=3,
                 branch_delegation=False)
    for name in ("run_budget","workspace_budget"):
        grant[name].update(tokens=1_000_000, api_calls=24, job_submissions=8, worker_tasks=28, cpu_seconds=480)
    batch.save(work/"grant.json",grant)
    fixture = Pair()
    ledger = funding.BatchFunding(work/"funding.sqlite",series="investigation-pair",slots=[work.name])
    original = offline.action
    with (work/"lock").open("w") as lock, patch.object(offline,"action",lambda *a,**kw: fixture.action(original,*a,**kw)), \
            offline.session(work,batch.old.BINARY,"standard",Event(),ledger) as (env, _, peer):
        cli = batch.native_cli(work,env,lock.fileno())
        arguments = ("--goal",GOAL,"--goal-config",work/"grant.json","--run-key","pair","--timeout",105)
        status = cli("research", *arguments, timeout=125)
        faults = [e for e in peer.rows if "error" in e]
        assert not faults and status["goal_status"] == "CLOSED", (faults,status["goal_status"],status.get("waiting_for"))
        requests = peer.request_count
        reopened = cli("research", *arguments, local=True)
        assert reopened["run_id"] == status["run_id"] and reopened["invocation"]["action"] == "export_closed"
        assert peer.request_count == requests and reopened["invocation"]["added"] == {"jobs": 0, "host_turns": 0}
        assert Path(reopened["report"]).is_file() and Path(reopened["result"]).is_file()
        receipt = verify(work, status["run_id"], reopened)
        receipt["scripted_requests"] = requests
        batch.save(work/"native-pair.json",receipt)
        print(json.dumps(receipt, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,required=True)
    run(parser.parse_args().work.resolve())
