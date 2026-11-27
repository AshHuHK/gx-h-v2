"""Run the reviewed page reader as a new CPU Job on a former Goal's frozen publisher window."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from acceptance.v62a.interleavings_fixture import PRODUCT, draft, save
from acceptance.v622.test_close import SurveyedStudy, close_decision
from gx_core.artifacts import ArtifactService
from gx_core.p0 import manifest

METHOD = "scholarly-page-search@2"
QUESTION = "How many distinct publications does the archived two-provider search cover, and what can the metadata establish about reading?"


def run(directory, source, first_receipt):
    document = json.loads(source.read_bytes())
    original = json.loads(first_receipt.read_bytes())
    expected = next(row for row in original["jobs"] if row["action"] == "next")
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert sha == expected["output_sha256"]["search-snapshot.json"]
    if not directory.exists():
        directory.mkdir(mode=0o700, parents=True)
        shutil.copyfile(source, directory / "publisher-window.json")
        save(directory / "goal.json", {"project":"publisher-window-replay","research_effort":"deep",
            "budget_profile":"oauth_research","input_files":[{"path":"publisher-window.json","role":"archive","sha256":sha}]})
        cli = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "research", "--state", str(directory/"state"),
               "--run-key","publisher-window-replay","--goal",QUESTION,"--goal-config",str(directory/"goal.json"),
               "--timeout","1e-12"]
        created = subprocess.run(cli,env={**os.environ,"GX_CODEX_OFFLINE":"1"},capture_output=True,text=True)
        if created.returncode not in (0,75):
            raise RuntimeError(created.stderr or created.stdout)
    with SurveyedStudy(directory,reopen=True) as study:
        nodes = study.conn.execute("SELECT node_id FROM v6_nodes WHERE run_id=?",(study.run_id,)).fetchall()
        if not nodes:
            lead = study.next_actor("run")
            study.call(lead,"gx_select_capability",{"action":"enable","method":METHOD,"revision":0},"enable-page-reader")
            action = draft({"op":"open_branch","branch":"local:window","hypothesis":"Catalogue records can share publication identifiers.",
                            "alternative":"The supplied window may contain distinct publications for every row.",
                            "method_family":"public catalogue coverage replay",
                            "discriminating_observation":"Count unique linked identifiers over the frozen windows.",
                            "stop_condition":"Stop after checking the original merged count and source readout.",
                            "budget_limits":{"job_submissions":1,"cpu_seconds":95}},
                           {"op":"add_node","node":"local:replay","branch":"local:window","capability":METHOD,
                            "inputs":study.materials,"parameters":{"query":document["query"],"action":"replay"},
                            "depends_on":[],"question":QUESTION},watches=[{"event":"job_settled","subject_ref":"local:replay"}])
            decision=study.call(lead,"gx_decide_revision",{"decision":action},"replay-window-complete-branch")
            node=decision["aliases"]["local:replay"]
        else:
            node,=nodes[0]
        row = study.settle(node)
        assets=ArtifactService(study.conn)
        proof=manifest.read_manifest(study.conn,row["job_id"])
        output=next(v for v in proof["completion"]["outputs"] if v["output_name"]=="candidates.json")
        candidates=assets.read_json(output["artifact_ref"])
        assert candidates["merged"]==original["next_merged"] and candidates["sources"]==original["next_sources"]
        state, stop = study.conn.execute("SELECT status,stop_decision_ref FROM v6_goals WHERE run_id=?",(study.run_id,)).fetchone()
        if state not in ("CLOSING", "CLOSED"):
            lead = study.next_actor("run")
            context, = study.conn.execute("SELECT visible_refs_json FROM v6_decision_tasks WHERE task_id=?",
                                          (lead.task_id,)).fetchone()
            if row["result_ref"] not in json.loads(context):
                wait = draft(watches=[{"event":"job_settled","subject_ref":node}])
                wait["rationale"] = "Keep the current recorded inputs until the reanalysis result reaches this scope."
                study.call(lead,"gx_decide_revision",{"decision":wait},"await-original-result")
                lead = study.next_actor("run")
            close = close_decision([row["result_ref"]])
            close["rationale"]="The archived search windows establish catalogue coverage. A JATS body or a reading locator requires a separate acquisition or reading receipt."
            close["ops"][0]["unresolved"]=["The search metadata alone does not establish scientific independence or the number of full articles read."]
            stop=study.call(lead,"gx_decide_revision",{"decision":close},"conclude-on-delivery")["decision_ref"]
        study.advance()
        state, stop = study.conn.execute("SELECT status,stop_decision_ref FROM v6_goals WHERE run_id=?",
                                         (study.run_id,)).fetchone()
        assert state == "CLOSED"
        return {"run_id":study.run_id,"origin_run":original["run_id"],"origin_job":expected["job_id"],
                "origin_port":expected["ports"]["search-snapshot.json"],"input_sha256":sha,
                "original_archive_ref_in_new_run":study.materials[0],"job_id":row["job_id"],"method":METHOD,
                "admission":proof["admission"]["capability"],"result_ref":row["result_ref"],
                "candidate_output":output,"merged":candidates["merged"],"sources_equal":True,
                "scientific_state":state,
                "decision_ref":stop,"scope":"Controlled native decisions, one fresh offline CPU Job, zero model calls and zero publisher connections."}


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("workspace",type=Path)
    cli.add_argument("snapshot",type=Path)
    cli.add_argument("first_receipt",type=Path)
    args=cli.parse_args()
    print(json.dumps(run(args.workspace.resolve(),args.snapshot,args.first_receipt),indent=2))
