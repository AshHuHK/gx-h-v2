"""Install a CSV plugin, run two scripted decisions on the official host and check a real CPU Job."""
import argparse
from contextlib import closing
import csv
from decimal import Decimal
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "acceptance/v6/engineering"))
from official import archive, brief_from, fixture_env, frozen_state, responses, wire_catalog

METHOD = "sensor-calibration@1"
PACKAGE = ROOT / "examples/plugins/sensor_calibration"
EXAMPLE = ROOT / "examples/v61-sensor-calibration"


def independent_result(samples, result):
    """Recalculate from reference and reading columns using exact decimal sums."""
    paired = {}
    with samples.open(newline="") as stream:
        for row in csv.DictReader(stream):
            paired.setdefault(row["period"], []).append((Decimal(row["reading_mV"]), Decimal(row["reference_mV"])))
    assert result["unit"] == "mV" and result["method"] == METHOD
    assert result["input_sha256"] == archive.sha256(samples)
    assert [item["period"] for item in result["periods"]] == list(paired)
    checks = []
    tolerance = Decimal("1e-12")
    for reported in result["periods"]:
        pairs = paired[reported["period"]]
        n = len(pairs)
        assert n == reported["n"]
        means = {
            "mean_error": (sum(value for value, _ in pairs) - sum(ref for _, ref in pairs)) / n,
            "mean_absolute_error": sum(abs(value - ref) for value, ref in pairs) / n,
            "root_mean_square_error": (sum((value - ref) ** 2 for value, ref in pairs) / n).sqrt(),
        }
        delta = {key: abs(Decimal(str(reported[key])) - expected) for key, expected in means.items()}
        assert all(value <= tolerance for value in delta.values()), (reported, means)
        checks.append({"period": reported["period"], "count": n, "reference_mV": {k: str(v) for k, v in means.items()},
                       "absolute_difference_mV": {k: str(v) for k, v in delta.items()}})
    return {"method": "independent decimal arithmetic from source CSV", "tolerance_mV": str(tolerance), "periods": checks}


def decision(brief):
    if brief["head"] is None:
        assert brief["observations"] == [] and brief["nodes"] == []
        material, = brief["materials"]
        node = "local:measure"
        return {"mode": "amend", "rationale": "Compare periods using the paired references and signed errors.",
                "evidence_refs": [], "reconsider_when": [{"event": "job_settled", "subject_ref": node}],
                "ops": [
                    {"op": "open_branch", "branch": "local:calibration", "hypothesis": "The mean signed errors differ.",
                     "alternative": "The means agree at the input resolution.", "method_family": "paired voltage errors",
                     "discriminating_observation": "Mean signed errors, sample counts and magnitudes in each period.",
                     "budget_limits": {"job_submissions": 1, "cpu_seconds": 30}, "stop_condition": "The observed periods have estimates."},
                    {"op": "add_node", "node": node, "branch": "local:calibration", "capability": METHOD,
                     "inputs": [material["artifact_ref"]], "parameters": {}, "depends_on": [],
                     "question": "What errors are observed in each period?"}]}
    obs, = brief["observations"]
    first, second = obs["observation"]["periods"]
    difference = second["mean_error"] - first["mean_error"]
    rationale = (f"The mean signed errors in {first['period']} and {second['period']} are "
                 f"{first['mean_error']:.3f} and {second['mean_error']:.3f} mV. "
                 f"The observed change is {difference:+.3f} mV for these {first['n'] + second['n']} pairs.")
    return {"mode": "amend", "rationale": rationale, "evidence_refs": [obs["artifact_ref"]],
            "reconsider_when": [{"event": "goal_changed", "subject_ref": "run"}],
            "ops": [{"op": "close_run", "reason": rationale, "claim_refs": [],
                    "unresolved": ["These descriptive observations do not establish a cause of the period difference."]}]}


def effects(state, rid):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        def rows(sql):
            return [dict(row) for row in db.execute(sql, (rid,))]
        return {
            "graph_versions": rows("SELECT version,sha256,parent_sha,decision_ref FROM v6_graph_versions WHERE run_id=? ORDER BY version"),
            "decisions": rows("SELECT task_id,thread_id,turn_id,call_id,decision_ref,request_json,receipt_json FROM v6_decisions WHERE run_id=? ORDER BY rowid"),
            "nodes": rows("SELECT node_id,exec_key,operation_key,status,job_id,result_ref FROM v6_nodes WHERE run_id=?"),
            "jobs": rows("SELECT job_id,capability_id,status,idempotency_key,reservation_ref,measured_json FROM jobs WHERE run_id=?"),
            "outputs": rows("SELECT artifact_ref,job_id,sha256,namespace,rel_path FROM artifacts WHERE run_id=? AND job_id IS NOT NULL"),
            "turn_usage": rows("SELECT thread_id,turn_id,confidence,settled_tokens,budget_status FROM p0_meter_native_turns WHERE run_id=? ORDER BY sequence"),
        }


def check_run(out, state, final, before, requests):
    job, = before["jobs"]
    node, = before["nodes"]
    assert job["status"] == node["status"] == "SUCCEEDED" and job["job_id"] == node["job_id"]
    assert job["capability_id"] == METHOD.rsplit("@", 1)[0]
    measured = json.loads(job["measured_json"])
    assert measured["cpu_seconds"] >= 0 and job["reservation_ref"]
    output, = (row for row in before["outputs"] if row["artifact_ref"] == node["result_ref"])
    raw = state / "artifacts" / output["namespace"] / output["rel_path"]
    assert archive.sha256(raw) == output["sha256"] and output["job_id"] == job["job_id"]
    shutil.copyfile(raw, out / "observation.json")
    result = archive.read(raw)
    independent = independent_result(EXAMPLE / "measurements.csv", result)
    obs, = requests[1]["visible"]["observations"]
    assert obs["source_job_id"] == job["job_id"] and obs["sha256"] == output["sha256"]
    assert obs["observation"] == {k: v for k, v in result.items() if k != "error_sign_counts"}
    assert requests[1]["decision"]["evidence_refs"] == [node["result_ref"]]
    decisions = before["decisions"]
    assert len(decisions) == len({d["thread_id"] for d in decisions}) == 2
    assert len({d["turn_id"] for d in decisions}) == 2
    for call, d in zip(requests, decisions):
        assert call["call_id"] == d["call_id"] and call["decision"] == json.loads(d["request_json"])["decision"]
    first, last = before["graph_versions"]
    assert first["sha256"] == last["parent_sha"] and last["sha256"] == final["graph_head"]["sha256"]
    assert [r["decision_ref"] for r in before["graph_versions"]] == [d["decision_ref"] for d in decisions]
    turns = before["turn_usage"]
    assert len(turns) == 2 and all(t["budget_status"] == "SETTLED" and t["settled_tokens"] == 35 for t in turns)
    assert final["resources"]["lines"]["job_submissions"]["booked"] == 1
    outcome = archive.read(state / "jobs" / job["job_id"] / "outcome.json")
    assert outcome["started"] and outcome["exit_code"] == 0 and outcome["tool_pid"] > 0
    archive.save(out / "job-outcome.json", outcome)
    return independent


def run(work, out, binary):
    release = work / "release"
    # Installing a plugin writes its deployment's catalog. Give this case an ordinary private installation.
    shutil.copytree(ROOT / "gx-h", release / "gx-h",
                    ignore=shutil.ignore_patterns("var", "tests", "__pycache__", ".venv", ".git"))
    shutil.copytree(ROOT / "bin", release / "bin")
    pin = archive.read(EXAMPLE / "package-pin.json")
    pinned_binary = archive.read(ROOT / "release-assets/lock.json")["linux_x64"]["binary_sha256"]
    assert archive.sha256(binary) == pinned_binary
    requests = []
    commands = []
    state = work / "state"

    def respond(body, ident):
        _, tools = wire_catalog(body)
        brief = brief_from(body)
        method, = brief["capabilities"]
        assert method["capability_version"] == METHOD
        assert method["plugin"] == {key: pin[key] for key in ("id", "version", "sha256")}, method
        allowed = {"gx", "gx_task_context", "gx_read_artifact", "gx_stage_claim", "gx_decide_revision", "gx_delegate_branch"}
        assert "gx_decide_revision" in tools and tools <= allowed, tools
        request = decision(brief)
        call_id = "call-" + ident
        requests.append({"call_id": call_id, "catalog": sorted(tools), "visible": brief, "decision": request})
        archive.save(out / "model-inputs.json", requests)
        assert len(requests) <= 2
        return {"type": "function_call", "namespace": "gx", "name": "gx_decide_revision", "call_id": call_id,
                "arguments": json.dumps({"decision": request})}, {"tool": "gx_decide_revision"}

    with responses.local_responses(out / "requests.jsonl") as peer, patch.object(responses, "fixture_action", respond):
        env = fixture_env(work, peer, str(binary))
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]

        def cli(arguments, *, stateful=False, offline=False):
            action, *rest = map(str, arguments)
            argv = [sys.executable, "-B", str(release / "bin/gx-p0"), action]
            if stateful:
                argv.extend(["--state", str(state)])
            proc = subprocess.run([*argv, *rest], cwd=work, env={**env, **({"GX_CODEX_OFFLINE": "1"} if offline else {})},
                                  text=True, capture_output=True, timeout=90)
            commands.append({"argv": [*argv, *rest], "exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
            archive.save(out / "commands.json", commands)
            if proc.returncode:
                raise RuntimeError(f"{action} failed ({proc.returncode}): {(proc.stderr or proc.stdout)[-1200:]}")
            return json.loads(proc.stdout)

        digest = cli(["plugin", "digest", PACKAGE])["sha256"]
        assert digest == pin["sha256"]
        installed = cli(["plugin", "install", PACKAGE, "--sha256", digest])
        assert installed["sha256"] == digest
        verified = cli(["plugin", "verify", pin["id"] + "@" + pin["version"]])
        assert verified["failures"] == {} and verified["plugins"][0]["sha256"] == digest
        started = cli(["start", "--goal", (EXAMPLE / "goal.md").read_text().strip(), "--goal-config", EXAMPLE / "grant.json",
                       "--run-key", "csv-calibration"], stateful=True)
        rid = started["run_id"]
        assert started["graph_head"] is None and started["mode"] == "goal" and peer.request_count == 0
        try:
            final = cli(["run", "--run-id", rid, "--timeout", "45"], stateful=True)
            before = effects(state, rid)
            assert final["status"] == "STOPPED" and final["goal_status"] == "CLOSED", (final["status"], peer.rows)
            reopened = cli(["resume", "--run-id", rid, "--timeout", "3"], stateful=True, offline=True)
            assert effects(state, rid) == before and peer.request_count == len(requests) == 2
            assert reopened["status"] == final["status"] and reopened["graph_head"] == final["graph_head"]
            assert reopened["resources"] == final["resources"]
            independent = check_run(out, state, final, before, requests)
            return {"passed": True, "decision_source": "scripted loopback Responses on official Codex",
                    "run_id": rid, "plugin": installed, "host_binary_sha256": pinned_binary, "run_status": final["status"],
                    "goal_status": final["goal_status"], "graph_head": final["graph_head"], "independent_check": independent,
                    "resources": final["resources"], "paid_requests": 0, "cold_extra_jobs": 0, "cold_extra_requests": 0, **before}
        finally:
            frozen_state(state, out)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--binary", type=Path, required=True)
    args = parser.parse_args()
    destination = args.out.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="gx-v61-csv-") as directory:
        receipt = run(Path(directory), destination, args.binary.resolve())
    archive.save(destination / "result.json", receipt)
