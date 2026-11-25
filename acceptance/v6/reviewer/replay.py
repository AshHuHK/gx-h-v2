"""Audit a sealed V6 export against private primary sources without contacting a model."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys


def read(path):
    return json.loads(Path(path).read_bytes())


def sha(data):
    return hashlib.sha256(data).hexdigest()


def digest(obj):
    return sha(json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def require(value, message):
    if not value:
        raise ValueError(message)


def ref(bundle, item):
    raw = (bundle / item["file"]).read_bytes()
    require(sha(raw) == item["sha256"], "export object changed")
    return json.loads(raw)


def raw_artifact(state, row):
    raw = (state / "artifacts" / row["namespace"] / row["rel_path"]).read_bytes()
    require(sha(raw) == row["sha256"] and len(raw) == row["bytes"], "source artifact changed")
    return raw


def original_turn(path, decision, root, accepted):
    """Retain only declared host configuration, delivered user brief and accepted external tool arguments."""
    session, context, brief, native = None, None, None, None
    dynamic_reads = set()
    methods = []
    for number, line in enumerate(path.open("rb"), 1):
        frame = json.loads(line)
        kind, obj = frame["type"], frame["payload"]
        if kind == "session_meta":
            require(obj["id"] == decision["thread_id"], "wrong session")
            session = {k: obj[k] for k in ("cli_version", "model_provider", "runtime_workspace_roots")}
            session["first_frame_utc"] = frame["timestamp"]
            methods = [f["name"] for t in obj["dynamic_tools"] for f in t.get("tools", [t])]
        if kind == "turn_context":
            context = {k: obj[k] for k in ("turn_id", "model", "effort", "sandbox_policy", "approval_policy")}
            context["context_frame_utc"] = frame["timestamp"]
        if not context or context["turn_id"] != decision["turn_id"]:
            continue
        if kind == "event_msg" and obj.get("type") == "item_completed":
            child = obj.get("item", {})
            if (child.get("type"), child.get("tool"), child.get("success")) == ("DynamicToolCall", "gx_read_artifact", True):
                dynamic_reads.add(child["arguments"]["ref"])
        if kind != "response_item":
            continue
        if obj["type"] == "message" and obj.get("role") == "user":
            if any(part.get("text") == root["brief"] for part in obj["content"]):
                brief = json.loads(root["brief"].split("\n", 1)[1])
        if obj["type"] == "function_call" and obj["call_id"] == decision["call_id"]:
            require(obj["name"].split(".")[-1].endswith("gx_decide_revision"), "wrong native call")
            args = json.loads(obj["arguments"])
            native = args["decision"]
            if isinstance(native, str):
                native = json.loads(native)
            require(native == accepted, "native decision differs from adoption")
            break
    require(brief and native and session, "missing original model input or accepted tool call")
    return brief, {"call_line": number, "host": session, "context": context, "session_tools": methods,
                   "explicit_gx_reads_before_call": sorted(dynamic_reads), "adopted_intent_sha256": digest(native),
                   "accepted_call_utc": frame["timestamp"], "brief_sha256": sha(root["brief"].encode())}


def jobs(db, state, run_id):
    result = []
    for j in db.execute("SELECT * FROM jobs WHERE run_id=? ORDER BY job_id", (run_id,)):
        jid = j["job_id"]
        p = state / "jobs" / jid
        spec, outcome, status = (read(p / (n + ".json")) for n in ("spec", "outcome", "status"))
        require(all(v["job_id"] == jid for v in (spec, outcome, status)), "different CPU job identity")
        require(j["exit_code"] == outcome["exit_code"] == 0 and j["status"] == "SUCCEEDED", "unfinished CPU job")
        node, = db.execute("SELECT * FROM v6_nodes WHERE job_id=?", (jid,))
        attempt, = db.execute("SELECT * FROM v6_job_attempts WHERE node_id=?", (node["node_id"],))
        reservation, = db.execute("SELECT * FROM budget_reservations WHERE reservation_ref=?", (j["reservation_ref"],))
        require(reservation["status"] == "SETTLED" and reservation["subject_id"] == jid, "job charge differs")
        outputs = []
        for f in outcome["files"]:
            a, = db.execute("SELECT * FROM artifacts WHERE job_id=? AND rel_path=?", (jid, "files/" + f["path"]))
            raw = raw_artifact(state, a)
            require(sha(raw) == f["sha256"], "CPU output differs from evidence")
            outputs.append({"artifact_ref": a["artifact_ref"], "kind": a["kind"], "sha256": a["sha256"]})
        cpu = outcome["cpu_system_seconds"] + outcome["cpu_user_seconds"]
        require(abs(cpu - json.loads(j["measured_json"])["cpu_seconds"]) < .00001, "measured CPU discrepancy")
        result.append({"job_id": jid, "origin_decision": node["decision_ref"], "operation_key": node["operation_key"],
                       "reservation_id": reservation["reservation_ref"], "capability": j["capability_id"],
                       "request_sha256": attempt["request_sha256"], "parameters": json.loads(attempt["identity_json"])["parameters"],
                       "physical_start_utc": outcome["started_at"], "physical_finish_utc": outcome["finished_at"],
                       "gx_finish_utc": j["finished_at"], "runner_pid": outcome["runner_pid"], "tool_pid": outcome["tool_pid"], "cpu_seconds": round(cpu, 6),
                       "exit_code": j["exit_code"], "argv_sha256": digest(spec["argv"]), "outputs": outputs})
    return result


def check_window(window_path, state, bundle, all_runs, kit):
    window = read(window_path)
    db = sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    row, = db.execute("SELECT * FROM v6_decisions WHERE decision_ref=?", (window["decision_ref"],))
    rid = row["run_id"]
    run, = db.execute("SELECT * FROM runs WHERE run_id=?", (rid,))
    root, = db.execute("SELECT * FROM p0_roots WHERE thread_id=?", (row["thread_id"],))
    record, = [r for r in all_runs if r["run_id"] == rid]
    commit, turn, packet = (ref(bundle, record[k]) for k in ("commit", "turn", "packet"))
    receipt = json.loads(row["receipt_json"])
    intent = json.loads(row["request_json"])["decision"]
    require(receipt == ref(bundle, commit["gx_receipt"]), "export versus source commit mismatch")
    require((turn["thread_id"], turn["turn_id"], turn["decision_id"]) ==
            (row["thread_id"], row["turn_id"], row["decision_ref"]), "different native identity in export")
    brief, native = original_turn(window_path.parent / window["rollout"], row, root, intent)
    callback, = db.execute("SELECT result_json FROM p0_root_calls WHERE thread_id=? AND turn_id=? AND call_id=?",
                           (row["thread_id"], row["turn_id"], row["call_id"]))
    require(json.loads(callback[0]) == receipt, "GX acknowledgement differs")
    delivery, = db.execute("SELECT text_sha256 FROM p0_root_deliveries WHERE thread_id=? AND turn_id=? AND state='acknowledged'",
                           (row["thread_id"], row["turn_id"]))
    require(delivery[0] == native["brief_sha256"], "not the acknowledged brief")
    access = json.loads(root["effective_profile_json"])["research_access"]
    require(access == ref(bundle, record["native_access"]), "source access projection mismatch")
    require(root["model"] == native["context"]["model"] and root["effort"] == native["context"]["effort"] and
            root["provider"] == native["host"]["model_provider"] and set(access["dynamic_tools"]) == set(native["session_tools"]),
            "effective model, effort, provider or tools differ")
    native["access"] = {k: access[k] for k in ("visible_files", "environments", "sandbox", "developer_instructions_sha256")}
    data = json.loads(run["inputs_json"])
    source_hashes = {}
    for f in data["files"]:
        a, = db.execute("SELECT * FROM artifacts WHERE artifact_ref=?", (f["artifact_ref"],))
        source_hashes[f["role"]] = sha(raw_artifact(state, a))
        require(source_hashes[f["role"]] == f["sha256"], "visible initial material digest mismatch")
    presented = ref(bundle, turn["presented_evidence"])
    delivered = {o["artifact_ref"]: o["observation"] for o in brief["observations"] if o.get("artifact_ref")}
    case = window.get("packet_id", "observed")
    if case != "observed":
        require(read(kit / "acceptance" / "inputs" / (case + ".json")) == packet, "different frozen case")
    else:
        require(run["objective"].strip() == (kit / "protein/RESEARCH_GOAL.md").read_text().strip(),
                "observed goal is different")
    observation_links = []
    for obs in packet["observations"]:
        ref_id, = {k for k,v in presented["evidence_map"].items()
                   if v == obs["id"] and k in delivered}
        value = obs.get("measurement", obs.get("value"))
        a, = db.execute("SELECT * FROM artifacts WHERE artifact_ref=?", (ref_id,))
        require(delivered[ref_id] == value == json.loads(raw_artifact(state, a)), "presented value lacks primary provenance")
        require(ref_id in intent["evidence_refs"] or any(b["id"] in intent["evidence_refs"] for b in brief["observations"]
                                                        if b.get("artifact_ref") == ref_id), "measurement not cited")
        observation_links.append({"packet_observation": obs["id"], "artifact_ref": ref_id, "source_job": a["job_id"],
                                  "value_sha256": digest(value)})
    outcomes = jobs(db, state, rid)
    claims = [json.loads(z[0]) for z in db.execute("SELECT document_json FROM hypothesis_revisions WHERE author_task_id=?",
                                                  (row["task_id"],))]
    submitted_claims = set(intent["evidence_refs"]) | {ref for op in intent["ops"] for ref in op.get("claim_refs", [])}
    accepted_claims = {c["claim_ref"] for c in receipt["claim_revisions"]}
    for claim in claims:
        require(claim["decision_id"] == row["decision_ref"] and claim["claim_ref"] in submitted_claims & accepted_claims,
                "persistent claim authorship mismatch")
    snapshots = {k: read(window_path.parent / window[k] / "capture.json") for k in ("before", "settled", "reopened")}
    effects = {}
    for phase, snapshot in snapshots.items():
        exported = ref(bundle, record[phase])
        by_id = {j["job_id"]: j for j in snapshot["rows"]["jobs"]}
        require(set(by_id) == {j["job_id"] for j in exported["jobs"]}, "different Job set in original snapshot")
        charges = {j["job_id"]: j["ledger_id"] for j in exported["submission_ledger"]}
        require(all(j["reservation_ref"] == charges[j["job_id"]] for j in by_id.values()), "charge identity differed")
        effects[phase] = {"status": snapshot["rows"]["runs"][0]["status"], "science": exported["science"],
                          "scope_states": exported["scope_states"], "job_ids": sorted(by_id), "submissions": charges}
    for job in outcomes:
        require(job["reservation_id"] == effects["reopened"]["submissions"][job["job_id"]], "source job reservation mismatch")
    exported_jobs = ref(bundle, record["reopened"])["jobs"]
    for job in outcomes:
        archived, = (j for j in exported_jobs if j["job_id"] == job["job_id"])
        require((archived["operation_key"], archived["request_sha256"]) == (job["operation_key"], job["request_sha256"]),
                "source and archived job arguments differ")
    if claims:
        require(claims[-1]["science"] == effects["settled"]["science"], "claim projection differs from accepted claim")
    stop_events = []
    for e in db.execute("SELECT * FROM events WHERE run_id=? AND kind IN ('v6.run.closed','v6.branch.stopped')", (rid,)):
        payload = json.loads(e["payload_json"])
        if payload.get("decision_id") == row["decision_ref"]:
            stop_events.append({k: e[k] for k in ("event_id", "kind", "subject_id", "prev_state", "new_state")})
    if "close_run" in [o["op"] for o in intent["ops"]]:
        require(any(e["kind"] == "v6.run.closed" and e["new_state"] == "CLOSED" for e in stop_events),
                "accepted closure lacks terminal event")
    for field in ("science", "scope_states", "job_ids", "submissions"):
        require(effects["settled"][field] == effects["reopened"][field], f"{field} changed on cold reopen")
    if stop_events:
        require(effects["settled"]["status"] == effects["reopened"]["status"], "terminal state changed on reopen")
    resume = read(window_path.parent / window["resume_receipt"])
    require("resume" in resume["argv"] and resume["stdout"]["run_id"] == rid and resume["exit_code"] in (0,75),
            "unconfirmed cold resume")
    require({j["job_id"] for j in outcomes} == set(effects["reopened"]["job_ids"]), "later or missing physical jobs")
    budget, = db.execute("SELECT * FROM budgets WHERE run_id=?", (rid,))
    meters = [dict(z) for z in db.execute("SELECT b.reservation_ref,b.status AS reservation_status,"
             "m.confidence,m.settled_tokens FROM budget_reservations b JOIN p0_meter_turns m USING(reservation_ref) "
             "WHERE b.budget_ref=? AND b.subject_kind='native_turn' ORDER BY b.created_at", (budget["budget_ref"],))]
    billed = json.loads(budget["spent_json"])
    require(billed.get("job_submissions") == len(outcomes), "physical and billed Jobs differ")
    return {"case": case, "run_id": rid, "decision_id": row["decision_ref"], "thread_id": row["thread_id"],
            "turn_id": row["turn_id"], "call_id": row["call_id"], "native": native, "input_hashes": source_hashes,
            "model_profile_sha256": digest(ref(bundle, record["profile"])), "evidence": observation_links,
            "rationale": intent["rationale"], "actions": [o["op"] for o in intent["ops"]], "claims": [
                {k: z[k] for k in ("claim_revision_id", "statement", "scope", "level", "chain_pairs", "limitations")} for z in claims],
            "jobs": outcomes, "effects": effects, "source_stop_events": stop_events, "cold_resume": {"exit_code": resume["exit_code"], "pid": resume["pid"]},
            "reopened_capture_utc": datetime.fromtimestamp(snapshots["reopened"]["captured_ns"] / 1e9, timezone.utc).isoformat(),
            "tokens_at_capture": snapshots["reopened"]["costs"]["tokens"],
            "provider_usd": snapshots["reopened"]["costs"]["provider_charge"]["amount"],
            "budget_spent": billed, "budget_limits": json.loads(budget["limits_json"]),
            "budget_reserved_when_reviewed": json.loads(budget["reserved_json"]), "source_token_meter": meters,
            "measured_cpu_seconds": round(sum(j["cpu_seconds"] for j in outcomes), 6)}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--bundle", type=Path, required=True)
    cli.add_argument("--anchor", type=Path, required=True)
    cli.add_argument("--kit", type=Path, required=True)
    cli.add_argument("--window-state", type=Path, nargs=2, action="append", required=True)
    p = cli.parse_args()
    root, anchor, kit = [x.resolve() for x in (p.bundle, p.anchor, p.kit)]
    require(root not in anchor.parents, "anchor belongs in reviewer custody")
    raw = (root / "bundle.json").read_bytes()
    require(sha(raw) == read(anchor)["bundle_sha256"], "detached digest differs")
    runs = json.loads(raw)["runs"]
    results = [check_window(w.resolve(), s.resolve(), root, runs, kit) for w,s in p.window_state]
    require(len({r["run_id"] for r in results}) == len(runs) == len(results), "incomplete case coverage")
    if json.loads(raw)["mode"] == "controls":
        for field in ("thread_id", "turn_id", "decision_id"):
            require(len(set(r[field] for r in results)) == 3, "unmatched task identities")
        for field in ("input_hashes", "model_profile_sha256", "budget_limits"):
            require(len({digest(r[field]) for r in results}) == 1, "unmatched starting conditions")
    verifiers = {}
    for title, exe in [("frozen", kit / "acceptance/verify.py"),
                       ("extended", Path(__file__).resolve().parents[1] / "verify.py")]:
        result = subprocess.run([sys.executable, "-B", str(exe), str(root), "--anchor", str(anchor)],
                                capture_output=True, text=True, check=False)
        verifiers[title] = {"source_sha256": sha(exe.read_bytes()), "exit_code": result.returncode,
                            "message": (result.stdout + result.stderr).strip()}
    if json.loads(raw)["mode"] == "controls":
        frozen_run = runpy.run_path(str(kit / "acceptance/verify.py"))["verify_run"]
        frozen_suite = read(kit / "acceptance/reviewer/suite.json")
        for row in runs:
            case = next(r for r in results if r["run_id"] == row["run_id"])
            try:
                frozen_run(root, row, frozen_suite, "controls")
            except ValueError as error:
                case["frozen_case"] = {"status": "FAIL", "reason": str(error)}
            else:
                case["frozen_case"] = {"status": "PASS"}
    print(json.dumps({"review_kind": "independent program and engineering review", "bundle_sha256": sha(raw),
                      "verifiers": verifiers, "windows": sorted(results, key=lambda r:r["case"])},
                     ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
