"""Publish a small, reproducible subset of the four credential-free engineering slots."""
import argparse
from contextlib import closing
import gzip
import io
import json
from pathlib import Path
import tarfile

from inspect_run import artifact, db_read, digest
from peer import GOAL

ARMS = ("quick", "medium", "change", "maintain")
TOOLS = {"gx_open_module", "gx_decide_revision", "gx_submit_module_report", "gx_stage_claim", "gx_read_artifact"}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n").encode()


def transcript(slot, turn, accepted_calls):
    path = slot / turn["rollout"]
    source = path.read_bytes()
    assert digest(source) == turn["rollout_sha256"]
    retained, observed = [], set()
    for line_number, line in enumerate(source.splitlines(), 1):
        event = json.loads(line)
        p, kind = event["payload"], event["type"]
        if kind == "session_meta":
            body = {k: p.get(k) for k in ("id", "cli_version", "model_provider")}
            assert body["model_provider"] == "gx_loopback"
        elif kind == "turn_context":
            body = {k: p.get(k) for k in ("turn_id", "model", "effort")}
        elif kind == "response_item" and p.get("type") == "message" and p.get("role") == "user":
            texts = [c["text"] for c in p.get("content", []) if c.get("text", "").startswith("GX research brief\n")]
            if not texts:
                continue
            original, = texts
            assert digest(original.encode()) == turn["brief_sha256"]
            body = {"role": "user", "frozen_brief": original}
        elif kind == "response_item" and p.get("type") == "function_call" and p["call_id"] in accepted_calls:
            assert p["name"] in TOOLS
            body = {k:p.get(k) for k in ("type", "name", "namespace", "arguments", "call_id")}
            observed.add(p["call_id"])
        elif kind == "response_item" and p.get("type") == "function_call_output" and p["call_id"] in accepted_calls:
            body = {k:p[k] for k in ("type", "call_id", "output")}
        elif kind == "event_msg" and p.get("type") == "token_count" and p.get("info"):
            body = {"total_token_usage": p["info"]["total_token_usage"]}
        else:
            continue
        retained.append({"line": line_number, "line_sha256": digest(line), "timestamp": event["timestamp"],
                         "type": kind, "payload": body})
    assert observed == set(accepted_calls)
    return b"".join(map(encoded, retained))


def records(work, network):
    trial = json.loads((work / "result.json").read_bytes())
    assert trial["status"] == "PASS" and set(trial["arms"]) == set(ARMS) and trial["paid_model_calls"] == 0
    route = json.loads(network.read_bytes())
    assert route["exit_code"] == 0 and "MATCH,REJECT" in route["proxy_config"] and route["ipv4_routes"] == []
    assert str(work.resolve()) == Path(route["command"][route["command"].index("--work") + 1]).resolve().as_posix()
    files = {"result.json": encoded(trial), "network.json": encoded({k: route[k] for k in (
        "exit_code", "interfaces", "ipv4_routes", "ipv6_route_interfaces", "proxy_program", "proxy_program_sha256",
        "proxy_config", "isolated_proxy_listeners", "resource_profile", "started_unix", "finished_unix")})}
    for arm in ARMS:
        slot = work / arm
        summary_bytes = (slot / "summary.json").read_bytes()
        assert digest(summary_bytes) == trial["arms"][arm]["sha256"]
        s = json.loads(summary_bytes)
        rid = s["run_id"]
        files[f"{arm}/summary.json"] = summary_bytes
        for t in s["turns"]:
            assert t["scope"] == "run" or t["scope"].startswith("module:")
            files[f"{arm}/native/{t['thread_id']}.jsonl"] = transcript(slot, t, t["calls"])
        with closing(db_read(slot / "state/gx.db")) as db:
            assert db.execute("SELECT goal FROM v6_goals WHERE run_id=?", (rid,)).fetchone()[0] == GOAL
            def rows(sql):
                return [dict(r) for r in db.execute(sql, (rid,))]
            ledger = {}
            for table in ("v6_graph_versions", "v6_decisions", "v6_nodes", "v62_module_reports", "v62_module_wakes",
                          "p0_root_deliveries"):
                ledger[table] = rows(f"SELECT * FROM {table} WHERE run_id=? ORDER BY rowid")
            ledger["p0_execution_manifests"] = rows("""SELECT m.* FROM p0_execution_manifests m
                JOIN jobs j USING(job_id) WHERE j.run_id=? ORDER BY j.rowid""")
            ledger["p0_root_calls"] = rows("""SELECT c.* FROM p0_root_calls c JOIN p0_roots r USING(thread_id)
                WHERE r.run_id=? ORDER BY c.rowid""")
            ledger["hypothesis_revisions"] = rows("""SELECT r.* FROM hypothesis_revisions r JOIN hypotheses h
                USING(hypothesis_id) WHERE h.run_id=? ORDER BY r.revision""")
            ledger["module_events"] = rows("""SELECT event_id,kind,ts,subject_id,payload_json FROM events WHERE run_id=?
                AND kind IN ('v62.module.opened','v62.module.reported','v62.module.wake') ORDER BY event_id""")
            files[f"{arm}/ledger.json"] = encoded(ledger)
            refs = [j["result_ref"] for j in s["jobs"]] + [r["ref"] for r in s["module_reports"]]
            for ref in refs:
                row, path = artifact(db, slot, rid, ref)
                files[f"objects/{row['sha256']}"] = path.read_bytes()
            for job in s["jobs"]:
                path = slot / "state/jobs" / job["job_id"] / "outcome.json"
                outcome = json.loads(path.read_bytes())
                fields = ("job_id", "tool_pid", "runner_pid", "started", "started_at", "finished_at", "exit_code",
                          "cpu_user_seconds", "cpu_system_seconds", "wall_seconds", "stdout_bytes", "stderr_bytes")
                files[f"{arm}/jobs/{job['job_id']}.json"] = encoded({k: outcome[k] for k in fields})
    files["SHA256SUMS"] = "".join(f"{digest(raw)}  {path}\n" for path, raw in sorted(files.items())).encode()
    return files


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--network", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    files = records(args.work.resolve(), args.network.resolve())
    with args.output.open("xb") as out, gzip.GzipFile(filename="", fileobj=out, mode="wb", mtime=0) as gz, \
            tarfile.open(fileobj=gz, mode="w") as tar:
        for path, raw in sorted(files.items()):
            info = tarfile.TarInfo(path)
            info.size = len(raw)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(raw))
    print(json.dumps({"sha256": digest(args.output.read_bytes()), "bytes": args.output.stat().st_size, "files": len(files)}))


if __name__ == "__main__":
    main()
