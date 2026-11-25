"""Recreate the public coordinate signoff and verify the chain from physical bytes.

run creates an external fixture. verify uses read-only SQLite and the independent
archived-coordinate implementation to reproduce the published evidence receipt.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h"), str(ROOT / "acceptance/v6/reviewer")]
import independent_geometry as oracle

SOURCE = ROOT / "examples/v621-coordinate-analysis/2RE9.json"
SOURCE_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"
REPORT_TEST = ("acceptance.v621.test_verified_metrics.QuantitativePublication."
               "test_original_receipt_real_job_report_and_delivered_numbers")


def need(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def load(path):
    return json.loads(path.read_bytes())


def save(path, obj):
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def db_at(state):
    db = sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def one(db, sql, *params):
    row, = db.execute(sql, params)
    return row


def physical(db, state, run, ref):
    row = one(db, "SELECT * FROM artifacts WHERE run_id=? AND artifact_ref=? AND published=1", run, ref)
    path = state / "artifacts" / row["namespace"] / row["rel_path"]
    need(path.resolve().is_relative_to((state / "artifacts").resolve()), "artifact path outside study")
    raw = path.read_bytes()
    need(len(raw) == row["bytes"] and digest(raw) == row["sha256"], "artifact content no longer matches " + ref)
    return row, path


def cli(state, *args):
    argv = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), args[0], "--state", str(state), *args[1:]]
    return subprocess.run(argv, capture_output=True, text=True, env={**os.environ, "GX_CODEX_OFFLINE": "1"})


def run_controls(work):
    study = work / "positive"
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(map(str, (ROOT, ROOT / "gx-h"))),
           "GX_CODEX_OFFLINE":"1", "GX_V621_METRICS_WORK":str(study),
           "GX_V621_METRICS_TRACE":str(work / "metrics-trace.json")}
    subprocess.run([sys.executable, "-B", "-m", "unittest", "-v", REPORT_TEST], cwd=ROOT, env=env, check=True)
    state = study / "study/state"
    positive = load(work / "metrics-trace.json")
    # A second Goal owns a different artifact reference to identical published bytes.
    grant = load(study / "study/grant.json")
    grant["run_budget"] = {resource:0 for resource in grant["run_budget"]}
    save(work / "control-grant.json", grant)
    with closing(db_at(state)) as db:
        goal = one(db, "SELECT goal FROM v6_goals WHERE run_id=?", positive["run_id"])["goal"]
    start = cli(state, "start", "--goal", goal,
                "--run-key", "coordinate-source-control", "--goal-config", str(work / "control-grant.json"))
    need(start.returncode == 0, "control Goal admission failed: " + start.stderr.strip())
    target = json.loads(start.stdout)["run_id"]
    original = positive["source_event"]["artifact_ref"]
    with closing(db_at(state)) as db:
        local = one(db, "SELECT artifact_ref FROM artifacts WHERE run_id=? AND sha256=? AND origin='INGEST'",
                    target, SOURCE_SHA)["artifact_ref"]
    need(local != original, "source control failed to establish a distinct Run-local artifact")
    source_id = "cross-run-original"
    reject = cli(state, "observe", "--run-id", target, "--source", source_id, "--artifact-ref", original,
                 "--sha256", SOURCE_SHA, "--acquisition", str(SOURCE))
    need(reject.returncode and "run-local artifact" in reject.stderr, "foreign source was not refused by Run owner")
    save(work / "source-refusal.json", {"target_run_id":target, "own_ref_same_bytes":local, "foreign_ref":original,
         "source_id":source_id, "argv":reject.args[3:], "returncode":reject.returncode, "diagnostic":reject.stderr.strip()})
    public = cli(state, "status", "--run-id", positive["run_id"])
    public.check_returncode()
    save(work / "public-status.json", json.loads(public.stdout))


def verify_execution(state, transcript, public_status):
    rid = transcript["run_id"]
    metadata = load(SOURCE)
    with closing(db_at(state)) as db:
        source = transcript["source_event"]
        ref = source["artifact_ref"]
        raw, source_path = physical(db, state, rid, ref)
        need(raw["origin"] == "INGEST" and raw["sha256"] == SOURCE_SHA, "wrong archive object")
        source_event = one(db, "SELECT * FROM events WHERE event_id=? AND run_id=? AND kind='v6.observation.added'",
                           source["event_id"], rid)
        recorded = json.loads(source_event["payload_json"])
        need(recorded == {k:v for k,v in source.items() if k != "event_id"}, "operator receipt differs from database")
        acq = recorded["acquisition"]
        need(acq["artifact_ref"] == ref and acq["sha256"] == SOURCE_SHA and "sampling" in acq["unknown_dependencies"],
             "acquisition did not attest the exact original or exposed unsupported independence")
        for key in ("data", "source", "publication"):
            need(acq["dependencies"][key] == [metadata[key]], "registered acquisition differs from source descriptor")
        need(acq["applicability"] == metadata["applicability"], "source applicability differs from its descriptor")

        job_id = transcript["job_id"]
        job = one(db, "SELECT * FROM jobs WHERE run_id=? AND job_id=?", rid, job_id)
        need(job["status"] == "SUCCEEDED" and job["exit_code"] == 0, "real job failed or did not settle")
        admission_event = one(db, "SELECT event_id FROM events WHERE run_id=? AND subject_id=? AND kind='job.created'",
                              rid, job_id)["event_id"]
        need(source["event_id"] < admission_event, "original was registered after admission")
        sealed = one(db, "SELECT * FROM p0_execution_manifests WHERE job_id=?", job_id)
        for key in ("admission","completion"):
            need(digest(sealed[key+"_json"].encode()) == sealed[key+"_sha256"], key + " digest differs")
        adm, done = (json.loads(sealed[k+"_json"]) for k in ("admission", "completion"))
        pin, = adm["inputs"]
        out, = done["outputs"]
        need((pin["artifact_ref"], pin["sha256"], pin["role"]) == (ref, SOURCE_SHA, "structure")
             and done["admission_sha256"] == sealed["admission_sha256"] and done["job_id"] == job_id
             and adm["capability"]["id"] == "deposited-chain-geometry" and adm["capability"]["version"] == "2",
             "Job method or input identity differs")
        need(out["output_name"] == "geometry.json" and out["nature"] == "analysis"
             and out["source_inputs"] == [pin], "named analysis publication differs from input")
        output, output_path = physical(db, state, rid, out["artifact_ref"])
        need(output["job_id"] == job_id and output["sha256"] == out["sha256"] == out["runner_sha256"],
             "report did not bind the producing runner's bytes")
        geometry = load(output_path)
        # The released reviewer independently reads mmCIF columns and computes set intersections.
        _, union, masks = oracle.selections(oracle.MMCIF2Dict(str(source_path)))
        selected = len({atom[0] for atom in masks["ordered_backbone"]})
        denominator = len(union)
        need((selected, denominator) == (49, 164) == (geometry["selected_residues"], geometry["observed_union_residues"]),
             "independent integer counts disagree")
        need(geometry["input_sha256"] == SOURCE_SHA and geometry["mask"] == "ordered_backbone"
             and abs(selected / denominator - geometry["coverage_fraction"]) < 1e-12, "floating coverage differs")

        published = transcript["report"]
        report_ref = published["report_ref"]
        report_row, report_path = physical(db, state, rid, report_ref)
        report = load(report_path)
        stored = one(db, "SELECT receipt_json FROM v62_module_reports WHERE run_id=? AND report_ref=?", rid, report_ref)
        need(json.loads(stored["receipt_json"]) == published and published["report_sha256"] == report_row["sha256"],
             "module release differs from historical report")
        metric, = report["metrics"]
        display = f"{selected}/{denominator} ({selected/denominator:.3f})"
        need(metric["display"] == display and (metric["numerator"],metric["denominator"]) == (selected,denominator)
             and metric["artifact_ref"] == out["artifact_ref"] and metric["sha256"] == out["sha256"]
             and metric["inputs"] == [pin] and metric["output_name"] == out["output_name"]
             and metric["manifest_sha256"] == sealed["completion_sha256"]
             and metric["admission_sha256"] == sealed["admission_sha256"] and metric["method"] == adm["capability"]
             and metric["scope"]["mask"] == "ordered_backbone", "report metric has different origin or value")
        finding, = report["findings"]
        need(display in finding["text"] and finding["source_refs"] == [out["artifact_ref"]],
             "published finding omits the verified result")
        proof, = [x for x in report["sources"] if x["artifact_ref"] == out["artifact_ref"]]
        need(any(x.get("output_name") == out["output_name"] and x.get("manifest_sha256") == sealed["completion_sha256"]
                 for x in proof["receipts"]), "module publication lacks its named-output proof")

        revision = transcript["formal_claim"]
        decision = one(db, "SELECT * FROM v6_decisions WHERE run_id=? AND decision_ref=?", rid, revision["decision_id"])
        lead = one(db, "SELECT * FROM p0_roots WHERE run_id=? AND lease_id=?", rid, decision["lease_id"])
        need(lead["lease_id"] == transcript["lead_lease"] and lead["thread_id"] == decision["thread_id"],
             "the adopting Lead does not own the reported turn")
        delivery = one(db, "SELECT * FROM p0_root_deliveries WHERE thread_id=? AND turn_id=?",
                       lead["thread_id"], decision["turn_id"])
        packet = json.loads(lead["brief"].split("\n", 1)[1])
        delivered, = [r for r in packet["module_reports"] if r["report_ref"] == report_ref]
        need(delivered["metrics"] == [metric] and delivered["findings"] == report["findings"] and packet["scope"] == "run",
             "acknowledged Lead packet did not carry the checked report")
        head = one(db, "SELECT r.* FROM hypotheses h JOIN hypothesis_revisions r ON h.hypothesis_id=r.hypothesis_id "
                   "AND h.head_revision=r.revision WHERE h.run_id=?", rid)
        stored_claim = json.loads(head["document_json"])
        need(stored_claim == revision and digest(head["document_json"].encode()) == head["sha256"],
             "adopted claim revision differs from stored document")
        public, = public_status["claims"]
        need(public["claim_revision_id"] == revision["claim_revision_id"] and public["publication"] == revision["publication"]
             and revision["metrics"] == [metric] and display in revision["statement"] and revision["level"] == "open"
             and revision["publication"]["direction"] == "undetermined", "public conclusion differs from adopted evidence")
        adopted = one(db, "SELECT event_id FROM events WHERE run_id=? AND kind='v6.claim.adopted' AND subject_id=?",
                      rid, revision["hypothesis_id"])["event_id"]
        need(adopted > published["event_id"] > admission_event, "report and adoption order differs")
        need(len(list(db.execute("SELECT 1 FROM jobs WHERE run_id=?",(rid,)))) == 1, "unexpected duplicate execution")
        return {"schema":"gx.v621.coordinate-publication-receipt/1", "status":"PASS", "case":"2RE9 ordered_backbone",
                "run_id":rid, "model":"scripted native callback",
                "paid_model_calls":0, "provider_tokens":None, "real_cpu_jobs":1,
                "original":source, "source_descriptor_sha256":digest(SOURCE.read_bytes()),
                "job":{"job_id":job_id,"admission_event_id":admission_event,"method":adm["capability"],
                       "input":pin,"admission_sha256":sealed["admission_sha256"],"manifest_sha256":sealed["completion_sha256"]},
                "output":out, "metric":metric, "independent_check":{"source_sha256":SOURCE_SHA,
                    "implementation_sha256":digest(Path(oracle.__file__).read_bytes()),"parser":"Bio.PDB.MMCIF2Dict",
                    "numerator":selected,"denominator":denominator,"ratio":selected / denominator,"integer_difference":0,
                    "absolute_ratio_difference":abs(selected / denominator - geometry["coverage_fraction"]),
                    "fraction_absolute_tolerance":1e-12},
                "module_report":{"report_ref":report_ref,"sha256":report_row["sha256"],"event_id":published["event_id"],
                                 "module":published["module_instance_id"],"version":published["version"],"finding":finding},
                "delivered_lead":{"kind":"controlled native callback", "context_ref":decision["context_ref"],
                    "lease_id":lead["lease_id"],"thread_id":lead["thread_id"],"turn_id":decision["turn_id"],
                    "delivery_state":delivery["state"],"brief_sha256":digest(lead["brief"].encode()),
                    "report_ref":report_ref,"metric_ref":delivered["metrics"][0]["metric_ref"],"finding":delivered["findings"][0]},
                "public_claim":{"claim_revision_id":revision["claim_revision_id"],"revision_sha256":head["sha256"],
                    "statement":revision["statement"],"scope":revision["scope"],"limitations":revision["limitations"],
                    "decision_ref":decision["decision_ref"],"event_id":adopted,"publication":public["publication"]},
                }


def verify_controls(work):
    state = work / "positive/study/state"
    transcript = load(work / "metrics-trace.json")
    rid = transcript["run_id"]
    foreign = load(work / "source-refusal.json")
    receipt = verify_execution(state, transcript, load(work / "public-status.json"))
    with closing(db_at(state)) as db:
        _, local_path = physical(db, state, foreign["target_run_id"], foreign["own_ref_same_bytes"])
        need(digest(local_path.read_bytes()) == SOURCE_SHA and foreign["target_run_id"] != rid and foreign["foreign_ref"] == receipt["original"]["artifact_ref"]
             and foreign["returncode"] != 0 and "run-local artifact" in foreign["diagnostic"],
             "negative source test did not offer identical bytes under a foreign Run")
        need(not db.execute("SELECT 1 FROM events WHERE run_id=? AND kind='v6.observation.added' AND subject_id=?",
                            (foreign["target_run_id"],foreign["source_id"])).fetchone(), "rejected acquisition was published")
        rejects = [v for v in transcript["rejected"] if "149/164" in v.get("input", "") or "149/164" in v.get("claim", "")]
        need({v["callback"]["tool"] for v in rejects} == {"gx_submit_module_report", "gx_stage_claim"} and
             all("SCHEMA_INVALID" in v["reason"] for v in rejects),
             "expected both report and claim refusals for the fabricated ratio")
        need("149/164" not in json.dumps([receipt["module_report"]["finding"],receipt["public_claim"]["statement"]]),
             "fabricated ratio entered an output")
        for rejection in rejects:
            call = rejection["callback"]
            need(call["error"] == "SCHEMA_INVALID" and call["head_before"] == call["head_after"] and
                 call["tool"] in ("gx_submit_module_report","gx_stage_claim"), "negative callback changed the graph")
    receipt["source_version"] = load(work/"version.json")
    receipt["refusals"] = {"different_numerator":rejects,"foreign_original":foreign}
    return receipt


def verify_wire(work):
    from acceptance.v62b.study import receipts as native

    state = work / "state"
    summary = load(work / "summary.json")
    cold, = [json.loads(c["stdout"]) for c in load(work/"raw/commands.json")
             if c["argv"][3] == "resume" and c["exit"] == 0]
    need(cold["goal_status"] == "CLOSED" and summary["extra_requests_after_resume"] == 0,
         "offline resume did not preserve the closed result")
    wire = load(work/"raw/wire.json")
    stage, = [c for c in wire if c["tool"] == "gx_stage_claim"]
    rid = summary["run_id"]
    with closing(db_at(state)) as db:
        pub = one(db, "SELECT receipt_json FROM v62_module_reports WHERE run_id=?", rid)
        decision = one(db, "SELECT * FROM v6_decisions WHERE run_id=? AND decision_ref=?",
                       rid, summary["claim"]["decision_id"])
        lead = one(db, "SELECT * FROM p0_roots WHERE run_id=? AND lease_id=?", rid, decision["lease_id"])
        need(lead["provider"] == "gx_loopback" and summary["model_wire_requests"] == len(wire),
             "actual provider identity or local request count differs")
        transcript = {"run_id":rid, "source_event":summary["source"], "job_id":summary["job_id"],
                      "report":json.loads(pub["receipt_json"]), "formal_claim":summary["claim"], "lead_lease":lead["lease_id"]}
        session_path = native.native_session(work/"codex/sessions")[lead["thread_id"]]
        session, context, stamp, calls, _ = native.delivered(session_path, decision["turn_id"], lead["brief"])
        adopt, = [c for c in wire if c["call_id"] == decision["call_id"]]
        links = []
        for w in (stage, adopt):
            cid = w["call_id"]
            body_path = work/"raw"/w["request_file"]
            body = load(body_path)
            need(digest(body_path.read_bytes()) == w["request_sha256"] and w["brief_text"] == lead["brief"]
                 and any(lead["brief"] in part.get("text","") for item in body["input"] if item.get("role") == "user"
                         for part in item["content"]), "raw model request lacks the frozen Lead brief")
            name, args, line = calls[cid]
            callback = one(db, "SELECT * FROM p0_root_calls WHERE thread_id=? AND turn_id=? AND call_id=?",
                           lead["thread_id"], decision["turn_id"], cid)
            request_sha = digest(json.dumps([name,args],sort_keys=True,separators=(",",":"),ensure_ascii=False).encode())
            need(name == w["tool"] and args == w["args"] and callback["request_sha"] == request_sha
                 and body["model"] == context["model"], "official turn and GX callback differ")
            links.append({"call_id":cid, "tool":name, "arguments_sha256":request_sha,
                          "wire_sha256":w["request_sha256"], "official_session_line":line})
    receipt = verify_execution(state, transcript, cold)
    need(stage["args"]["evidence_refs"] == [receipt["output"]["artifact_ref"], receipt["module_report"]["report_ref"]]
         and "{{metric:coverage}}" in stage["args"]["statement"] and summary["report_replay"]["redeliveries"] == 2,
         "adopting Lead did not cite the released result and its report")
    receipt["model"] = "Official Codex App Server with local scripted Responses"
    receipt["source_version"] = {"git_head":summary["git_head"], "codex_sha256":summary["codex_sha256"]}
    receipt["delivered_lead"].update(kind=receipt["model"], session_sha256=digest(session_path.read_bytes()),
                                     official_session=session, turn_context=context, received_at=stamp,
                                     calls=links, model_wire_requests=len(wire))
    receipt["cold_recovery"] = {"goal_status":cold["goal_status"], "report_replays":2, "new_model_requests":0}
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode",choices=("run","verify","wire"))
    parser.add_argument("work",type=Path)
    parser.add_argument("--controls", type=Path, help="Verified control state to link to an official-wire result")
    parser.add_argument("--output", type=Path, help="Export a reconstructed signoff receipt")
    options = parser.parse_args()
    work = options.work.resolve()
    if options.mode == "wire":
        receipt = verify_wire(work)
        if options.controls:
            controls = verify_controls(options.controls.resolve())
            need(controls == load(options.controls/"verified-chain.json"), "independent negative controls differ")
            need(receipt["source_version"]["git_head"] == controls["source_version"]["git_head"],
                 "positive and rejected traces were generated on different commits")
            receipt["control_run_id"] = controls["run_id"]
            receipt["control_receipt_sha256"] = digest((options.controls/"verified-chain.json").read_bytes())
            receipt["refusals"] = controls["refusals"]
        if options.output:
            save(options.output, receipt)
        print(json.dumps({"status":"PASS","run_id":receipt["run_id"],"model":receipt["model"],
                          "output":str(options.output) if options.output else None,
                          "display":receipt["metric"]["display"],"controls":bool(options.controls)}))
        return
    if options.mode == "run":
        need(not work.is_relative_to(ROOT), "store signoff state outside the repository")
        work.mkdir(mode=0o700, parents=True, exist_ok=False)
        head = subprocess.check_output(["git","-C",str(ROOT),"rev-parse","HEAD"],text=True).strip()
        save(work/"version.json", {"git_head":head,"modified_files_at_start":subprocess.check_output(
            ["git","-C",str(ROOT),"status","--porcelain","--untracked-files=normal"],text=True).splitlines()})
        run_controls(work)
    receipt = verify_controls(work)
    archived = work / "verified-chain.json"
    if options.mode == "run":
        save(archived, receipt)
    elif archived.exists() or not options.output:
        need(receipt == load(archived), "archived source-to-claim receipt differs from readback")
    if options.output:
        save(options.output, receipt)
    output = options.output or archived
    print(json.dumps({"status":"PASS","run_id":receipt["run_id"],"verified_receipt":str(output),
                      "verified_receipt_sha256":digest(output.read_bytes())}))


if __name__ == "__main__":
    main()
