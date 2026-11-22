"""A natural supplement crosses a live scientific review using the pinned App Server and local CPU Jobs."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
from unittest.mock import patch

from e2e_grid import fixture_env
import e2e_responses as responses
from gx_core.p0.cli import demo_spec


PACKAGE = Path(__file__).resolve().parents[1]
OPERATOR_TEXT = "补充：先前对照可能受污染"
MESSAGE = "source-during-original-review"
ordinary_response = responses.fixture_action


def scientific_response(body, ident):
    """Read only the model-visible assignment and tool outputs, including the original source and observations."""
    item, facts = ordinary_response(body, ident)
    if facts.get("tool") != "gx_submit_product":
        return item, facts
    task = responses.brief_from(body)["input"]
    sources = task.get("supplement_sources", [])
    if not sources:
        return item, facts
    artifacts, _ = responses.tool_history(body["input"])
    product = json.loads(item["arguments"])["product"]
    proposal = artifacts.read_json(task["proposal_ref"])
    routes = [r for ref in task["evidence_refs"] for r in artifacts.read_json(ref)["routes"]]
    reviewed = []
    try:
        for source in sources:
            document = artifacts.read_json(source["artifact_ref"])
            raw_refs = [r["observations_ref"] for r in routes if r.get("observations_ref")]
            originals = [artifacts.read_json(ref) for ref in raw_refs]
            identified_control = any(r.get("control_id") and r["control_id"] in document["text"] for r in originals)
            if identified_control:
                disposition = "pending"
                reason = "The note names an observed control. Its possible contamination needs an independent measurement before interpreting that result."
            elif originals and all(r.get("method_id") == "fixture-grid" for r in originals) and "可能" in document["text"]:
                disposition = "rejected"
                reason = (f"The operator reports possible control contamination without a sample identifier. The "
                          f"{len(originals)} cited {proposal['method']} outputs are computed from input coordinates, "
                          "and neither observation identifies an experimental control. The warning does not change "
                          f"the threshold judgments at {proposal['threshold']:g} for these sampled coordinates.")
            else:
                disposition = "pending"
                reason = "The source cannot yet be linked to a tested observation. The relevance to the original decision is unresolved."
            reviewed.append({"source_id":document["source_id"],"artifact_ref":source["artifact_ref"],
                             "disposition":disposition,"reason":reason,"evidence_refs":raw_refs})
    except responses.ReadArtifact as needed:
        ref, = needed.args
        return {"type":"function_call","name":"gx_read_artifact","arguments":json.dumps({"ref":ref}),
                "call_id":"call-"+ident}, {**facts,"tool":"gx_read_artifact","ref":ref}
    field = "source_reviews" if task["output_kind"] == "science_assessment" else "source_decisions"
    if field == "source_decisions":
        assessment = artifacts.read_json(task["assessment_ref"])
        independent = {r["source_id"]:r for r in assessment["source_reviews"]}
        for result in reviewed:
            finding = independent[result["source_id"]]
            assert result["disposition"] == finding["disposition"] and result["evidence_refs"] == finding["evidence_refs"]
        product["reason"] += " " + " ".join(result["reason"] for result in reviewed)
    product[field] = reviewed
    return {**item,"arguments":json.dumps({"product":product})}, {**facts,"source_ids":[s["source_id"] for s in reviewed]}


def save(path, value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+"\n")


def snapshot(state):
    with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        def rows(sql, args=()):
            return [dict(r) for r in conn.execute(sql,args)]
        source = rows("SELECT s.source_id,g.text,g.text_sha256,s.provenance_json FROM sources s JOIN source_segments g "
                      "USING(source_id) WHERE s.kind='operator_note'")
        values = rows("SELECT artifact_ref,task_id,kind,sha256,namespace,rel_path FROM artifacts WHERE kind IN "
                      "('operator_source','science_assessment','science_decision','research_report','research_evidence','grid_observations','score_evaluation')")
        for entry in values:
            data = (state/"artifacts"/entry.pop("namespace")/entry.pop("rel_path")).read_bytes()
            assert hashlib.sha256(data).hexdigest() == entry["sha256"]
            entry["body"] = json.loads(data)
        return {"source":source, "artifacts":values, "message":rows("SELECT message_id,receipt_turn_id,action_json,result_json FROM "
                "p0_human_inputs WHERE message_id=?",(MESSAGE,)), "jobs":rows("SELECT job_id,status,scheduler_ref FROM jobs ORDER BY rowid"),
                "submitted":rows("SELECT t.task_id,t.role,t.stage,s.submission_id,t.attempt,s.result,l.principal,s.artifact_refs_json "
                "FROM submissions s JOIN tasks t USING(task_id) JOIN leases l ON l.lease_id=s.lease_id ORDER BY s.rowid"),
                "versions":rows("SELECT item_id,version,sha256 FROM p0_intent_versions ORDER BY item_id,version"),
                "outbox":rows("SELECT topic,idempotency_key,payload_json,consumed_at FROM outbox WHERE topic LIKE 'p0.%' ORDER BY outbox_id"),
                "decisions":rows("SELECT * FROM science_decisions"),"run":rows("SELECT run_id,status,workflow_version,workflow_sha256 FROM runs")[0]}


def run(out, codex):
    out.mkdir(parents=True,exist_ok=True)
    report = {"result":"FAIL","mode":"official Codex 0.169.0, scripted loopback Responses, genuine fixture-grid CPU Jobs",
              "commands":[],"operator_text":OPERATOR_TEXT}
    try:
        with tempfile.TemporaryDirectory(prefix="gx-actual-source-", ignore_cleanup_errors=True) as tmp:
            work = Path(tmp)
            state = work/"state"
            spec = demo_spec()
            spec["max_rounds"] = 1
            specfile = work/"project.json"
            save(specfile,spec)
            with responses.local_responses(work/"requests.jsonl") as peer, patch.object(responses,"fixture_action",scientific_response):
                env = fixture_env(work, peer, codex)
                report["codex"] = subprocess.check_output([codex,"--version"],env=env,text=True).strip()
                assert report["codex"] == "codex-cli 0.169.0"
                def cmd(verb,*flags):
                    argv = ["bin/gx-p0",verb,"--state",str(state),*flags]
                    report["commands"].append([a.replace(str(work),"$WORK") for a in argv])
                    return argv
                def invoke(verb,*args):
                    result = subprocess.run(cmd(verb,*args),cwd=PACKAGE,env=env,capture_output=True,text=True,timeout=90)
                    assert result.returncode == 0, result.stderr[-1500:]
                    return json.loads(result.stdout)
                invoke("start","--spec",str(specfile))
                original = snapshot(state)
                peer.fault_enabled = True
                execution = subprocess.Popen(cmd("run","--timeout","110"),cwd=PACKAGE,env=env,
                                             stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                try:
                    assert peer.fault_ready.wait(45), "original review never reached its prepared hand-in"
                    answer = invoke("message","--text",OPERATOR_TEXT,"--message-id",MESSAGE,"--queue-only")
                    assert answer["status"]=="queued", answer
                    deadline = time.monotonic()+12
                    while time.monotonic()<deadline:
                        with closing(sqlite3.connect((state/"gx.db").as_uri()+"?mode=ro",uri=True)) as check:
                            if check.execute("SELECT 1 FROM p0_human_inputs WHERE message_id=? AND consumed_at IS NOT NULL",
                                             (MESSAGE,)).fetchone():
                                break
                        time.sleep(0.03)
                    interim = snapshot(state)
                    assert interim["source"] and not interim["submitted"], "the source must commit before the old assessment"
                    save(out/"pre-review.json",interim)
                finally:
                    peer.fault_release.set()
                    try:
                        raw, errors = execution.communicate(timeout=125)
                    except subprocess.TimeoutExpired:
                        execution.terminate()
                        execution.communicate(timeout=10)
                        raise
                assert execution.returncode==0,errors[-1000:]
                state_view = json.loads(raw)
                assert state_view["status"]=="COMPLETED", (state_view["status"], state_view.get("tasks"))
                before_replay = peer.request_count
                same = invoke("message","--text",OPERATOR_TEXT,"--message-id",MESSAGE)
                reopened = invoke("run","--timeout","5")
                final = snapshot(state)
                save(out/"completed.json",final)
                save(out/"responses.json",peer.rows)
                save(out/"worker-reader.json",{"sources":reopened["supplements"],"reviews":reopened["reviews"]
                                              if "reviews" in reopened else reopened.get("review_history")})
                assert same["result"]["source_id"]==final["source"][0]["source_id"] and peer.request_count==before_replay
                assert original["versions"] == final["versions"] and len(final["jobs"])==2
                assert {j["job_id"] for j in original["jobs"]} == {j["job_id"] for j in final["jobs"]}
                assert {j["status"] for j in final["jobs"]}=={"SUCCEEDED"}
                src, = final["source"]
                assert src["text"] == OPERATOR_TEXT and src["text_sha256"] == hashlib.sha256(OPERATOR_TEXT.encode()).hexdigest()
                products = [a for a in final["artifacts"] if a["kind"] in ("science_assessment","science_decision")]
                old,new,decision = products
                assert not old["body"].get("source_reviews") and new["body"]["evidence_refs"]==old["body"]["evidence_refs"]
                assert new["body"]["source_reviews"][0]["source_id"]==src["source_id"]
                assert decision["body"]["assessment_ref"]==new["artifact_ref"]
                assert decision["body"]["source_decisions"]==new["body"]["source_reviews"]
                assert final["decisions"][0]["decision_ref"]==decision["artifact_ref"]
                assert len(final["submitted"])==3 and len({s["principal"] for s in final["submitted"]})==3
                source_ref=new["body"]["source_reviews"][0]["artifact_ref"]
                for p in (new,decision):
                    assert any(r.get("task_id")==p["task_id"] and r.get("ref")==source_ref for r in peer.rows)
                assert reopened["supplements"][0]["disposition"]==new["body"]["source_reviews"][0]["disposition"]=="rejected"
                assert all(row["consumed_at"] for row in final["outbox"] if row["topic"]=="p0.supplement.review")
                report.update(result="PASS",run=final["run"],message=final["message"],input_source_id=src["source_id"],
                              source_ref=source_ref,old_assessment=old["artifact_ref"],new_assessment=new["artifact_ref"],
                              decision=decision["artifact_ref"],disposition="rejected",jobs=len(final["jobs"]),
                              independent_worker_roots=3,official_response_requests=peer.request_count,replay_requests=0)
    except Exception as exc:
        report["result"] = "FAIL"
        report["error"] = type(exc).__name__ + ": " + str(exc)
        raise
    finally:
        save(out/"summary.json",report)


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",required=True,type=Path)
    parser.add_argument("--codex",default="codex")
    args=parser.parse_args()
    run(args.output.resolve(),shutil.which(args.codex))
