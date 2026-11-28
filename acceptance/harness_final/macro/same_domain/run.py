"""Run the original yeast PMID coverage recipe on a new cohort, using controlled native callbacks."""
import argparse
from contextlib import closing
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[4]
HERE = Path(__file__).resolve().parent
EXPORT = ROOT / "acceptance/science_capabilities/portable_method"
SOURCE = json.loads((EXPORT / "source.json").read_bytes())
PROVIDER = "org.gx.record-coverage-8902880ed9d712bf6baa5899@1.0.0"
BUILDER = "org.gx.record-method-builder@1.0.0"
QUESTION = ("In the supplied S. cerevisiae S288C glucan 1,3-beta-glucosidase records, "
            "what fraction of distinct accessions has an experimental citation in its catalytic activity "
            "annotation, and which accessions require follow-up? What can these records establish?")


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def save(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2)+"\n")


def command(runner, *args, codes=(0,)):
    result = subprocess.run([*runner,*map(str,args)], text=True, capture_output=True, timeout=120)
    if result.returncode not in codes:
        raise RuntimeError(f"{args[0]} exited {result.returncode}: {result.stderr} {result.stdout}")
    return json.loads(result.stdout)


def setup(args):
    """Install in a private copy of the candidate to leave both field deployments intact."""
    args.work.mkdir(mode=0o700, parents=True, exist_ok=False)
    release = args.work / "release"
    release.mkdir()
    for folder in ("gx-h","bin"):
        shutil.copytree(ROOT/folder,release/folder,ignore=shutil.ignore_patterns("__pycache__","*.pyc","var"))
    subprocess.run([sys.executable,"-B",str(HERE/"fetch_materials.py"),"--db",str(args.index),"--out",str(args.work/"materials")],check=True)
    runner = [sys.executable,"-B",str(release/"bin/gx-p0")]
    installed = command(runner,"plugin","install",EXPORT/"plugin","--sha256",SOURCE["plugin_sha256"])
    verified = command(runner,"plugin","verify",PROVIDER)
    if verified["failures"] or installed["sha256"] != SOURCE["plugin_sha256"]:
        raise ValueError("Installed method package differs from the archived version")
    save(args.work/"installed.json",installed)
    env = {**os.environ,"PYTHONPATH":os.pathsep.join(map(str,(release/"gx-h",ROOT,ROOT/"gx-h/tests",ROOT/"acceptance/v62a"))),
           "GX_CODEX_OFFLINE":"1","PYTHONDONTWRITEBYTECODE":"1"}
    subprocess.run([sys.executable,"-B",str(__file__),"--execute","--work",str(args.work),
                    "--source-state",str(args.source_state),"--index",str(args.index)],env=env,check=True)


def verify_closed(work):
    """Reconcile the saved readout and decision with the closed target without dispatching work."""
    evidence = json.loads((work/"result.json").read_bytes())
    rid = evidence["run_id"]
    with closing(sqlite3.connect((work/"state/gx.db").as_uri()+"?mode=ro",uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        goal = conn.execute("SELECT status,stop_decision_ref FROM v6_goals WHERE run_id=?",(rid,)).fetchone()
        assert (goal["status"],goal["stop_decision_ref"]) == ("CLOSED",evidence["accepted_decision"])
        kinds = ("v6.decision.committed","v6.node.settled","v624.decision.rejected","v624.decision.corrected",
                 "v6.run.close_requested","v6.run.closed")
        evidence["events"] = [dict(row) for row in conn.execute(
            "SELECT event_id,kind,subject_id,reason FROM events WHERE run_id=? AND kind IN "
            "(SELECT value FROM json_each(?)) ORDER BY event_id",(rid,json.dumps(kinds)))]
        jobs = {x[0] for x in conn.execute("SELECT job_id FROM jobs WHERE run_id=?",(rid,))}
        assert len(jobs) == 2 and jobs == {r["job_id"] for r in evidence["target_jobs"].values()}
        assert digest(work/"state/research"/rid/"REPORT.md") == evidence["report_sha256"]
        out = evidence["published_output"]
        output = work/"state/artifacts"/rid/"jobs"/evidence["target_jobs"]["local:glucan-measure_coverage"]["job_id"]/out["rel_path"]
        assert digest(output) == out["sha256"] and json.loads(output.read_bytes()) == evidence["reading"]
    save(work/"result.json",evidence)


def execute(args):
    from gx_core import config
    from gx_core.native_release import native_state
    from gx_core.domain import ids
    from gx_core.p0 import v6_goal
    from acceptance.science_capabilities.restore_sealed_recipe import snapshot
    from acceptance.v62a.interleavings_fixture import Scenario, branch, draft
    from acceptance.harness_final.macro.test_portable_stages import attach

    state = args.work/"state"
    origin = args.source_state
    release = args.work/"release"
    assert config.HARNESS_ROOT == release/"gx-h"
    runner = [sys.executable,"-B",str(release/"bin/gx-p0")]
    archive = EXPORT/"accession-pmid-coverage@1.json"
    assert digest(archive) == SOURCE["recipe_sha256"]
    with (origin/".native.lock").open("rb") as lock, native_state(state):
        fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        restored = state.joinpath("gx.db").exists()
        with closing(sqlite3.connect((origin/"gx.db").as_uri()+"?mode=ro",uri=True)) as src:
            before = snapshot(src,SOURCE["source_run"],SOURCE["recipe"])
            assert before["recipe_digest"] == SOURCE["recipe_sha256"]
            with closing(sqlite3.connect(state/"gx.db")) as dst:
                if not restored:
                    src.backup(dst)
                assert snapshot(dst, SOURCE["source_run"], SOURCE["recipe"]) == before
        for folder in ("artifacts","jobs","research","runs","threads"):
            if not restored and (origin/folder).exists():
                shutil.copytree(origin/folder,state/folder)
    method, = [cap for cap in before["methods"] if cap.startswith("record-coverage-")]
    inputs = args.work/"materials"
    files = {"cohort":inputs/"candidates.json","evidence":inputs/"pointers.json",
             "method_source":ROOT/"examples/record-method-authoring/method-source.md"}
    options = {"research_effort":"deep","parent_method_options":[method],
               "input_files":[{"role":role,"path":str(path),"sha256":digest(path)} for role,path in files.items()]}
    save(args.work/"successor.json",options)
    call = command(runner,"message","--state",state,"--run-id",SOURCE["source_run"],
                   "--message-id","glucan-record-coverage","--text",QUESTION,
                   "--successor","--successor-config",args.work/"successor.json")
    save(args.work/"successor-admission.json",call)
    assert call["status"]=="successor_started", call
    run = call["result"]["run_id"]
    assert run != SOURCE["source_run"]
    opened = command(runner,"research","--state",state,"--run-id",run,"--timeout","0.000001",codes=(75,))
    assert opened["goal_status"]=="OPEN" and opened["driver"]=="time_limit", opened
    with Scenario(args.work,reopen=True) as s:
        attach(s,run)
        def past(key):
            record = s.conn.execute("SELECT c.result_json FROM p0_root_calls c JOIN p0_roots r USING(thread_id) "
                                    "WHERE r.run_id=? AND c.call_id=? ORDER BY c.rowid LIMIT 1",(run,key)).fetchone()
            return json.loads(record[0]) if record else None
        role_refs = {f["role"]:f["artifact_ref"] for f in json.loads(
            s.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?",(run,)).fetchone()[0])["files"]}
        measure = "local:glucan-measure_coverage"
        got, submitted = past("expand"), past("adopt-pinned-steps")
        if not submitted:
            s.lead = s.next_actor("run")
            s.call(s.lead,"gx_query_catalog",{"kind":"method","reference":method},"inspect-optional-method")
            selected = s.call(s.lead,"gx_select_capability",{"method":method,"action":"enable","revision":0},"select-saved-method")
            assert selected["revision"]==1
            s.call(s.lead,"gx_decide_revision",{"decision":draft()},"wait-for-view")
            s.lead = s.next_actor("run")
            released = s.call(s.lead,"gx_query_catalog",{"kind":"recipe","reference":SOURCE["recipe"]},"read-saved-recipe")
            assert released["spec"]["digest"] == SOURCE["recipe_sha256"]
            got = s.call(s.lead,"gx_instantiate_recipe",{"recipe":SOURCE["recipe"],"digest":SOURCE["recipe_sha256"],
                      "prefix":"glucan","branch":"local:coverage","inputs":role_refs},"expand")
            br = dict(branch("coverage"),hypothesis="Every unique fungal accession has a linked experimental paper.",
                      alternative="At least one lacks a pointer in this annotation snapshot.",
                      method_family="Accession and PMID field coverage",discriminating_observation="Count distinct matched accessions.",
                      stop_condition="A complete join over the supplied cohort is available.",
                      budget_limits={"job_submissions":2,"cpu_seconds":120})
            submitted = s.call(s.lead,"gx_decide_revision",{"decision":draft(br,*got["ops"],
                       watches=[{"event":"job_settled","subject_ref":measure}])},"adopt-pinned-steps")
        jobs = {}
        for op in got["ops"]:
            node = submitted["aliases"][op["node"]]
            settled = s.settle(node)
            jobs[op["node"]] = {k:settled[k] for k in ("node_id","job_id","result_ref","status")}
        result = jobs[measure]
        assert result["status"] == "SUCCEEDED" and result["job_id"] not in {j["job_id"] for j in SOURCE["source_nodes"].values()}
        lead = s.next_actor("run")
        answer = s.call(lead,"gx_read_artifact",{"ref":result["result_ref"]},"read-this-coverage")
        endpoints = []
        offset = 0
        while offset is not None:
            key = "receive-coverage-endpoints-"+str(offset)
            page_args = {"ref":result["result_ref"],"endpoint_offset":offset}
            page = s.call(lead,"gx_read_artifact",page_args,key)
            s.roots.delivery.observe("item/completed",{"threadId":lead.thread,"turnId":lead.turn,
                "item":{"type":"dynamicToolCall","tool":"gx_read_artifact","id":key,"arguments":page_args,
                        "status":"completed","success":True,"contentItems":[{"type":"inputText","text":ids.canonical_json(page)}]}})
            endpoints.extend(page["endpoint_rows"])
            offset = page["next_endpoint_offset"]
        expected = {r["accession"] for r in json.loads(files["cohort"].read_bytes())["candidates"]}
        linked = {r["entry"] for r in json.loads(files["evidence"].read_bytes())["pointers"] if r["pmid"]}
        assert (answer["numerator"],answer["denominator"]) == (len(expected & linked),len(expected))
        lacking = sorted(expected - linked)
        decision = (f"The supplied annotation has experimental PMID pointers for {answer['numerator']}/{answer['denominator']} "
                    f"distinct accessions. Prioritize source review for {', '.join(lacking) if lacking else 'none lacking a pointer'}. "
                    "The pointer count alone cannot establish substrate activity or absence of activity.")
        closing_op = {"op":"close_run","reason":decision,"claim_refs":[],"unresolved":
                      ["Direct substrate-specific experimental support must be checked in the cited primary studies."]}
        final = draft(closing_op,evidence=[result["result_ref"]])
        final["rationale"] = decision
        final["metric_refs"] = [result["result_ref"]+"#"+row["key"] for row in endpoints if row["id"] in ("coverage","numerator","denominator")]
        assert final["metric_refs"], endpoints
        completed = s.call(lead,"gx_decide_revision",{"decision":final},"decide-from-received-endpoints")
        v6_goal.ensure_task(s.conn,run)
        assert completed.get("task_status") == "ACCEPTED"
        after = snapshot(s.conn,SOURCE["source_run"],SOURCE["recipe"])
        assert before == after
        def record(sql,*params): return s.rows(sql,*params)
        evidence = {
            "transport":"SCRIPTED_CALLBACK","official_model_calls":0,"cpu_jobs":"real",
            "science_case":"Independent engineering control on a different food-yeast cohort",
            "candidate":subprocess.check_output(["git","-C",str(ROOT),"rev-parse","HEAD"],text=True).strip(),
            "question":QUESTION,"run_id":run,"original_recipe":before,"original_unchanged":before==after,
            "provider":PROVIDER,"plugin_sha256":SOURCE["plugin_sha256"],"source_index_sha256":digest(args.index),
            "target_inputs":{role:{"ref":role_refs[role],"sha256":digest(path)} for role,path in files.items()},
            "target_jobs":jobs,"published_output":record("SELECT artifact_ref,sha256,kind,rel_path FROM artifacts WHERE artifact_ref=?",result["result_ref"])[0],
            "reading":answer,"received_endpoints":endpoints,"used_metric_refs":final["metric_refs"],"independent_join":{"covered":sorted(expected & linked),"follow_up":lacking},
            "accepted_decision":completed["decision_ref"],"decision_turn":lead.turn,"reason":decision,
            "call_receipts":[{"thread":r[0],"call_id":r[1],"turn":r[2]} for r in s.conn.execute(
                "SELECT c.thread_id,c.call_id,c.turn_id FROM p0_root_calls c JOIN p0_roots r USING(thread_id) WHERE r.run_id=? ORDER BY c.rowid",(run,))]}
    report = command(runner,"research","--state",state,"--run-id",run,"--timeout","0.1",codes=(0,75))
    evidence["state"] = report["goal_status"]
    assert report["goal_status"] == "CLOSED", report
    evidence["report_sha256"] = digest(report["report"])
    save(args.work/"result.json",evidence)
    verify_closed(args.work)


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--source-state",type=Path)
    parser.add_argument("--index",type=Path)
    parser.add_argument("--verify-closed",action="store_true",help="Check existing local Run, Job and report receipts.")
    parser.add_argument("--execute",action="store_true",help=argparse.SUPPRESS)
    args=parser.parse_args()
    args.work=args.work.resolve()
    if args.verify_closed:
        verify_closed(args.work)
    else:
        if not args.source_state or not args.index:
            parser.error("a fresh study requires --source-state and --index")
        args.source_state,args.index = args.source_state.resolve(),args.index.resolve()
        if args.execute: execute(args)
        else: setup(args)
