"""Replay the public V6.2B decisions over the pinned official host and local JobService."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "acceptance/v6/engineering"))
from official import MARKER, archive, fixture_env, frozen_state, responses, wire_catalog
from native_probe_peer import declared_calls
sys.path.insert(0, str(ROOT / "acceptance/v61/adaptive"))
from study_score import geometry_oracle
from peer import GOAL, INVENTORY, METHOD, SURVEY, response
from inspect_run import digest, snapshot

ARMS = ("quick", "medium", "change", "maintain")
SURVEY_PACKAGE = "org.gx.research-survey@1.0.0"
BINARY = ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
RECEIPT_TYPE = "Public scripted official Codex App Server, local Responses, genuine JobService CPU subprocesses"
CARD = ROOT / "examples/plugins/v62_public_materials/workflows.json"


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def source_identity():
    def git(*args):
        return subprocess.check_output(["git", "-C", ROOT, *args])
    paths = git("ls-files", "-co", "--exclude-standard", "-z", "--", "bin/gx-p0", "gx-h/gx_core", "gx-h/contracts",
                "gx-h/plugins/installed", "gx-h/config", "gx-h/adapters/scientific/tl1a_geometry",
                "acceptance/e2e_grid.py", "acceptance/e2e_responses.py", "acceptance/native_probe_peer.py",
                "acceptance/v6/reviewer/independent_geometry.py", "acceptance/v61/adaptive/study_score.py",
                "acceptance/v6/engineering/official.py", "acceptance/v6/engineering/public_cli.py",
                "acceptance/v62b/engineering").split(b"\0")
    h = hashlib.sha256()
    for rel in sorted(set(p for p in paths if p)):
        path = ROOT / os.fsdecode(rel)
        if rel.startswith(b"acceptance/") and path.suffix != ".py":
            continue
        if path.is_file() and (path.suffix in {".py", ".json", ".sql", ".toml", ".md"} or path.name == "gx-p0"):
            h.update(rel + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return {"head": git("rev-parse", "HEAD").decode().strip(),
            "runtime_and_acceptance_sha256": h.hexdigest()}


def grant(arm):
    original = ROOT / "acceptance/v61/adaptive/study-config.json"
    data = json.loads(original.read_bytes())
    for item in data["input_files"]:
        source = (original.parent / item["path"]).resolve()
        assert archive.sha256(source) == item["sha256"]
        item["path"] = str(source)
    data.update(project="v62b-public-coordinate-engineering", capabilities=[METHOD],
                plugins=["org.gx.protein@0.2.0"], research_effort={
                    "quick": "quick", "medium": "medium", "change": "deep", "maintain": "deep"}[arm])
    if arm == "quick":
        data.pop("research_effort")
    else:
        data["plugins"].append(SURVEY_PACKAGE)
        data["input_files"].append({"role": "workflow_spec", "path": str(CARD), "sha256": archive.sha256(CARD)})
        data["research_modules"] = [INVENTORY + "@1", SURVEY + "@1"]
        if data["research_effort"] == "deep":
            data["research_modules"] += ["offline_literature_mechanism@1", "hypothesis_space@1"]
    limits = {"job_submissions": 8, "api_calls": 12, "tokens": 1000000, "cpu_seconds": 300,
              "gpu_jobs": 0, "gpu_minutes": 0, "worker_tasks": 24, "wall_hours": 1}
    data.update(run_budget=limits, workspace_budget=limits.copy(), turn_token_budget=65536)
    return data


def replay_report(slot, rid, wire):
    """Redeliver the exact captured terminal App Server call to the cold GX receiver."""
    from gx_core.native_release import native_state
    from gx_core.p0.roots import Roots
    from gx_core.persistence import store
    saved, = [e for e in wire if e["tool"] == "gx_submit_module_report"]
    with native_state(slot / "state"), closing(store.open_store(slot / "state/gx.db")) as conn:
        original, = conn.execute("""SELECT c.* FROM p0_root_calls c JOIN p0_roots r USING(thread_id)
                                    WHERE r.run_id=? AND c.call_id=?""", (rid, saved["call_id"]))
        receiver = Roots(conn, run_id=rid, cwd=slot / "state")
        exact = {"threadId": original["thread_id"], "turnId": original["turn_id"], "callId": saved["call_id"],
                 "namespace": saved["namespace"], "tool": saved["tool"], "arguments": saved["args"]}
        receipt = json.loads(original["result_json"])
        assert receipt["report_ref"] and receiver._call(exact) == receipt and receiver._call(exact) == receipt
        return {"redeliveries": 2, "thread_id": original["thread_id"], "turn_id": original["turn_id"],
                "call_id": saved["call_id"], "report_ref": receipt["report_ref"],
                "original_receipt_sha256": digest(original["result_json"].encode())}


def run_arm(slot, binary, arm):
    slot.mkdir(mode=0o700)
    raw = slot / "raw"
    raw.mkdir()
    data = grant(arm)
    archive.save(slot / "grant.json", data)
    wire, commands = [], []
    # This sole intervention affects the MODULE's pilot Job. The Lead cannot inspect the selected arm.
    pilot_mask = {"change": "ordered_backbone", "maintain": "common_ca"}.get(arm)
    def action(body, ident):
        if len(wire) >= 40:
            raise RuntimeError("Public-script request ceiling reached")
        call, item = response(body, ident, pilot_mask, MARKER, responses.message_texts, responses.output_json,
                              wire_catalog, declared_calls)
        wire.append({"call_id": call["call_id"], "utc": now(), "monotonic": time.monotonic(), **item})
        archive.save(raw / "wire.json", wire)
        return call, {"tool": item["tool"], "scope": item["brief"]["scope"]}

    with responses.local_responses(raw / "requests.jsonl") as peer, patch.object(responses, "fixture_action", action):
        env = fixture_env(slot, peer, str(binary))
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]
        assert env["GX_P0_TEST_PROVIDER"] == "gx_loopback"
        assert not set(env) & {"OPENAI_API_KEY", "OPENAI_ACCESS_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"}
        assert not (slot / "codex/auth.json").exists()

        def cli(action, *args, offline=False):
            argv = [sys.executable, "-B", str(ROOT / "bin/gx-p0"), action, "--state", str(slot / "state"), *map(str,args)]
            proc = subprocess.run(argv, cwd=slot, env={**env, **({"GX_CODEX_OFFLINE":"1"} if offline else {})},
                                  capture_output=True, text=True, timeout=120)
            commands.append({"argv": argv, "exit": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr})
            archive.save(raw / "commands.json", commands)
            if proc.returncode:
                errors = [r["error"] for r in peer.rows if "error" in r]
                raise RuntimeError(f"{action} exited {proc.returncode}: {(proc.stderr or proc.stdout)[-900:]}; {errors[:3]}")
            return json.loads(proc.stdout)

        entry = cli("start", "--goal", GOAL, "--goal-config", slot / "grant.json", "--run-key", "coordinate-coverage")
        rid = entry["run_id"]
        assert entry["mode"] == "goal" and entry["graph_head"] is None and not peer.request_count
        start = time.monotonic()
        try:
            final = cli("run", "--run-id", rid, "--timeout", "75")
            wall = time.monotonic() - start
            assert final["status"] == "STOPPED" and final["goal_status"] == "CLOSED", (final["status"], peer.rows[-3:])
            before = snapshot(slot, rid, arm, wire, geometry_oracle)
            calls = peer.request_count
            replay = replay_report(slot, rid, wire) if before["module_reports"] else {"redeliveries": 0}
            cold = cli("resume", "--run-id", rid, "--timeout", "5", offline=True)
            assert before == snapshot(slot, rid, arm, wire, geometry_oracle)
            assert final["graph_head"] == cold["graph_head"] and final["resources"] == cold["resources"]
            assert len(wire) == len(peer.rows) == peer.request_count == calls
            totals = {}
            for role in ("run", "module"):
                turns = [t for t in before["turns"] if (role == "run") == (t["scope"] == "run")]
                totals[role] = {
                    "native_turns": len(turns), "responses": sum(t["response_requests"] for t in turns),
                    "delivered_frozen_brief_bytes": sum(t["frozen_brief_utf8_bytes"] for t in turns),
                    "fixture_tokens": {k: sum(t["fixture_usage"][k] for t in turns) for k in
                                       ("input_tokens","output_tokens","cached_input_tokens","total_tokens")},
                    "provider_input_tokens": None, "provider_output_tokens": None, "provider_cache_tokens": None,
                }
            summary = {"status": "PASS", "case": arm, "decision_source": RECEIPT_TYPE, **before,
                       "run_wall_seconds": round(wall, 6), "role_totals": totals,
                       "paid_model_calls": 0, "provider_billed_usd": None, "resources": final["resources"],
                       "scientific_model_result":"NOT_RUN",
                       "cold_resume": {"additional_requests": 0, "additional_jobs": 0, "new_wakes": 0,
                                       "new_claims": 0, "graph_head": cold["graph_head"]},
                       "report_redelivery": replay, "grant_sha256": archive.sha256(slot / "grant.json")}
            archive.save(slot / "summary.json", summary)
            return summary
        finally:
            frozen_state(slot / "state", raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--binary", type=Path, default=BINARY)
    parser.add_argument("--arm", choices=("all", *ARMS), default="all")
    args = parser.parse_args()
    work, binary = args.work.resolve(), args.binary.resolve()
    if work.exists() or work.is_relative_to(ROOT):
        parser.error("Provide a fresh output directory outside the source checkout.")
    work.mkdir(parents=True, mode=0o700)
    pin = archive.read(ROOT / "release-assets/lock.json")["linux_x64"]["binary_sha256"]
    assert archive.sha256(binary) == pin
    result = {"status": "FAIL", "started_utc": now(), "source": source_identity(), "decision_source": RECEIPT_TYPE,
              "official_binary_sha256": pin, "paid_model_calls": 0, "scientific_model_result": "NOT_RUN", "arms": {}}
    try:
        for arm in (ARMS if args.arm == "all" else [args.arm]):
            item = run_arm(work / arm, binary, arm)
            result["arms"][arm] = {"status": item["status"], "receipt": f"{arm}/summary.json",
                "sha256": archive.sha256(work / arm / "summary.json"), "run_id": item["run_id"], "role_totals": item["role_totals"],
                "jobs": [{k:j[k] for k in ("entry","mask","selected_positions","observed_positions","selected_atoms","job_id")} for j in item["jobs"]],
                "round_wakes": len(item["round_wakes"]), "maturity": (item["claim"]["maturity"] or {}).get("stage"),
                "report": [{k:r[k] for k in ("ref","sha256","action","decision_ref","followup_job")} for r in item["module_reports"]],
                "wall_seconds": item["run_wall_seconds"], "cold_replay": item["report_redelivery"]}
            archive.save(work / "result.json", result)
        if args.arm == "all":
            left, right = [archive.read(work / arm / "summary.json") for arm in ("change", "maintain")]
            assert left["grant_sha256"] == right["grant_sha256"]
            changed, kept = [next(j for j in s["jobs"] if j["entry"] == "2O0O") for s in (left, right)]
            assert changed["input_sha256"] == kept["input_sha256"] and changed["sha256"] != kept["sha256"]
            assert changed["selected_atoms"] < kept["selected_atoms"]
            starting_inputs = []
            for arm in ("change", "maintain"):
                delivery = archive.read(work / arm / "raw/wire.json")[0]
                first = delivery["brief"]
                assert first["goal"] == GOAL and not first["observations"] and not first["module_reports"] and not first["nodes"]
                starting_inputs.append({
                    "goal": first["goal"], "materials": [(m["role"], m["name"], m["sha256"]) for m in first["materials"]],
                    "scope": first["scope"], "research_effort": first["research_effort"], "method_cards": first["capabilities"],
                    "module_cards": first["module_catalog"], "budget": first["budget"], "tools": delivery["tools"],
                    "model": delivery["model"], "model_reasoning_effort": delivery["model_reasoning_effort"]})
            assert starting_inputs[0] == starting_inputs[1]
            result["public_pair"] = {
                "n": 2, "goal_sha256": digest(GOAL.encode()), "identical_grant_sha256": left["grant_sha256"],
                "initial_scientific_inputs_sha256": digest(json.dumps(starting_inputs[0], sort_keys=True).encode()),
                "manipulation": "Only the scripted inventory worker's pilot mask varied. The Lead read its published, measured result.",
                "change": 1, "maintain": 1, "provider_runs": 0}
        result["finished_source"] = source_identity()
        assert result["source"]["runtime_and_acceptance_sha256"] == result["finished_source"]["runtime_and_acceptance_sha256"], (
            "Release runtime or replay script changed during the trial; run the frozen candidate again.")
        result["status"] = "PASS"
    except BaseException as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result["finished_utc"] = now()
        archive.save(work / "result.json", result)
    print(json.dumps({"status": result["status"], "receipt": str(work / "result.json")}))


if __name__ == "__main__":
    main()
