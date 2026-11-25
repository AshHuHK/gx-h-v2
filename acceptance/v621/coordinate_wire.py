"""Record the original 2RE9 publication through the official Codex host and a local scripted Responses peer."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h"), str(ROOT / "acceptance/v62b/engineering")]
from run import BINARY, MARKER, archive, declared_calls, fixture_env, replay_report, responses, wire_catalog
from peer import decide, historical_calls, read, report

ORIGINAL = ROOT / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"
ACQUISITION = ROOT / "examples/v621-coordinate-analysis/2RE9.json"
ORIGINAL_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"
METHOD = "deposited-chain-geometry@2"
GOAL = ("Measure ordered-backbone coverage in the supplied original 2RE9 coordinate record and report the "
        "selected and observed residue counts with their ratio. Restrict interpretation to this deposited model.")


def scripted(brief, replies):
    if brief.get("module"):
        source, = [m["artifact_ref"] for m in brief["materials"] if m["role"] == "structure"]
        if ("gx_read_artifact", source) not in replies:
            return read(source)
        if not brief["nodes"]:
            op = {"op":"add_node", "node":"local:coverage", "branch":brief["module"]["branch_id"],
                  "capability":METHOD, "inputs":[source], "parameters":{"mask":"ordered_backbone"},
                  "depends_on":[], "question":"Measure the ordered-backbone residue coverage in the original deposition."}
            return decide([op], "Run the scoped ordered-backbone measurement.", watch={"event":"job_settled","subject_ref":op["node"]})
        node, = brief["nodes"]
        ref = node.get("result_ref")
        if not ref:
            return decide([], "Await the admitted measurement.", watch={"event":"job_settled","subject_ref":node["node_id"]})
        if ("gx_read_artifact", ref) not in replies:
            return read(ref)
        output = replies["gx_read_artifact", ref]
        if output["mask"] != "ordered_backbone" or output["input_sha256"] != ORIGINAL_SHA:
            raise ValueError("The received output does not identify the commissioned archive and mask")
        return report("Archive coverage is {{metric:coverage}}.", [ref])
    if brief.get("module_reports"):
        released, = brief["module_reports"]
        metric, = released["metrics"]
        ref, report_ref = metric["artifact_ref"], released["report_ref"]
        staged = replies.get(("gx_stage_claim", None))
        if not staged:
            return "gx_stage_claim", {"statement":"Archive coverage is {{metric:coverage}}.", "level":"open",
                    "scope":json.loads(ACQUISITION.read_bytes())["applicability"],
                    "criteria":"Ordered-backbone selected positions divided by the observed-union positions.",
                    "limitations":"Sampling independence is unknown. The finding applies only to the deposited model.",
                    "rationale":"The released report cites the named geometry analysis and its verified input.",
                    "evidence_refs":[ref, report_ref]}
        reason = "The deposited-coordinate measurement is complete and the result is recorded in the claim."
        return decide([{"op":"close_run","reason":reason,"claim_refs":[staged["claim_ref"]],
                        "unresolved":["Sampling independence is unknown."]}], reason, [ref, report_ref])
    if not brief.get("previous_decision") and ("gx_open_module", None) not in replies:
        source, = [m["artifact_ref"] for m in brief["materials"] if m["role"] == "structure"]
        return "gx_open_module", {"module_id":"data_inventory","module_version":"2","round":1,
            "operation_key":"commission-coordinate-analysis", "input_refs":[source], "capabilities":[METHOD],
            "focus":"Measure the ordered_backbone selection in the supplied original archive.",
            "expected_delivery":"A sourced selected/observed coverage reading with the actual mask.",
            "sub_budget":{"job_submissions":1,"cpu_seconds":45,"worker_tasks":6,"api_calls":8,"tokens":320000}}
    return decide([], "Await the released coordinate inventory.")


def run(work, binary):
    work.mkdir(mode=0o700, parents=True)
    raw = work / "raw"
    raw.mkdir()
    lock = archive.read(ROOT / "release-assets/lock.json")["linux_x64"]["binary_sha256"]
    if archive.sha256(binary) != lock or archive.sha256(ORIGINAL) != ORIGINAL_SHA:
        raise ValueError("The host or archive differs from its pinned digest")
    limits = {"job_submissions":2, "api_calls":24, "tokens":1000000, "cpu_seconds":180,
              "worker_tasks":16, "gpu_jobs":0, "gpu_minutes":0, "wall_hours":1}
    grant = {"project":"original-coordinate-publication", "research_effort":"deep", "branch_delegation":False,
             "research_modules":["data_inventory@2"], "plugins":["org.gx.protein@0.3.0"], "capabilities":[METHOD],
             "evidence_policy":"gx.evidence/6.2.1", "run_budget":limits, "workspace_budget":limits,
             "turn_token_budget":32768, "initial_job_limit":2,
             "input_files":[{"role":"structure","path":str(ORIGINAL),"sha256":ORIGINAL_SHA}]}
    archive.save(work / "grant.json", grant)
    wire, commands = [], []

    def action(body, ident):
        if len(wire) >= 35:
            raise ValueError("The local acceptance exceeded its expected request bound")
        messages = (s for item in reversed(body["input"]) if item.get("role") in ("developer","user")
                    for s in responses.message_texts(item) if MARKER in s)
        payload = next(messages).split(MARKER,1)[1]
        brief, end = json.JSONDecoder().raw_decode(payload)
        replies = historical_calls(body["input"], responses.output_json)
        name, args = scripted(brief, replies)
        namespaces, tools = wire_catalog(body)
        namespace, = [ns for ns, tool in declared_calls(namespaces) if tool == name]
        # Retain the actual inbound model request as well as parsed fields usable by a verifier.
        body_path = raw / ("request-" + ident + ".json")
        archive.save(body_path, body)
        call_id = "call-" + ident
        wire.append({"call_id":call_id, "brief":brief, "brief_text":MARKER+payload[:end],
                     "tools":sorted(tools), "tool":name, "args":args, "namespace":namespace,
                     "request_file":body_path.name, "request_sha256":archive.sha256(body_path)})
        archive.save(raw / "wire.json", wire)
        call = {"type":"function_call","call_id":call_id,"name":name,"arguments":json.dumps(args)}
        if namespace:
            call["namespace"] = namespace
        return call, {"scope":brief["scope"],"tool":name}

    with responses.local_responses(raw / "requests.jsonl") as peer, patch.object(responses,"fixture_action",action):
        env = fixture_env(work, peer, str(binary))
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        assert env["GX_P0_TEST_PROVIDER"] == "gx_loopback" and not (work/"codex/auth.json").exists()

        def cli(verb, *args, offline=False):
            argv = [sys.executable, "-B", str(ROOT/"bin/gx-p0"), verb, "--state", str(work/"state"), *map(str,args)]
            child = {**env, **({"GX_CODEX_OFFLINE":"1"} if offline else {})}
            proc = subprocess.run(argv,cwd=work,env=child,capture_output=True,text=True,timeout=115)
            commands.append({"argv":argv,"exit":proc.returncode,"stdout":proc.stdout,"stderr":proc.stderr})
            archive.save(raw/"commands.json",commands)
            if proc.returncode or any("error" in row for row in peer.rows):
                raise ValueError(f"{verb}: {(proc.stderr or proc.stdout)[-400:]}, {[r.get('error') for r in peer.rows if 'error' in r]}")
            return json.loads(proc.stdout)

        start = cli("start","--goal",GOAL,"--run-key","original-2re9","--goal-config",work/"grant.json")
        run_id = start["run_id"]
        source, = start["materials"]
        assert peer.request_count == 0
        acquisition = cli("observe","--run-id",run_id,"--source","public-coordinate-record","--artifact-ref",source,
                          "--sha256",ORIGINAL_SHA,"--acquisition",ACQUISITION)
        assert peer.request_count == 0
        final = cli("run","--run-id",run_id,"--timeout",85)
        if final["goal_status"] != "CLOSED":
            raise ValueError(f"Goal did not close: {final.get('waiting_for')}")
        before = len(wire)
        replay = replay_report(work, run_id, wire)
        cold = cli("resume","--run-id",run_id,"--timeout",5,offline=True)
        assert before == len(wire) == peer.request_count and all(row["peer"]=="127.0.0.1" for row in peer.rows)
        assert final["resources"] == cold["resources"] and cold["goal_status"] == "CLOSED"
        public, = cold["claims"]
        with closing(sqlite3.connect((work/"state/gx.db").as_uri()+"?mode=ro",uri=True)) as db:
            job, = db.execute("SELECT job_id,status FROM jobs WHERE run_id=?",(run_id,))
            assert job[1] == "SUCCEEDED"
            doc, = db.execute("SELECT document_json FROM hypothesis_revisions JOIN hypotheses USING(hypothesis_id) WHERE run_id=?",(run_id,)).fetchone()
        claim = json.loads(doc)
        staged, = [row for row in wire if row["tool"] == "gx_stage_claim"]
        delivered, = staged["brief"]["module_reports"]
        metric, = delivered["metrics"]
        assert staged["brief"]["scope"] == "run" and replay["report_ref"] == delivered["report_ref"]
        assert (metric["numerator"],metric["denominator"],metric["display"]) == (49,164,"49/164 (0.299)")
        assert claim["metrics"] == [metric] and public["claim_revision_id"] == claim["claim_revision_id"]
        assert claim["statement"] == "Archive coverage is 49/164 (0.299)."
        summary = {"status":"PASS", "protocol":"official Codex App Server with a local scripted Responses peer",
                   "git_head":subprocess.check_output(["git","-C",ROOT,"rev-parse","HEAD"],text=True).strip(),
                   "codex_sha256":lock,"run_id":run_id,"source":acquisition,"job_id":job[0],
                   "report_replay":replay,"lead_stage_call":staged["call_id"],"metric":metric,"claim":claim,
                   "model_wire_requests":before,"extra_requests_after_resume":0,"paid_model_calls":0,"provider_tokens":None}
        archive.save(work/"summary.json",summary)
        print(json.dumps({"status":"PASS","run_id":run_id,"work":str(work)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=BINARY)
    args = parser.parse_args()
    work = args.work.resolve()
    if work.exists() or work.is_relative_to(ROOT):
        parser.error("Provide a fresh directory outside the checkout")
    run(work, args.binary.resolve())
