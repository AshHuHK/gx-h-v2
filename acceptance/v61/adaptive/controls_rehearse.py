#!/usr/bin/env python3
"""Exercise one whole blinded block with an official host and an explicitly scripted neutral policy."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

from controls import CASE_IDS, HERE, REPO, V6, design, digest, meter, paths, read, trial, verify, write
from controls_score import check_impl, numerical_endpoint, original_verifier
from project_window import Archive, resolve_window
from results import one

sys.path.insert(0, str(V6 / "engineering"))
import official


def fixture_call(body, ident):
    brief = official.brief_from(body)
    refs = [obs["artifact_ref"] for obs in brief["observations"] if obs.get("artifact_ref")]
    if not refs:
        raise RuntimeError("Scripted peer did not receive a measurement")
    decision = {"mode": "resolve", "rationale": "The offline rehearsal acknowledges the supplied observations.",
                "evidence_refs": refs,
                "reconsider_when": [{"event": "goal_changed", "subject_ref": "run"}], "ops": []}
    return {"type": "function_call", "namespace": "gx", "name": "gx_decide_revision", "call_id": "call-" + ident,
            "arguments": json.dumps({"decision": decision})}, {"tool": "gx_decide_revision"}


def replay_history(custody):
    result = {}
    for batch in ("first", "repeat"):
        bundle = V6 / "controls/runs" / batch / "bundle"
        report = read(V6 / "controls/runs" / batch / "results.json")
        header = read(bundle / "bundle.json")
        suite = read(V6 / "reviewer/suite.json")
        anchor = custody / f"controls-{batch}.anchor.json"
        if read(anchor)["bundle_sha256"] != verify.sha((bundle / "bundle.json").read_bytes()):
            raise ValueError("Original controls export differs from the archived reviewer anchor")
        results = []
        for row in header["runs"]:
            packet = verify.document(bundle, row["packet"])
            turn = verify.document(bundle, row["turn"])
            args = verify.document(bundle, turn["tool_arguments"])
            if isinstance(args, str): args = json.loads(args)
            view = verify.document(bundle, turn["presented_evidence"])
            proof, = [item for item in report["worlds"] if item["run_id"] == row["run_id"]]
            endpoint = numerical_endpoint(packet["packet_id"], packet, proof, verify.document(bundle, row["before"]),
                                         verify.document(bundle, row["settled"]), args, view["evidence_map"])
            product, _ = check_impl(verify, bundle, row, suite)
            kit, _ = check_impl(original_verifier(), bundle, row, suite)
            results.append({"case": packet["packet_id"], "run_id": row["run_id"],
                            "original_kit": kit, "product": product, "structured_endpoint": endpoint["status"]})
        result[batch] = {"anchor": read(anchor), "source": "historical V6 published windows", "trials": results}
    return result


def loop_slot(work, row, binary):
    world, review = paths(work, row)
    existing = review / "loopback-result.json"
    if existing.exists():
        return read(existing)
    if row["block"] != "block-1" and (review / "seed.json").exists():
        return read(review / "seed.json")
    with official.responses.local_responses(review / "loopback-transactions.jsonl") as server, \
            patch.object(official.responses, "fixture_action", fixture_call):
        env = official.fixture_env(world, server, str(binary.resolve()))
        env["PATH"] = str(REPO / ".venv/bin") + os.pathsep + env["PATH"]
        trial.seed_world(world, review, env)
        if row["block"] != "block-1":
            return read(review / "seed.json")
        trial.execute_world(world, review, env, 60)
    packet = read(review / "intervention.json")
    suite = design()[1]["suite"]
    dest = review / "loopback-evidence"
    dest.mkdir()
    native = resolve_window(Archive(dest), "controls", review / "window.json", registered_packet=packet)
    product, _ = check_impl(verify, dest, native, suite)
    kit, _ = check_impl(original_verifier(), dest, native, suite)
    source = one(review)
    t = verify.document(dest, native["turn"])
    args = verify.document(dest, t["tool_arguments"])
    if isinstance(args, str): args = json.loads(args)
    v = verify.document(dest, t["presented_evidence"])
    endpoint = numerical_endpoint(packet["packet_id"], packet, source, verify.document(dest, native["before"]),
                                 verify.document(dest, native["settled"]), args, v["evidence_map"])
    expected = "PASS" if packet["packet_id"] == "p63" else "FAIL"
    if not (endpoint["status"] == expected and kit["status"] == product["status"] == "PASS"):
        raise ValueError(f"Rehearsal did not distinguish deliberately indiscriminate actions: {kit} {product} {endpoint['status']}")
    raw = verify.stable({"version": 6, "mode": "controls", "runs": [native]})
    (dest / "bundle.json").write_bytes(raw)
    result = {"case": packet["packet_id"], "run_id": native["run_id"], "native_thread": t["thread_id"],
              "native_turn": t["turn_id"], "decision_id": t["decision_id"], "bundle_sha": verify.sha(raw),
              "endpoint": endpoint["status"], "expected_scripted_endpoint": expected,
              "conditions_passed": True, "kit": kit, "product": product, "scripted_provider_requests": server.request_count,
              "host_provider": verify.document(dest, native["profile"])["provider"],
              "protected_jobs": source["jobs"], "crypto_material": "no OAuth credential supplied to loopback"}
    write(existing, result)
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work", required=True, type=Path)
    p.add_argument("--binary", required=True, type=Path)
    p.add_argument("--custody", type=Path)
    p.add_argument("--out", type=Path)
    p.add_argument("--slot")
    args = p.parse_args()
    active = {name for _, name in socket.if_nameindex()
              if int((Path("/sys/class/net") / name / "flags").read_text(), 16) & 1}
    if active != {"lo"} or len(Path("/proc/net/route").read_text().splitlines()) > 1:
        p.error("Run using acceptance/phase2a/regression/loopback.py in the offline network namespace")
    work = args.work.resolve()
    plan = read(work / "preregistered.json")
    if not plan.get("rehearsal") or not (work / "rehearsal-only").exists():
        raise ValueError("Prepare a rehearsal-only cohort with --rehearsal")
    rows = plan["trials"]
    if args.slot:
        loop_slot(work, next(r for r in rows if r["slot"] == args.slot), args.binary); return
    if not args.out or not args.custody: p.error("Supply the review anchor directory and aggregate receipt path")
    def child(row):
        cmd = [sys.executable, "-B", __file__, "--work", work, "--binary", args.binary, "--slot", row["slot"]]
        subprocess.run(cmd, check=True, timeout=150)
        return read(paths(work, row)[1] / "loopback-result.json") if row["block"] == "block-1" else None
    with ThreadPoolExecutor(max_workers=5) as pool:
        witnesses = [r for r in pool.map(child, rows) if r]
    seeded = [read(paths(work, row)[1] / "seed.json") for row in plan["trials"]]
    assert len(seeded) == len({s["run_id"] for s in seeded}) == len({s["protected_job"]["job_id"] for s in seeded}) == 25
    result = {"status": "PASS", "source_checkout": plan["source"]["git_head"], "source": plan["source"],
              "design_digest": plan["design_digest"], "registered_utc": plan["registered_utc"],
              "classes": ["HISTORICAL_REPLAY", "SCRIPTED", "OFFICIAL_HOST_LOOPBACK", "REAL_COMPUTE"],
              "replay": replay_history(args.custody), "worlds_seeded": len(seeded),
              "unique_protected_job_ids_at_admission": [s["protected_job"]["job_id"] for s in seeded],
              "loopback_windows": witnesses, "source_work": str(work), "new_paid_model_calls": 0,
              "registered_denominator_for_paid_campaign": {case: 5 for case in CASE_IDS}}
    write(args.out, result)
    print(json.dumps({k:result[k] for k in ("status","classes","worlds_seeded","new_paid_model_calls")}))


if __name__ == "__main__":
    main()
