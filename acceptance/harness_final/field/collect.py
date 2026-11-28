"""Export receipted actions and scientific evidence by original Run id from a read-only database."""
import argparse
from contextlib import closing
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3

HERE = Path(__file__).resolve().parent
helper = importlib.util.spec_from_file_location(
    "field_evidence", HERE.parents[1] / "harness_refactor/field/evidence.py")
ev = importlib.util.module_from_spec(helper)
helper.loader.exec_module(ev)


def host_submissions(study, threads, committed):
    """Direct decision/report attempts include structural errors that GX never commits."""
    records = []
    for file in sorted((study/"codex/sessions").rglob("*.jsonl")):
        thread = turn = None
        pending = {}
        with file.open() as source:
            for line in source:
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    if not line.endswith("\n"):
                        break
                    raise
                r = item.get("payload",{})
                if item.get("type") == "session_meta":
                    thread = r.get("id")
                    if thread not in threads or r.get("model_provider") != "openai":
                        break
                if item.get("type") == "turn_context":
                    turn = r.get("turn_id")
                if thread not in threads or item.get("type") != "response_item":
                    continue
                cid = r.get("call_id")
                if r.get("type") == "function_call" and r.get("name") in (
                        "gx_decide_revision","gx_submit_module_report"):
                    attempt = {"thread_id":thread,"turn_id":turn,"call_id":cid,"tool":r["name"],
                               "at_utc":item.get("timestamp"),"arguments_sha256":hashlib.sha256(
                                   r.get("arguments","").encode()).hexdigest(),
                               "gx_native_row":(thread,turn,cid) in committed,"feedback":None}
                    pending[cid] = attempt
                    records.append(attempt)
                if r.get("type") == "function_call_output" and cid in pending:
                    output = r.get("output")
                    try:
                        result = json.loads(output)
                    except (TypeError,ValueError):
                        result = None
                    pending[cid]["feedback"] = (ev.pick(result,
                        "code category error pointer failure_count max_failures terminal outcome event_id report_ref")
                        if isinstance(result,dict) else str(output)[:400])
    return records


def export(study, run):
    state = study / "state"
    host = ev.rollouts(study)
    with closing(sqlite3.connect((state / "gx.db").resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        goal = db.execute("SELECT * FROM v6_goals WHERE run_id=?", (run,)).fetchone()
        if goal is None:
            raise ValueError("Unknown Run")
        config = json.loads(db.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run,)).fetchone()[0])
        spec = config["inputs"]["p0_spec"]
        inputs = [ev.pick(x, "role artifact_ref sha256") for x in config["files"]]
        jobs = ev.jobs(db, state, run)
        for item in jobs:
            manifest = db.execute("SELECT completion_json FROM p0_execution_manifests WHERE job_id=?",
                                  (item["job_id"],)).fetchone()
            if manifest and manifest[0]:
                done = json.loads(manifest[0])
                if done.get("failure"):
                    item["failure"] = done["failure"]
        output_refs = {f["artifact_ref"] for j in jobs for f in j["outputs"] if f["verified_bytes"]}
        decisions = ev.decisions(db, run)
        known = {f["artifact_ref"] for f in inputs} | output_refs | {x for v in decisions for x in v["evidence_refs"]}
        reports = []
        for row in db.execute("SELECT receipt_json FROM v62_module_reports WHERE run_id=? ORDER BY rowid", (run,)):
            r = json.loads(row[0])
            raw = ev.bytes_for(db, state, r["report_ref"], r["report_sha256"])
            doc = json.loads(raw) if raw else {}
            author = r.get("author") or {}
            submit = db.execute("""SELECT n.request_json FROM p0_v6_native_calls n
                JOIN p0_roots rt USING(thread_id) JOIN v6_decision_tasks t USING(task_id)
                WHERE t.run_id=? AND t.scope=? AND rt.task_id=? AND n.thread_id=? AND n.turn_id=? AND n.call_id=?""",
                (run, r["scope"], author.get("task_id"), author.get("thread_id"), author.get("turn_id"), author.get("call_id"))).fetchone()
            cited = sorted(set(r["source_refs"]).intersection(ev.strings([doc.get(k) for k in (
                "qualitative_notes","findings","verified_metrics","metrics","source_quotations")])))
            known.add(r["report_ref"])
            reports.append({**ev.pick(r, "module_instance_id scope author report_ref report_sha256 event_id wake_event_id status"),
                            **ev.pick(doc, "findings qualitative_notes researcher_message gaps next_steps applicability"),
                            "sources":r["source_refs"], "cited_scientific_refs":cited, "verified_bytes":raw is not None,
                            "official_worker_submission":bool(author.get("kind") == "MODULE" and submit and
                              json.loads(submit[0]).get("tool") == "gx_submit_module_report" and
                              host.get((author.get("thread_id"), author.get("turn_id")), {}).get("provider") == "openai")})
        documents, derived = {}, {}
        for ref in known:
            row = db.execute("SELECT sha256,kind,media_type FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone()
            if row is None or "json" not in (row["media_type"] or ""):
                continue
            raw = ev.bytes_for(db, state, ref, row["sha256"])
            if raw is None:
                continue
            data = json.loads(raw)
            canon = json.dumps(data,ensure_ascii=False,sort_keys=True,separators=(",", ":")).encode()
            documents[ref] = hashlib.sha256(canon).hexdigest()
            if row["kind"] in ("v62_source_extraction","v62_evidence_review") and data.get("source_ref") in output_refs:
                src = db.execute("SELECT sha256 FROM artifacts WHERE artifact_ref=?", (data["source_ref"],)).fetchone()
                if src and src["sha256"] == data.get("source_sha256"):
                    derived[ref] = data["source_ref"]
        turns = ev.turns(db,run,host,known)
        actions = ev.calls(db,run,host,known,documents)
        real = {(t["thread_id"],t["turn_id"]) for t in turns if (t["official"] or {}).get("provider") == "openai"}
        request_index = {}
        for r in db.execute("""SELECT n.thread_id,n.turn_id,n.call_id,n.request_json FROM p0_v6_native_calls n
                JOIN p0_roots rt USING(thread_id) WHERE rt.run_id=?""", (run,)):
            request_index[(r["thread_id"],r["turn_id"],r["call_id"])] = json.loads(r["request_json"])
        for c in actions:
            req = request_index[(c["thread_id"],c["turn_id"],c["call_id"])]["arguments"]
            if c["tool"] in ("gx_open_module","gx_send_research_hint","gx_select_capability",
                              "gx_instantiate_recipe","gx_watch_join"):
                c["submitted_arguments"] = req
            if c["tool"] == "gx_decide_revision":
                c["decision_ops_as_submitted"] = req.get("decision",{}).get("ops",[])
            c["official"] = c["official"] and (c["thread_id"],c["turn_id"]) in real
        extractions = []
        for row in db.execute("""SELECT n.thread_id,n.turn_id,n.call_id,n.rowid AS seq,n.request_json,c.result_json,
                t.scope,rt.task_id FROM p0_v6_native_calls n JOIN p0_roots rt USING(thread_id)
                JOIN v6_decision_tasks t USING(task_id) JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
                WHERE rt.run_id=? AND json_extract(n.request_json,'$.tool')
                IN ('gx_extract_evidence','gx_review_evidence') ORDER BY n.rowid""",(run,)):
            req = json.loads(row["request_json"])
            out = json.loads(row["result_json"])
            args = req["arguments"]
            extractions.append({**ev.pick(row,"thread_id turn_id call_id task_id scope seq"),"tool":req["tool"],
                "official":(row["thread_id"],row["turn_id"]) in real,
                "arguments":ev.pick(args,"claim applicability limitations source_ref receipt_id segment_id evidence_ref verdict"),
                "receipt":ev.pick(out,"accepted artifact_ref evidence_ref evidence_id quote_sha256 scope found_in anchor_status verdict")})
        receipts = {x["receipt_id"]:x for x in actions if x.get("receipt_id")}
        reads = []
        for row in db.execute("""SELECT r.receipt_id,s.blob_artifact_ref source_ref,s.blob_sha256,
                r.source_id,r.segment_id,g.label,g.text_sha256,g.text,r.task_id
                FROM source_reads r JOIN sources s USING(source_id)
                JOIN source_segments g USING(source_id,segment_id) JOIN v6_decision_tasks t USING(task_id)
                WHERE t.run_id=? ORDER BY r.rowid""",(run,)):
            c = receipts.get(row["receipt_id"])
            matched = (c and c.get("arguments",{}).get("source_ref") == row["source_ref"] and
                       c["task_id"] == row["task_id"] and
                       c.get("returned_text_sha256") == row["text_sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()
                       and ev.bytes_for(db,state,row["source_ref"],row["blob_sha256"]) is not None)
            reads.append({**ev.pick(row,"receipt_id source_ref blob_sha256 source_id segment_id label text_sha256"),
                          **ev.pick(c or {}, "seq call_id turn_id scope official"), "read_content_verified":bool(matched)})
        access = []
        for row in db.execute("""SELECT root_key,thread_id,task_id,cwd,effective_profile_json,
                brief FROM p0_roots WHERE run_id=? AND effective_profile_json IS NOT NULL""",(run,)):
            research = json.loads(row["effective_profile_json"]).get("research_access")
            if not research:
                continue
            settings = research["thread_config"]
            access.append({**ev.pick(row,"root_key thread_id task_id"),
                           **ev.pick(research,"cwd visible_files environments sandbox dynamic_tools"),
                           "shell_enabled":settings.get("features.shell_tool"),
                           "unified_exec_enabled":settings.get("features.unified_exec"),
                           "project_doc_bytes":settings.get("project_doc_max_bytes"),
                           "brief_mentions_private_custody":any(x in row["brief"] for x in (
                               "/custody/", "rubric.json", "archive-oracle.json", "independent-sql.json"))})
        recipes = []
        for row in db.execute("SELECT * FROM v6_saved_recipes WHERE source_run=? ORDER BY rowid", (run,)):
            doc = json.loads(row["definition_json"])
            recipes.append({**ev.pick(row,"reference digest source_run"),"definition":doc})
        index = [dict(row) for row in db.execute(
            "SELECT event_id,kind,subject_id,ts FROM events WHERE run_id=? ORDER BY event_id",(run,))]
        return {"run_id":run,"goal_text_sha256":hashlib.sha256(goal["goal"].strip().encode()).hexdigest(),
                **ev.pick(goal,"status stop_decision_ref"),"inputs":inputs, "predecessor":spec.get("predecessor",{}),
                "research_effort":spec.get("research_effort"),"turns":turns,"calls":actions,"jobs":jobs,
                "reports":reports,"original_reads":reads,"decisions":decisions,"research_access":access,"source_evidence_actions":extractions,
                "official_direct_submissions":host_submissions(study,{t["thread_id"] for t in turns},request_index),
                "lead_effects":ev.compare_decisions(db,run,goal,decisions,turns,actions,reports,output_refs,derived),
                "recipes":recipes,"events":ev.events(db,run),"event_locator":index}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("study",type=Path)
    p.add_argument("run_id")
    p.add_argument("output",type=Path)
    a = p.parse_args()
    payload = json.dumps(export(a.study,a.run_id),ensure_ascii=False,indent=2).encode() + b"\n"
    a.output.write_bytes(gzip.compress(payload,mtime=0) if a.output.suffix == ".gz" else payload)
