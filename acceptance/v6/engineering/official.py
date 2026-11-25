"""Exercise two Goal-only decision turns over the locked official host with a local scripted peer."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

from public_cli import ROOT, archive, cli, frozen_state
sys.path[:0] = [str(ROOT / "acceptance"), str(ROOT / "gx-h")]
from e2e_grid import fixture_env
import e2e_responses as responses
from native_probe_peer import wire_catalog

STRUCTURE = ROOT / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"
MARKER = "GX research brief\n"
QUALITY = "tl1a-coordinate-quality@1"


def brief_from(body):
    for item in reversed(body.get("input", [])):
        if item.get("role") in {"user", "developer"}:
            for text in responses.message_texts(item):
                if MARKER in text:
                    return json.JSONDecoder().raw_decode(text.split(MARKER, 1)[1].lstrip())[0]
    raise ValueError("No V6 Lead brief in the model-visible request")


def scripted(brief):
    # This is a protocol test. The canned question and action are isolated from the live research runner.
    if not brief["observations"]:
        structure, = brief["materials"]
        branch, node = "local:inventory", "local:count"
        return {"mode": "amend", "rationale": "The question asks for a coordinate inventory.",
                "evidence_refs": [], "reconsider_when": [{"event": "job_settled", "subject_ref": node}],
                "ops": [{"op": "open_branch", "branch": branch, "hypothesis": "All observed chains have coordinates.",
                         "alternative": "At least one chain lacks coordinates.", "method_family": "deposited coordinates",
                         "discriminating_observation": "Per-chain observed atom counts.",
                         "budget_limits": {"job_submissions": 1, "cpu_seconds": 30},
                         "stop_condition": "Recover the coordinate inventory."},
                        {"op": "add_node", "node": node, "branch": branch, "capability": QUALITY,
                         "inputs": [structure["artifact_ref"]], "parameters": {}, "depends_on": [],
                         "question": "What chains have deposited coordinates?"}]}
    return {"mode": "amend", "rationale": "The deposited-coordinate inventory has returned. It completes this limited test.",
            "evidence_refs": brief["evidence_refs"], "reconsider_when": [{"event": "goal_changed", "subject_ref": "run"}],
            "ops": [{"op": "close_run", "reason": "The requested inventory was obtained.", "claim_refs": [],
                     "unresolved": ["This test drew no inference beyond this coordinate set."]}]}


def inventory(out, state, run_id, delivered):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        jobs = [dict(r) for r in db.execute("SELECT job_id,status,idempotency_key,measured_json FROM jobs WHERE run_id=?", (run_id,))]
        assert len(jobs) == 1 and jobs[0]["status"] == "SUCCEEDED", jobs
        node, = db.execute("SELECT node_id,result_ref,job_id,operation_key FROM v6_nodes WHERE run_id=?", (run_id,)).fetchall()
        assert node["job_id"] == jobs[0]["job_id"] and node["result_ref"] in delivered[-1]["visible"]["evidence_refs"], dict(node)
        decisions = [dict(r) for r in db.execute("SELECT * FROM v6_decisions WHERE run_id=? ORDER BY rowid", (run_id,))]
        assert len(decisions) == len(delivered) == 2
        assert len({r["thread_id"] for r in decisions}) == len({r["turn_id"] for r in decisions}) == 2
        assert [r["call_id"] for r in decisions] == [r["call_id"] for r in delivered]
        for stored, sent in zip(decisions, delivered):
            assert json.loads(stored["request_json"])["decision"] == sent["decision"]
        ref = db.execute("SELECT namespace,rel_path,sha256,job_id FROM artifacts WHERE artifact_ref=?",
                         (node["result_ref"],)).fetchone()
        assert ref["job_id"] == node["job_id"]
        body = (state / "artifacts" / ref["namespace"] / ref["rel_path"]).read_bytes()
        assert hashlib.sha256(body).hexdigest() == ref["sha256"]
        (out / "observation.json").write_bytes(body)
        result = json.loads(body)
        wire, = delivered[-1]["visible"]["observations"]
        assert (wire["artifact_ref"], wire["source_job_id"], wire["sha256"]) == (
            node["result_ref"], jobs[0]["job_id"], ref["sha256"])
        for field in ("schema", "input_sha256", "method", "source", "per_chain", "mapping"):
            assert wire["observation"][field] == result[field], field
        assert result["input_sha256"] == archive.sha256(STRUCTURE)
        assert result["method"] == QUALITY and set(result["per_chain"]) == {"A", "B", "C"}
        versions = [dict(r) for r in db.execute("SELECT version,sha256,parent_sha,decision_ref FROM v6_graph_versions WHERE run_id=? ORDER BY version", (run_id,))]
        assert len(versions) == 2 and versions[1]["parent_sha"] == versions[0]["sha256"]
        usage = [dict(row) for row in db.execute("SELECT thread_id,turn_id,confidence,budget_status,settled_tokens "
                   "FROM p0_meter_native_turns WHERE run_id=? ORDER BY sequence", (run_id,))]
        assert len(usage) == 2 and all(r["confidence"] == "PROVIDER_CONFIRMED" and r["budget_status"] == "SETTLED"
                                      and r["settled_tokens"] == 35 for r in usage), usage
        return {"versions": versions, "local_fixture_usage": usage, "jobs": jobs, "node": dict(node), "observation_sha256": ref["sha256"],
                "output_schema": result["schema"], "decisions": decisions}


def run(work, out, binary):
    pin = archive.read(ROOT / "release-assets/lock.json")["linux_x64"]["binary_sha256"]
    assert archive.sha256(binary) == pin, "Wrong official host"
    requests = []
    calls = []
    def action(body, ident):
        _, exposed = wire_catalog(body)
        assert "gx_decide_revision" in exposed, sorted(exposed)
        blocked = {name for name in exposed if any(part in name for part in
                   ("exec", "shell", "spawn", "web_search", "memory", "mcp", "apply_patch", "read_file"))}
        assert not blocked, sorted(blocked)
        brief = brief_from(body)
        expected_tools = {"gx", "gx_decide_revision", "gx_read_artifact", "gx_stage_claim", "gx_task_context"}
        if any(branch["status"] == "OPEN" for branch in brief["branches"]) and "gx_delegate_branch" in exposed:
            expected_tools.add("gx_delegate_branch")
        assert exposed == expected_tools, sorted(exposed)
        text = json.dumps(body.get("input", []), ensure_ascii=False) + (body.get("instructions") or "")
        assert not any(x in text for x in ("reference.json", "reviewer/suite.json", "common_score.json",
                                           "GX_H_V6_Agent_Kit", "V6_LEAD_READ_ISOLATION_SENTINEL"))
        decision = scripted(brief)
        call_id = "call-" + ident
        requests.append({"call_id": call_id, "visible": brief, "decision": decision, "catalog": sorted(exposed),
                         "request_sha256": hashlib.sha256(text.encode()).hexdigest()})
        archive.save(out / "model-inputs.json", requests)
        if len(requests) > 2:
            raise ValueError("Unexpected third model request; inspect model-inputs.json")
        return {"type": "function_call", "namespace": "gx", "name": "gx_decide_revision", "call_id": call_id,
                "arguments": json.dumps({"decision": decision})}, {"tool": "gx_decide_revision"}

    with responses.local_responses(out / "requests.jsonl") as server, patch.object(responses, "fixture_action", action):
        env = fixture_env(work, server, str(binary))
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        state = work / "state"
        def call(action, *extra):
            result = cli(state, env, action, *extra)
            calls.append(result)
            archive.save(out / "commands.json", calls)
            return result["body"]

        config = work / "goal.json"
        archive.save(config, {"project": "v6-engineering", "turn_token_budget": 1024,
                              "capabilities": [QUALITY], "input_files": [{"path": str(STRUCTURE), "role": "structure"}]})
        rid = call("start", "--goal", "How many deposited chains have observable coordinates?",
                   "--goal-config", config, "--run-key", "inventory")["run_id"]
        assert not server.request_count
        try:
            final = call("run", "--run-id", rid, "--timeout", "45")
            assert final["status"] == "STOPPED" and final["goal_status"] == "CLOSED", (final["status"], server.rows)
            before = inventory(out, state, rid, requests)
            seen = server.request_count
            assert call("resume", "--run-id", rid, "--timeout", "3")["status"] == final["status"]
            assert inventory(out, state, rid, requests) == before
            assert server.request_count == seen == 2, server.rows
            return {"passed": True, "run_id": rid, "scope": "scripted local Responses; actual CPU geometry inventory",
                    "lock_binary_sha256": pin, "official_host_version": subprocess.check_output(
                    [binary, "--version"], env=env, text=True).strip(), "run_status": final["status"],
                    "cold_resume_additional_requests": 0, "cold_resume_additional_jobs": 0, **before}
        finally:
            if (state / "gx.db").exists():
                frozen_state(state, out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="gx-v6-official-") as work:
        receipt = run(Path(work), args.out, args.binary.resolve())
    archive.save(args.out / "result.json", receipt)
