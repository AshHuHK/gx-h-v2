"""Start one fresh, blinded V6 observed-structure acceptance run via gx-p0."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

REPO = Path(__file__).resolve().parents[2]
CLI = REPO / "bin/gx-p0"
MODEL = "gpt-6.2-astra"
COORDINATES = REPO / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"
COORD_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"
LIMITS = {"api_calls": 5, "tokens": 1_000_000, "job_submissions": 5,
          "cpu_seconds": 300, "gpu_jobs": 0, "gpu_minutes": 0, "worker_tasks": 24,
          "wall_hours": 1}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                               allow_nan=False) + "\n", encoding="utf-8")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def live_environment(private_dir, model=MODEL):
    """Only the official host gets a reference to the existing login; use fresh native state."""
    from sys import path as import_path
    if str(REPO / "gx-h") not in import_path:
        import_path.insert(0, str(REPO / "gx-h"))
    from gx_core.p0.profiles import clash_env
    checked = clash_env(native_http=True)
    proxy = {k: v for k, v in checked.items() if k.lower() in
             {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}}
    required = {"http_proxy": "http://127.0.0.1:7890", "https_proxy": "http://127.0.0.1:7890",
                "all_proxy": "socks5h://127.0.0.1:7891"}
    if any(proxy.get(k) != v or proxy.get(k.upper()) != v for k, v in required.items()):
        raise ValueError("The configured network route does not match the workspace Clash endpoints")
    if any(set(proxy.get(k, "").split(",")) - {"", "localhost", "127.0.0.1", "::1"}
           for k in ("NO_PROXY", "no_proxy")):
        raise ValueError("Unexpected proxy bypass in the effective configuration")
    private_dir = Path(private_dir).resolve()
    if private_dir.is_relative_to(REPO):
        raise ValueError("Supply a private directory outside the product tree")
    auth = (Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "auth.json").resolve()
    if not auth.is_file():
        raise RuntimeError("No existing official file-backed OAuth login is available")
    private_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    if private_dir.stat().st_mode & 0o077:
        raise ValueError("The acceptance private directory must have mode 0700")
    home = private_dir / "home"
    codex = home / "codex"
    codex.mkdir(parents=True, mode=0o700)
    (codex / "auth.json").symlink_to(auth)
    (codex / "config.toml").write_text('project_doc_max_bytes = 0\nweb_search = "disabled"\n')
    profile = private_dir / "model-profile.json"
    save(profile, {"default": {"model": model, "provider": "openai", "effort": "xhigh",
                               "multi_agent": False}})
    env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "SSL_CERT_FILE",
                                       "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE") if k in os.environ}
    if (REPO / ".venv/bin/python3").is_file():
        env["PATH"] = str(REPO / ".venv/bin") + os.pathsep + env["PATH"]
    env.update(proxy)
    # gx-p0 is public and the product chooses its own release-pinned binary by default.
    if os.environ.get("GX_P0_CODEX"):
        env["GX_P0_CODEX"] = os.environ["GX_P0_CODEX"]
    env.update(HOME=str(home), CODEX_HOME=str(codex), GX_P0_PROFILE=str(profile),
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    return env


def command(env, args, *, timeout=90):
    proc = subprocess.run([str(CLI), *map(str, args)], env=env, cwd=env["HOME"],
                          text=True, capture_output=True, timeout=timeout)
    try:
        body = json.loads(proc.stdout)
    except ValueError:
        # Keep raw transport and potential credential diagnostics out of acceptance artifacts.
        body = {"error": "GX command returned no JSON", "stderr_bytes": len(proc.stderr.encode())}
    return proc.returncode, body


def preflight(env, out):
    login_rc, login = command(env, ["login-status"])
    rc, report = command(env, ["doctor", "--role", "planner"])
    result = {"timestamp": now(), "login": login.get("login"), "login_exit": login_rc,
              "doctor_exit": rc, **{k: report.get(k) for k in ("ready", "provider", "model",
              "codex_version", "account_type", "validation", "required_efforts", "error")},
              "model_catalog": report.get("models", {}).get(MODEL), "model_turns": 0}
    save(out / "preflight.json", result)
    return login_rc == rc == 0 and result["account_type"] == "chatgpt" and result["ready"]


def snapshot(db, run_id):
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=5)) as con:
        con.row_factory = sqlite3.Row
        con.execute("BEGIN")
        def rows(sql, params=(run_id,)):
            return [dict(r) for r in con.execute(sql, params)]
        state = rows("SELECT status, workflow_id, workflow_version FROM runs WHERE run_id=?")[0]
        state["jobs"] = rows("SELECT job_id,capability_id,status,scheduler_ref FROM jobs WHERE run_id=? ORDER BY rowid")
        state["roots"] = rows("SELECT thread_id,task_id,attempt,role,model,effort FROM p0_roots WHERE run_id=? ORDER BY rowid")
        state["turns"] = rows("SELECT u.thread_id,u.turn_id,u.total_tokens,u.terminal_status FROM p0_root_turn_usage u "
                              "JOIN p0_roots r ON r.thread_id=u.thread_id WHERE r.run_id=? ORDER BY u.rowid")
        state["ledger"] = rows("SELECT b.limits_json,b.spent_json,b.reserved_json FROM runs r JOIN budgets b "
                               "ON b.budget_ref=r.budget_ref WHERE r.run_id=?")
        state["decisions"] = rows("SELECT decision_ref,call_id,thread_id,turn_id,request_json,receipt_json "
                                  "FROM v6_decisions WHERE run_id=? ORDER BY rowid")
        return state


def invocation(env, args, timeout, receipt):
    argv = [str(CLI), *map(str, args)]
    started_ns = time.time_ns()
    with subprocess.Popen(argv, env=env, cwd=env["HOME"], stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, text=True) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                stdout, stderr = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
        try:
            body = json.loads(stdout)
        except ValueError:
            body = {"error": "No parseable GX stdout"}
        entry = {"argv": argv, "started_ns": started_ns, "finished_ns": time.time_ns(),
                 "exit_code": proc.returncode, "pid": proc.pid, "stdout": body,
                 "stderr_bytes": len(stderr.encode())}
        save(receipt, entry)
        return entry


def capture(db, rid, directory):
    result = subprocess.run([sys.executable, str(REPO / "acceptance/v6/export.py"), "capture",
                             "--db", str(db), "--run", rid, "--out", str(directory)],
                            capture_output=True, text=True, timeout=45)
    if result.returncode:
        raise RuntimeError("Observer could not capture the native GX state: " + result.stdout[:240])
    return directory


def capture_signature(state):
    return (state["status"], tuple(d["decision_ref"] for d in state["decisions"]),
            tuple((j["job_id"], j["status"]) for j in state["jobs"]),
            tuple((t["thread_id"], t["turn_id"], t["terminal_status"]) for t in state["turns"]))


def native_rollout(private, thread):
    for path in (private / "home/codex/sessions").rglob("*.jsonl"):
        with path.open() as stream:
            line = stream.readline()
        if line:
            row = json.loads(line)
            if row.get("type") == "session_meta" and row.get("payload", {}).get("id") == thread:
                return path
    return None


def observe_changes(private, rid, env, seconds, out):
    db = private / "state/gx.db"
    cli_args = ["run", "--state", db.parent, "--run-id", rid, "--timeout", str(seconds)]
    monitor = private / "monitor"
    monitor.mkdir()
    captures = []
    last_signature = None
    with ThreadPoolExecutor(max_workers=1) as worker:
        running = worker.submit(invocation, env, cli_args, seconds + 90, private / "run-invocation.json")
        while not running.done():
            state = snapshot(db, rid)
            signature = capture_signature(state)
            if signature != last_signature:
                folder = monitor / f"{len(captures):04d}"
                capture(db, rid, folder)
                captures.append(folder)
                last_signature = signature
            time.sleep(0.2)
        executed = running.result()
    captures.append(capture(db, rid, private / "settled"))
    # Avoid fresh model requests on cold reopen. GX may still finish or reconcile existing Jobs.
    reopened = invocation({**env, "GX_CODEX_OFFLINE": "1"},
                          ["resume", "--state", db.parent, "--run-id", rid, "--timeout", "3"],
                          60, private / "resume-invocation.json")
    capture(db, rid, private / "reopened")
    state = snapshot(db, rid)
    public = {k: state[k] for k in ("status", "jobs", "roots", "turns", "ledger")}
    save(out / "observed.json", {"run_id": rid, "captured_at": now(), "native": public,
                                 "run_exit": executed["exit_code"], "cold_resume_exit": reopened["exit_code"],
                                 "decisions": len(state["decisions"]), "window_candidates": len(captures),
                                 "provider_charge_usd": None, "evidence_status": "REQUIRES_WINDOW_VERIFICATION"})
    return captures, executed, reopened


def observed(args, private, out, env):
    sys.path.insert(0, str(REPO / "acceptance"))
    from acc1_research import code_version
    save(out / "source-at-launch.json", {**code_version(), "captured_at": now()})
    goal_file = (args.kit.resolve() / "protein/RESEARCH_GOAL.md" if args.kit
                 else REPO / "examples/v6-2re9/goal.md")
    if digest(COORDINATES) != COORD_SHA:
        raise ValueError("The deposited coordinate digest differs from the released asset")
    # Give the operator API only the goal, the released public coordinates and the actual limits.
    # The worktree, kit, acceptance expectations and native session archives stay outside the lead cwd.
    grant = private / "grant.json"
    save(grant, {"project": "v6-observed-structure", "capabilities":
                 ["tl1a-coordinate-quality@1", "tl1a-geometry@1", "tl1a-review@1"],
                 "input_files": [{"path": str(COORDINATES), "role": "structure", "sha256": COORD_SHA}],
                 "workspace_budget": LIMITS, "run_budget": LIMITS, "turn_token_budget": 150000})
    save(out / "operator-inputs.json", {"goal_sha256": digest(goal_file),
                                        "coordinates_sha256": digest(COORDINATES),
                                        "limits": LIMITS, "model": MODEL, "effort": "xhigh",
                                        "effective_config_requested": "official OpenAI, fresh thread store",
                                        "expected_provider_cost_usd": None})
    state_dir = private / "state"
    key = "observed-protein"
    argv = ["start", "--state", state_dir, "--run-key", key, "--goal",
            goal_file.read_text().strip(), "--goal-config", grant]
    started = invocation(env, argv, 90, private / "start-invocation.json")
    rid = started["stdout"].get("run_id")
    if started["exit_code"] or not rid:
        save(out / "observed.json", {"stage": "start", "exit": started["exit_code"],
                                     "reason": started["stdout"].get("error"), "model_turns": 0})
        return 3
    fresh = snapshot(state_dir / "gx.db", rid)
    if fresh["jobs"] or fresh["decisions"] or fresh["turns"] or fresh["workflow_id"]:
        raise ValueError("The Goal-only run did not open with an empty graph")
    if json.loads(fresh["ledger"][0]["limits_json"]) != LIMITS:
        raise ValueError("The effective child budget differs from the registered research limits")
    captures, run, resume = observe_changes(private, rid, env, args.seconds, out)
    def returned_state(receipt):
        return (receipt["exit_code"] == 0 or
                (receipt["exit_code"] == 75 and receipt["stdout"].get("driver") == "time_limit"))
    if not all(returned_state(receipt) for receipt in (run, resume)):
        return 4
    return prepare_observed_window(private, out, rid, captures)


def accepted_and_rejected(private, out):
    """Record only public GX calls and public error results. Original native sessions stay private."""
    raw = json.loads((private / "reopened/capture.json").read_bytes())
    rows = raw["rows"]
    decisions = {d["call_id"]: d for d in rows["v6_decisions"]}
    rejections = []
    for root in rows["p0_roots"]:
        thread = root["thread_id"]
        if not thread:
            continue
        path = native_rollout(private, thread)
        if not path:
            continue
        calls = {}
        turn = None
        with path.open() as stream:
            for line in stream:
                event = json.loads(line)
                item = event.get("payload") or {}
                if event["type"] == "event_msg" and item.get("type") == "task_started":
                    turn = item["turn_id"]
                if event["type"] != "response_item":
                    continue
                call_id = item.get("call_id")
                if item.get("type") == "function_call" and item.get("name") == "gx_decide_revision":
                    calls[call_id] = {"thread_id": thread, "turn_id": turn, "call_id": call_id,
                                      "namespace": item.get("namespace"), "tool": item["name"],
                                      "arguments": json.loads(item["arguments"])}
                if item.get("type") != "function_call_output" or call_id not in calls or call_id in decisions:
                    continue
                try:
                    answer = json.loads(item["output"])
                except (TypeError, ValueError):
                    continue
                if isinstance(answer, dict) and "error" in answer:
                    record = {**calls[call_id], "response": answer, "task_id": root["task_id"],
                              "attempt": root["attempt"], "native_rollout_name": path.name,
                              "source": "official Codex rollout function_call + matching function_call_output"}
                    save(out / (call_id + ".json"), record)
                    rejections.append(call_id)
    closed = [e for e in rows["events"] if e["kind"] == "v6.run.closed"]
    observed = [e for e in rows["events"] if e["kind"] == "v6.node.settled"]
    start = datetime.fromisoformat(rows["runs"][0]["created_at"])
    artifacts = {a["artifact_ref"]: a for a in rows["artifacts"]}
    operations = []
    for node in rows["v6_nodes"]:
        spec = json.loads(node["spec_json"])
        output = artifacts.get(node["result_ref"], {})
        operations.append({"node_id": node["node_id"], "job_id": node["job_id"],
                           "capability": spec["capability"], "parameters": spec["parameters"],
                           "status": node["status"], "exec_key": node["exec_key"],
                           "method_question": spec["question"], "result_ref": node["result_ref"],
                           "result_sha256": output.get("sha256")})
    after = json.loads((private / "settled/capture.json").read_bytes())
    def identity(snapshot):
        r = snapshot["rows"]
        return {"job_ids": sorted(j["job_id"] for j in r["jobs"]),
                "native_turns": sorted((u["thread_id"], u["turn_id"]) for u in r["p0_meter_native_turns"]),
                "decisions": sorted(d["decision_ref"] for d in r["v6_decisions"]),
                "graph_heads": sorted((g["version"], g["sha256"]) for g in r["v6_graph_versions"])}
    before_cold, reopened = identity(after), identity(raw)
    save(out / "execution-summary.json", {
        "run_id": raw["run_id"], "opened_at": start.isoformat(), "status": rows["runs"][0]["status"],
        "elapsed_to_first_observation_seconds": (
            datetime.fromisoformat(observed[0]["ts"]) - start).total_seconds() if observed else None,
        "actual_stop": [{k: e[k] for k in ("kind","event_id","ts","prev_state","new_state","payload_json")}
                        for e in closed],
        "decisions": [dict(decision_ref=d["decision_ref"], task_id=d["task_id"], thread_id=d["thread_id"],
                           turn_id=d["turn_id"], call_id=d["call_id"], decision=json.loads(d["request_json"])["decision"])
                      for d in rows["v6_decisions"]],
        "operations": operations, "costs": raw["costs"], "rejected_native_calls": rejections,
        "scientific_claims": [json.loads(h["document_json"]) for h in rows["hypothesis_revisions"]],
        "cold_replay_same_jobs_turns_decisions_graph_heads": before_cold == reopened,
        "cold_replay_same_costs": after["costs"] == raw["costs"],
        "cold_replay": {"before": before_cold, "after": reopened},
        "reused_nodes": sum(n["status"] == "REUSED" for n in rows["v6_nodes"]),
    })


def prepare_observed_window(private, out, rid, captures):
    accepted_and_rejected(private, out)
    histories = [(c, json.loads((c / "capture.json").read_bytes())["rows"]) for c in captures]
    final = histories[-1][1]
    # The verifier decides whether the submitted effect and evidence are sufficient.
    # Try every genuine window, keep every attempt and error for the independent reviewer.
    attempts = []
    for d in final["v6_decisions"]:
        decision_id = d["decision_ref"]
        request = json.loads(d["request_json"])
        refs = set((request.get("decision") or {}).get("evidence_refs", []))
        if not refs:
            continue
        earlier = [(folder, rows) for folder, rows in histories if
                   not any(x["decision_ref"] == decision_id for x in rows["v6_decisions"]) and
                   bool(refs & {a["artifact_ref"] for a in rows["artifacts"] if a["job_id"]}) and
                   any(j["status"] == "SUCCEEDED" for j in rows["jobs"])]
        rollout = native_rollout(private, d["thread_id"])
        if not earlier or rollout is None:
            attempts.append({"decision_ref": decision_id, "result": "missing preceding capture or native rollout"})
            continue
        before, _ = earlier[-1]
        dest = private / f"window-{len(attempts):03d}.json"
        save(dest, {"before": str(before.relative_to(private)), "settled": "settled",
                    "reopened": "reopened", "decision_ref": decision_id,
                    "rollout": str(rollout.relative_to(private)), "resume_receipt": "resume-invocation.json"})
        target = out / f"candidate-{len(attempts):03d}"
        cmd = [sys.executable, str(REPO / "acceptance/v6/export.py"), "bundle", "--mode", "observed",
               "--window", str(dest), "--out", str(target)]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        attempt = {"decision_ref": decision_id, "run_id": rid, "rollout_sha256": digest(rollout),
                   "workspace_window": dest.name, "export_exit": result.returncode,
                   "export_result": (result.stdout or result.stderr)[-1000:]}
        attempts.append(attempt)
        if not result.returncode:
            attempt["bundle"] = str(target.relative_to(out) / "bundle.json")
    save(out / "window-attempts.json", attempts)
    return 0 if any(a.get("export_exit") == 0 for a in attempts) else 5


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("preflight", "observed"))
    parser.add_argument("--private-dir", required=True, type=Path)
    parser.add_argument("--evidence-dir", required=True, type=Path)
    parser.add_argument("--kit", type=Path, help="use the exact neutral goal from the supplied Agent Kit")
    parser.add_argument("--seconds", type=int, default=360)
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error("Choose a run window from 1 to 3600 seconds")
    private = args.private_dir.resolve()
    out = args.evidence_dir.resolve()
    if out == private or out.is_relative_to(private) or private.is_relative_to(out):
        parser.error("Use separate private and reviewer directories")
    out.mkdir(parents=True, exist_ok=False)
    try:
        env = live_environment(private)
    except (ConnectionError, FileNotFoundError, RuntimeError, ValueError) as exc:
        save(out / "preflight.json", {"timestamp": now(), "ready": False, "error": str(exc),
                                      "stage": "environment", "model_turns": 0})
        return 2
    if not preflight(env, out):
        return 2
    if args.mode == "preflight":
        return 0
    return observed(args, private, out, env)


if __name__ == "__main__":
    sys.exit(main())
