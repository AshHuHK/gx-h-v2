"""Run a source-bound locator-coverage builder and reuse its product in a second ordinary research Goal."""
import argparse
from contextlib import ExitStack, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import shutil
from unittest.mock import patch

from acceptance.v622.test_close import SurveyedStudy, close_decision
from acceptance.v62a.interleavings_fixture import branch, draft
from gx_core import config
from gx_core.artifacts import ArtifactService
from gx_core.capabilities import manifest
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.p0 import cli, conversation, conversation_delivery, v62_outputs
from gx_core.p0.roots import Roots

HERE = Path(__file__).resolve().parent
BUILDER = "derive-record-coverage@1"
FIELDS = {"source_quote": "Match each source-qualified record ID to a nonempty PMCID in the archived provider metadata.",
          "cohort_rows":"records", "cohort_id":"record_key", "evidence_rows":"metadata",
          "evidence_id":"record_key", "evidence_value":"pmcid"}
QUESTIONS = {
    "urban": "For this urban sensor search snapshot, how many unique records have an explicit PubMed Central locator? What can we retrieve next, and what remains unknown?",
    "coastal": "In this new, separately collected coastal and fog sensor comparison, which records have locators for retrieving originals? Account for records with no locator and limit the interpretation to this supplied window.",
}


def materials(case):
    result = [{"role": r["role"], "path": str(HERE/case["study"]/r["file"]), "sha256": r["sha256"]}
              for r in case["input_files"]]
    if case["study"] == "urban":
        text = HERE/"method-source.md"
        result.append({"role":"method_source", "path":str(text), "sha256":hashlib.sha256(text.read_bytes()).hexdigest()})
    return result


def input_refs(s):
    files = json.loads(s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?",(s.run_id,)).fetchone()[0])["files"]
    return {f["role"]: f["artifact_ref"] for f in files}


def settle(s, node):
    completed = s.settle(node)
    assert completed["status"] == "SUCCEEDED", completed
    port, = v62_outputs.published_outputs(s.conn,s.run_id,completed["job_id"])
    return {"job_id":completed["job_id"],"node_id":node,
            "output_name":port["output_name"],"artifact_ref":port["artifact_ref"],"sha256":port["sha256"],
            "document":ArtifactService(s.conn).read_json(port["artifact_ref"])}


def measure(s, actor, method, case):
    inputs = input_refs(s)
    route = branch("archive-reading")
    route.update(hypothesis="Some entries expose a stable PMC locator in this response.",
                 alternative="The response supplies no PMC locators.", method_family="Bibliographic metadata coverage",
                 discriminating_observation="Count distinct source-qualified record IDs and explicit PMC IDs.",
                 stop_condition="Every record in this window is accounted for.",
                 budget_limits={"job_submissions":1,"cpu_seconds":30})
    node = s.node(actor,"measure",inputs["cohort"],branch_id=route["branch"],capability=method)
    node.update(inputs=[inputs["cohort"],inputs["evidence"]],question="How many records carry a locator?")
    action = s.call(actor,"gx_decide_revision",{"decision":draft(route,node,evidence=list(inputs.values()),
               watches=[{"event":"job_settled","subject_ref":"local:measure"}])},"measure-snapshot")
    output = settle(s, action["aliases"]["local:measure"])
    measured = {k:output["document"][k] for k in ("numerator","denominator")}
    assert measured == case["reference_count"], (case,measured)
    for role in ("cohort","evidence"):
        original, = [f["sha256"] for f in case["input_files"] if f["role"]==role]
        assert output["document"][role+"_sha256"] == original
    reader = s.next_actor("run")
    ref = output["artifact_ref"]
    args = {"ref":ref,"endpoint_offset":0}
    page = s.call(reader,"gx_read_artifact",args,"read-coverage")
    s.roots.delivery.observe("item/completed",{"threadId":reader.thread,"turnId":reader.turn,
       "item":{"type":"dynamicToolCall","tool":"gx_read_artifact","id":"read-coverage","arguments":args,
               "status":"completed","success":True,"contentItems":[{"type":"inputText","text":ids.canonical_json(page)}]}})
    decision = close_decision([ref])
    disposition = (f"Use the {measured['numerator']} explicit PMC locators for a later full-text request. "
                   f"For the other {measured['denominator']-measured['numerator']} records, consult identifiers "
                   "or the publisher. Retrieval and appraisal have not yet taken place in this study.")
    decision["rationale"] = disposition
    decision["metric_refs"] = [ref+"#"+page["endpoint_rows"][0]["key"]]
    decision["ops"][0]["reason"] = "All records in the specified response window have been accounted for."
    decision["ops"][0]["unresolved"] = ["Actual full-text availability and evidence quality remain unknown."]
    close = s.call(reader,"gx_decide_revision",{"decision":decision},"close-coverage")
    s.advance()
    status, = s.conn.execute("SELECT status FROM v6_goals WHERE run_id=?",(s.run_id,)).fetchone()
    assert status == "CLOSED", status
    return {"run_id":s.run_id,"question":QUESTIONS[case["study"]],"inputs":inputs,"operation":action["decision_ref"],
            "measurement":output,"independent_sql_reference":case["reference_count"],"close":close["decision_ref"],
            "disposition":disposition,"goal_status":status}


def run(work):
    first, second = json.loads((HERE/"provenance.json").read_bytes())
    state = work/"study/state"
    with ExitStack() as stack:
        # Building and installing a derived package updates this deployment registry. Keep the verified full release intact.
        release = work/"release/gx-h"
        shutil.copytree(config.HARNESS_ROOT,release,ignore=shutil.ignore_patterns("var","__pycache__",".venv","tests"))
        stack.enter_context(patch.object(config,"HARNESS_ROOT",release))
        stack.enter_context(patch.object(config,"CONFIG_DIR",release/"config"))
        stack.enter_context(patch.object(manifest,"HARNESS_ROOT",release))
        limits = {"job_submissions":8,"api_calls":10,"tokens":100000,"cpu_seconds":180,"worker_tasks":24,
                  "gpu_jobs":0,"gpu_minutes":0,"wall_hours":1}
        grant = {"project":"archived-pmc-locators","research_effort":"deep","research_modules":[],
                 "plugins":["org.gx.record-method-builder@1.0.0"],"capabilities":[BUILDER],"capability_collections":[],
                 "input_files":materials(first),"workspace_budget":limits,"run_budget":limits}
        cfg = work/"grant.json"
        cfg.write_text(json.dumps(grant))
        with patch.object(cli,"run_until_stopped",side_effect=lambda conn, _, rid, timeout: cli.snapshot(conn,rid)), redirect_stdout(io.StringIO()):
            assert cli.main(["research","--state",str(state),"--run-key","archived-locator-cohort",
                             "--goal",QUESTIONS["urban"],"--goal-config",str(cfg)])==0
        s = stack.enter_context(SurveyedStudy(state.parent,reopen=True))
        lead = s.next_actor("run")
        inputs = input_refs(s)
        route = branch("calibrate")
        route.update(budget_limits={"job_submissions":1,"cpu_seconds":30},method_family="Grouped identifier coverage",
                     discriminating_observation="Confirm behavior on missing and duplicate publication pointers.")
        node = s.node(lead,"derive",inputs["method_source"],branch_id=route["branch"],capability=BUILDER,parameters=FIELDS)
        action = s.call(lead,"gx_decide_revision",{"decision":draft(route,node,evidence=[inputs["method_source"]],
                         watches=[{"event":"job_settled","subject_ref":"local:derive"}])},"build-method")
        build = settle(s,action["aliases"]["local:derive"])
        actor = s.next_actor("run")
        try:
            s.call(actor,"gx_install_record_method",{"build_ref":inputs["cohort"],"expected_revision":0},"nonbuild")
        except SchemaInvalid as exc:
            rejected = exc.message
        else:
            raise AssertionError("A cohort cannot authorize method installation")
        install = {"build_ref":build["artifact_ref"],"expected_revision":0}
        published = s.call(actor,"gx_install_record_method",install,"install-method")
        assert published == s.call(actor,"gx_install_record_method",install,"replayed-install")
        method = published["capability_version"]
        s.call(actor,"gx_decide_revision",{"decision":draft(evidence=[build["artifact_ref"]])},"accept-build")
        parent = measure(s,s.next_actor("run"),method,first)
        options = {"research_effort":"deep","parent_method_options":[method],"input_files":materials(second)}
        mid = "coastal-locator-followup"
        conversation.send(s.conn,s.run_id,QUESTIONS["coastal"],mid,successor=options)
        frozen, = s.conn.execute("SELECT successor_json FROM p0_conversation_messages WHERE message_id=?",(mid,)).fetchone()
        child = conversation_delivery.start_successor(s.conn,state,s.run_id,mid,QUESTIONS["coastal"],
                                                       json.loads(frozen),native_owner=True)
        s.run_id = child["run_id"]
        transport = s.roots.client
        s.roots = Roots(s.conn,run_id=s.run_id,cwd=state,token_budget=10)
        s.roots.client = transport
        actor = s.next_actor("run")
        candidates = s.call(actor,"gx_query_catalog",{"kind":"method","query":"record evidence coverage",
                           "required_outputs":["coverage.json"],"limit":40},"find-coverage")
        hits = [row for row in candidates["items"] if "coverage.json" in row["output_names"]
                and row["status"] in {"available","selectable"}]
        assert len(hits)==1 and hits[0]["reference"]==method, hits
        selected = hits[0]["reference"]
        card = s.call(actor,"gx_query_catalog",{"kind":"method","reference":selected},"read-method-card")
        selection = s.call(actor,"gx_select_capability",{"method":selected,"action":"enable","revision":0},"activate")
        s.call(actor,"gx_decide_revision",{"decision":draft()},"view-changed")
        successor = measure(s,s.next_actor("run"),selected,second)
        assert parent["measurement"]["job_id"] != successor["measurement"]["job_id"]
        return {"transport":"ordinary GX admission and successor message; controlled native decisions, real detached CPU Jobs",
                "model_calls":0,"workdir":str(work),"builder_job":build,"publication":published,
                "negative_unrelated_input":rejected,"replayed_install_identical":True,"parent":parent,
                "successor_lookup":{"request":"record evidence coverage","total":candidates["total"],
                                    "selected":selected,"digest":card["spec"]["portable_sha256"],"activation":selection},
                "successor":successor}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("work",type=Path)
    p.add_argument("output",type=Path)
    args = p.parse_args()
    result = run(args.work.resolve())
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n")
