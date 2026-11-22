"""Compare the public plugin-selected study with an unselected Run and a cold v2.1 Run."""
import argparse
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "acceptance"))
import e2e_responses
from e2e_audit import audit, check_native_charges
from e2e_grid import fixture_env
from v21_staging import check, git, job_sidecars, now, read_db, save, sha, stage_source
from grid_peer import MeasurementPeer, TOOL


FROZEN = "61ed0067448658e02d181b3cb85d7f68e39b6616"
PLUGIN = "org.gx.science-grid@2.0.0"


def hash_json(value):
    data = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(data).hexdigest()


@contextmanager
def starting_install(candidate, frozen, old):
    """A staged upgrade keeps the operator's original absolute installation path."""
    if not old:
        yield
        return
    parked = candidate.with_name("parked-candidate")
    candidate.rename(parked)
    try:
        frozen.rename(candidate)
        try:
            yield
        finally:
            candidate.rename(frozen)
    finally:
        parked.rename(candidate)


def science(data):
    """Run-local artifact and receipt IDs are compared inside each Run by e2e_audit."""
    rounds = []
    for entry in data["rounds"]:
        routes = [{key: route[key] for key in ("path_id", "prediction", "supported", "observations")}
                  for route in entry["routes"]]
        decision = entry["decision"]
        rounds.append({"round": entry["round"], "threshold": entry["threshold"], "routes": routes,
                       "continue_paths": decision["continue_paths"], "pivots": decision.get("pivots", []),
                       "next_grids": decision["next_grids"], "reason": decision["reason"],
                       "stop_reason": decision.get("stop_reason")})
    return {"rounds": rounds, "stop_reason": data["stop_reason"],
            "settlements": [{key: row[key] for key in ("round_index", "path_id", "direction", "kind")}
                            for row in data["settlements"]]}


def worker_assignments(db, composed):
    assignments = []
    for row in db.execute("SELECT task_id,role,brief FROM p0_roots WHERE task_id IS NOT NULL ORDER BY rowid"):
        packet = json.loads(row["brief"].removeprefix("GX task brief\n"))
        details = packet.get("workflow_context", {}).get("grid_measurements")
        if composed:
            expected = {k: packet["input"][k] for k in ("proposal_ref", "evidence_refs", "assessment_ref")
                        if k in packet["input"]}
            check(expected == details, "plugin launch brief changed the assigned proposal, evidence or review")
        else:
            check(details is None, "legacy worker acquired composed task context")
        assignments.append({"task_id": row["task_id"], "role": row["role"], "measurement_inputs": details})
    check(len(assignments) == 6, "missing launch briefs for the three assessment and decision pairs")
    return assignments


def scope_receipts(state, scripted, data):
    with closing(read_db(state)) as db:
        results = []
        for call_id, seen in scripted.replies.items():
            rows = db.execute(
                "SELECT c.thread_id,c.turn_id,c.request_sha,c.result_json,r.task_id,r.attempt,r.role,r.lease_id "
                "FROM p0_root_calls c JOIN p0_roots r USING(thread_id) WHERE c.call_id=?", (call_id,)).fetchall()
            reply = {**seen, "call_id": call_id}
            if "error" in seen["result"]:
                check(not rows, "denied measurement was stored as a successful call")
            else:
                check(len(rows) == 1, "successful model reply has no unique durable callback")
                row = dict(rows[0])
                check(row["request_sha"] == hash_json([TOOL, seen["arguments"]])
                      and json.loads(row.pop("result_json")) == seen["result"], "callback bytes differ from model reply")
                accepted, = [s for s in data["submissions"] if s["task_id"] == seen["task_id"]]
                check(row["attempt"] == accepted["attempt"] and row["thread_id"] == accepted["principal"]
                      and row["task_id"] == seen["task_id"], "tool read has a different task, attempt or principal")
                reply["root_receipt"] = row
            results.append(reply)
        good = [r["result"] for r in results if "root_receipt" in r]
        check(len(results) == 4 and len(good) == 2, "expected two accepted route reads and two refusals")
        for route in data["rounds"][0]["routes"]:
            measured, = [r for r in good if r["path_id"] == route["path_id"]]
            observations = route["observations"]
            threshold = data["rounds"][0]["threshold"]
            checks = measured["metrics"]
            check(checks["observations"] == len(observations) and checks["max_score"] == max(p["score"] for p in observations)
                  and checks["count_at_or_above"] == sum(p["score"] >= threshold for p in observations),
                  "plugin metrics differ from the independent observations")
        kind = "science_assessment"
        expected = scripted.assessment
        row = db.execute("SELECT namespace,rel_path FROM artifacts WHERE artifact_ref=?",
                         (data["rounds"][0]["decision"]["assessment_ref"],)).fetchone()
        accepted = json.loads((state / "artifacts" / row["namespace"] / row["rel_path"]).read_bytes())
        check(expected and all(accepted[k] == v for k, v in expected.items()),
              "the accepted assessment differs from the worker's plugin-derived product")
        return {"tool": TOOL, "calls": results, "submitted_kind": kind, "accepted_assessment": expected}


def case(work, candidate, frozen, binary, name, base_spec, report, foreign_run=None):
    directory = work / name
    directory.mkdir()
    state = directory / "state"
    spec = {**base_spec, "plugins": [PLUGIN]} if name == "composed" else base_spec
    spec_path = directory / "spec.json"
    save(spec_path, spec)
    transcript = directory / "requests.jsonl"
    scripted = MeasurementPeer(foreign_run)
    summary = report["cases"][name] = {"commands": [], "spec_sha256": sha(spec_path)}
    with e2e_responses.local_responses(transcript) as peer, patch.object(e2e_responses, "fixture_action", scripted):
        env = fixture_env(directory, peer, str(binary))
        env["PATH"] = str(ROOT / ".venv/bin") + os.pathsep + env["PATH"]

        def cli(action, *args, label=None):
            command = [str(candidate / "bin/gx-p0"), action, "--state", str(state), *map(str, args)]
            start = time.monotonic()
            before = peer.request_count
            proc = subprocess.run(command, cwd=candidate, env=env, capture_output=True, text=True, timeout=145)
            info = {"install": label or ("v2.1" if candidate == frozen else "candidate"),
                    "argv": ["bin/gx-p0", action, "--state", "$STATE",
                             *[str(a).replace(str(spec_path), "$SPEC") for a in args]],
                    "exit_code": proc.returncode, "elapsed_seconds": round(time.monotonic()-start, 3),
                    "new_responses": peer.request_count-before}
            summary["commands"].append(info)
            output = directory / f"{len(summary['commands']):02d}-{action}"
            output.with_suffix(".stdout").write_text(proc.stdout)
            output.with_suffix(".stderr").write_text(proc.stderr)
            check(proc.returncode == 0, f"{name} {action} failed. {proc.stderr[-1200:]} See {output}.stdout")
            doc = json.loads(proc.stdout)
            info.update(run_id=doc["run_id"], status=doc["status"])
            return doc

        old = name == "cold-v21"
        label = "v2.1" if old or candidate == frozen else "candidate"
        with starting_install(candidate, frozen, old):
            created = cli("start", "--spec", spec_path, label=label)
            check(len(created["jobs"]) == 2, "start did not launch both selected experiments")
            paused = cli("stop", label=label)
        with closing(read_db(state)) as db:
            pin = dict(db.execute("SELECT run_id,workflow_id,workflow_version,workflow_sha256 FROM runs").fetchone())
        before = peer.request_count
        reopened = cli("status")
        check(reopened["run_id"] == created["run_id"] and reopened["status"] == paused["status"] == "PAUSED"
              and peer.request_count == before, "cold read issued research or changed the Run")
        final = cli("resume", "--timeout", "110")
        check(final["status"] == "COMPLETED" and final["report_ref"], "public resume did not deliver a scientific report")
        checked = audit(state)
        check({j["job_id"] for j in created["jobs"]} <= {j["job_id"] for j in checked["jobs"]},
              "initial competing CPU Jobs lost their identities")
        charges = check_native_charges(checked, peer.request_count * 35)
        check(not any("error" in row for row in peer.rows), "malformed test provider response")
        exposed = {row["task_id"] for row in peer.rows if TOOL in row["gx_tools"] and row.get("task_id")}
        if name == "composed":
            check(exposed, "no worker received the installed plugin tool")
            summary["measurement"] = scope_receipts(state, scripted, checked)
        else:
            check(not exposed and not scripted.replies, "plugin tool exposed to a legacy worker")
        summaries = science(checked)
        with closing(read_db(state)) as db:
            assignments = worker_assignments(db, name == "composed")
            caps = [dict(r) for r in db.execute(
                "SELECT DISTINCT c.capability_id,c.version,c.status,c.manifest_sha256,a.content_artifact_ref,"
                "json_extract(a.source_json,'$.admission') AS admission,"
                "json_extract(a.source_json,'$.shipped_sha256') AS shipped_sha256 FROM jobs j "
                "JOIN capabilities c ON j.capability_id=c.capability_id AND j.capability_version=c.version "
                "JOIN assets a ON c.capability_id=a.asset_id AND c.version=a.version "
                "AND c.manifest_sha256=a.content_sha256")]
            shipped = json.loads((candidate / "gx-h/config/shipped.json").read_bytes())["capabilities"]["fixture-grid@1"]
            check(len(caps) == 1 and (caps[0]["capability_id"], caps[0]["version"], caps[0]["admission"],
                                     caps[0]["shipped_sha256"]) == ("fixture-grid", "1", "SHIPPED", shipped)
                  and all(m["admission"]["capability"]["sha256"] == caps[0]["manifest_sha256"]
                          for m in checked["manifests"]), "Jobs did not use the shipped grid capability pin")
            binding = json.loads(db.execute("SELECT inputs_json FROM runs").fetchone()[0]).get("plugin_binding")
            if name == "composed":
                check(binding and len(binding["packages"]) == 1
                      and binding["packages"][0]["id"] + "@" + binding["packages"][0]["version"] == PLUGIN
                      and binding["capabilities"] == [{"capability_version": "fixture-grid@1", "portable_sha256": shipped,
                          "manifest_sha256": caps[0]["manifest_sha256"], "content_artifact_ref": caps[0]["content_artifact_ref"]}],
                      "the Run binding did not select the capability that JobService executed")
            else:
                check(not binding, "legacy Run unexpectedly acquired a plugin binding")
            check(dict(db.execute("SELECT run_id,workflow_id,workflow_version,workflow_sha256 FROM runs").fetchone()) == pin,
                  "cold continuation changed the Run workflow pin")
        check(len({j["idempotency_key"] for j in checked["jobs"]}) == 4, "duplicate CPU Job operation key")
        before_replay = peer.request_count
        replayed = cli("run", "--timeout", "10")
        after = audit(state)
        check(replayed["status"] == "COMPLETED" and peer.request_count == before_replay
              and hash_json(checked) == hash_json(after), "terminal replay changed the original results or accounting")
        summary.update(workflow=pin, plugin_binding=binding, capabilities=caps, jobs=checked["jobs"], report_ref=checked["report_ref"],
                       accepted_workers=checked["submissions"], scientific_settlements=checked["settlements"],
                       worker_assignments=assignments,
                       artifact_count=len(checked["artifact_hashes"]), artifact_index_sha256=hash_json(checked["artifact_hashes"]),
                       cpu_job_sidecars=job_sidecars(state, checked["jobs"]), ledger=charges, evidence=summaries,
                       model_visible_tool_tasks=sorted(exposed), scientific_result_sha256=hash_json(summaries),
                       terminal_replay_new_calls=peer.request_count-before_replay, requests_sha256=sha(transcript))
        return state, created["run_id"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", default="WORKTREE", help="Git revision or WORKTREE")
    parser.add_argument("--work", type=Path, required=True, help="New private working directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--legacy-controls", action="store_true", help="Exercise only the unselected control paths")
    args = parser.parse_args()
    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    sources = ("acceptance/v3/p1/paired_grid.py", "acceptance/v3/p1/grid_peer.py",
               "acceptance/v3/p1/restore_bindings.py", "acceptance/e2e_responses.py",
               "acceptance/e2e_audit.py", "acceptance/e2e_grid.py", "acceptance/v21_staging.py",
               "gx-h/examples/science_judgment.py")
    result = {"started_utc": now(), "command": [Path(sys.executable).name, "-B", *sys.argv], "cases": {},
              "probe_sha256": {name: sha(ROOT / name) for name in sources},
              "mode": "official Codex 0.169.0; scripted localhost Responses; detached CPU Jobs", "result": "FAIL"}
    try:
        candidate = work / "candidate"
        frozen = work / "v21"
        rev = git("rev-parse", args.candidate) if args.candidate != "WORKTREE" else args.candidate
        result["frozen_install"] = stage_source(work, frozen, FROZEN)
        result["candidate_install"] = stage_source(work, candidate, rev)
        result["checkout_head"] = git("rev-parse", "HEAD")
        lock = json.loads((ROOT / "release-assets/lock.json").read_bytes())["linux_x64"]
        binary = ROOT / "vendor/codex-runtime/package" / lock["binary"]
        result["codex_sha256"] = sha(binary)
        check(result["codex_sha256"] == lock["binary_sha256"], "wrong official Codex binary")
        base_spec = json.loads((candidate / "examples/paired-grid.json").read_bytes())
        case(work, frozen, frozen, binary, "reference-v21", base_spec, result)
        case(work, candidate, frozen, binary, "cold-v21", base_spec, result)
        legacy_state, legacy_id = case(work, candidate, frozen, binary, "legacy", base_spec, result)
        if not args.legacy_controls:
            composed_state, _ = case(work, candidate, frozen, binary, "composed", base_spec, result, legacy_id)
            command = [sys.executable, "-B", str(Path(__file__).with_name("restore_bindings.py")),
                       str(candidate), str(legacy_state), str(composed_state)]
            proc = subprocess.run(command, text=True, capture_output=True, timeout=45)
            check(proc.returncode == 0, f"same-process cold restore failed: {proc.stderr[-900:]}")
            result["same_process_restore"] = json.loads(proc.stdout)
        check(len({v["scientific_result_sha256"] for v in result["cases"].values()}) == 1,
              "frozen, unselected or composed numerical findings/decisions differ")
        result.update(result="PASS", exit_code=0)
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}", exit_code=1)
        raise
    finally:
        result["ended_utc"] = now()
        save(args.output, result)


if __name__ == "__main__":
    main()
