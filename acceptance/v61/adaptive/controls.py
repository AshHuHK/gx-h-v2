#!/usr/bin/env python3
"""Preregister, seed and score the V6.1 five-condition native decision experiment."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

HERE = Path(__file__).resolve().parent
V6 = HERE.parents[1] / "v6"
REPO = V6.parents[1]
sys.path[:0] = [str(V6 / "controls"), str(V6), str(REPO / "gx-h")]
import fixture
import live
import run as trial
from controls_score import KIT, score
from gx_core.p0 import profiles, v6_costs
import verify

CASE_IDS = ("p17", "p42", "p63", "h81", "h92")
BLOCKS = 5
TURN_RESERVATION = 65536
TIME_LIMIT = 240
CASE_DIR = HERE / "controls_cases"
read, digest = fixture.read, fixture.digest


def write(path, value):
    tmp = path.with_suffix(".tmp")
    fixture.save(tmp, value)
    tmp.replace(path)


def design():
    _, initial, public = fixture.fixture_objects()
    packets = {p["packet_id"]: p for p in public}
    for case in CASE_IDS[3:]:
        packets[case] = read(CASE_DIR / (case + ".json"))
    suite = {"goal_digest": initial["goal_digest"], "base_digest": digest(initial),
             "packet_digests": {case: digest(packet) for case, packet in packets.items()}}
    paths = [Path(__file__).resolve(), HERE / "controls_score.py", CASE_DIR / "rubric.md", KIT,
             KIT.parent / "reviewer/suite.json", V6 / "live.py", V6 / "verify.py", V6 / "project_window.py",
             V6 / "export.py", V6 / "native_witness.py",
             *(V6 / "controls" / n for n in ("run.py", "fixture.py", "results.py", "review.py")),
             *(V6 / "inputs" / (n + ".json") for n in ("goal", "initial", *CASE_IDS[:3])),
             *(CASE_DIR / (n + ".json") for n in CASE_IDS[3:])]
    return packets, {"schema": "gx.v61.controls/1", "suite": suite, "blocks": BLOCKS, "planned_trials": 25,
                     "planned_per_case": 5, "public": list(CASE_IDS[:3]), "held_out": list(CASE_IDS[3:]),
                     "randomization": "SystemRandom permutation in each five-case block; fresh opaque worlds",
                     "primary_endpoint": "controls_cases/rubric.md", "failure_replacements": 0,
                     "profile": {"provider": "openai", "model": live.MODEL, "effort": "xhigh",
                                 "multi_agent": False, "temperature": "provider default; unavailable numeric value"},
                     "per_world": {**fixture.LIMITS, "turn_token_reservation": TURN_RESERVATION,
                                   "deadline_seconds": TIME_LIMIT},
                     "estimated_tokens_25": [1_300_000, 1_600_000], "provider_usd": None,
                     "hashes": {str(p.relative_to(REPO)): fixture.file_sha(p) for p in paths}}


def paths(work, row):
    return work / "states" / row["block"] / row["slot"], work / "review" / row["block"] / row["slot"]


def plan_for(work, *, freeze=False):
    plan = read(work / "preregistered.json")
    if digest(design()[1]) != plan["design_digest"]:
        raise ValueError("The preregistered packets, rubric or control code changed")
    if freeze and plan["source"] != fixture.setup_source():
        raise ValueError("Frozen product code or Git HEAD changed since preparation")
    return plan


def prepare(work, token_cap, plugin, rehearsal):
    if token_cap < TURN_RESERVATION:
        raise ValueError("Cumulative token cap must accommodate one native reservation")
    if work == REPO or REPO in work.parents:
        raise ValueError("Use an operator-private directory outside the checkout")
    packets, protocol = design()
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    if rehearsal:
        (work / "rehearsal-only").touch()
    trials = []
    for i in range(1, BLOCKS + 1):
        block = f"block-{i}"
        entries = fixture.prepare(work / "states" / block, work / "review" / block, packets=packets.values(),
                                  run_options={"plugins": plugin} if plugin else {})
        for entry in entries:
            trials.append({"block": block, **entry})
    if len({digest(r["visible_initial_sha256"]) for r in trials}) != 1 or len({r["slot"] for r in trials}) != 25:
        raise ValueError("Every trial must have an independent workspace and identical initial materials")
    plan = {"registered_utc": live.now(), "design": protocol, "design_digest": digest(protocol),
            "source": fixture.setup_source(), "cumulative_token_admission_cap": token_cap,
            "source_checkout": str(REPO), "max_provider_calls": 25, "trials": trials,
            "protein_selection": plugin, "rehearsal": rehearsal}
    write(work / "preregistered.json", plan)
    print(json.dumps({"trials": len(trials), "design_sha": plan["design_digest"], "paid_calls": 0}))


def seed(work, plan):
    for row in plan["trials"]:
        world, records = paths(work, row)
        if digest(read(records / "intervention.json")) != row["packet_sha256"]:
            raise ValueError("Stored intervention does not match the predeclared packet")
        if (records / "seed.json").is_file():
            continue
        seed_home = world / "seed-home"
        seed_home.mkdir(mode=0o700, exist_ok=True)
        profile = seed_home / "profile.json"
        write(profile, {"default": {"model": live.MODEL, "provider": "openai", "effort": "xhigh",
                                    "multi_agent": False}})
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "LD_LIBRARY_PATH", "GX_P0_CODEX")
               if k in os.environ}
        if (REPO / ".venv/bin").is_dir():
            env["PATH"] = str(REPO / ".venv/bin") + os.pathsep + env.get("PATH", "")
        env.update(HOME=str(seed_home), GX_P0_PROFILE=str(profile), GX_CODEX_OFFLINE="1",
                   **profiles.proxy_settings())
        trial.seed_world(world, records, env)
    publish_report(work, plan)


def meter(world, records):
    receipt = records / "seed.json"
    if not receipt.is_file():
        return None
    db_path = world / "state/gx.db"
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        return v6_costs.resource_report(db, read(receipt)["run_id"])


def metered_exposure(cost):
    if not cost:
        return 0
    line = cost["lines"]["tokens"]
    return int(line["booked"] + line["reserved"])


def sums(records):
    costs = [r["costs"] for r in records if r.get("costs")]
    return {"calls_booked": sum(c["lines"]["api_calls"]["booked"] for c in costs),
            "tokens_provider_confirmed": sum((c["tokens"] or {}).get("provider_attributed", 0) for c in costs),
            "tokens_estimated": sum((c["tokens"] or {}).get("provisional_booked", 0) for c in costs),
            "tokens_held": sum(c["lines"]["tokens"]["reserved"] for c in costs),
            "unresolved_refs": [ref for c in costs for ref in (c["tokens"] or {}).get("unresolved", [])],
            "possibly_understated": [ref for c in costs for ref in (c["tokens"] or {}).get("possibly_understated", [])],
            "job_submissions": sum(c["lines"]["job_submissions"]["booked"] for c in costs),
            "jobs_recorded": sum(c["jobs_recorded"] for c in costs),
            "submissions_held": sum(c["lines"]["job_submissions"]["reserved"] for c in costs),
            "measured_cpu_seconds": round(sum(c["measured_cpu"]["observed"] for c in costs), 4),
            "provider_usd": None}


def tally(rows):
    counts = {"planned": len(rows), "passed": 0, "failed": 0, "running_or_interrupted": 0, "not_run": 0}
    for r in rows:
        counts[{"PASS": "passed", "FAIL": "failed", "ADMITTED": "running_or_interrupted",
                "NOT RUN": "not_run"}[r["status"]]] += 1
    counts["observed_rate"] = (counts["passed"] / (counts["passed"] + counts["failed"])
                               if counts["passed"] + counts["failed"] else None)
    counts["passes_over_planned"] = f"{counts['passed']}/{counts['planned']}"
    return {**counts, "resources": sums(rows)}


def grouped_verdict(work, block, rows):
    """Run the published, unchanged full verifiers over each completed public triplet."""
    directory = work / "review" / block / "public-triplet"
    if len(rows) != 3 or any(not (work / r["bundle"] / "bundle.json").exists() for r in rows):
        return {"status": "NOT RUN"}
    directory.mkdir(exist_ok=True)
    objects = directory / "objects"
    objects.mkdir(exist_ok=True)
    entries = []
    for row in rows:
        bundle = work / row["bundle"]
        entries.extend(read(bundle / "bundle.json")["runs"])
        for src in (bundle / "objects").iterdir():
            dest = objects / src.name
            if not dest.exists():
                os.link(src, dest)
    raw = verify.stable({"version": 6, "mode": "controls", "runs": entries})
    (directory / "bundle.json").write_bytes(raw)
    anchor = directory.parent / "public-triplet.capture-hash.json"
    write(anchor, {"bundle_sha256": verify.sha(raw)})
    out = {"capture_sha256": verify.sha(raw), "anchor_kind": "capture consistency; same evaluator process"}
    for label, exe in [("original_kit", KIT), ("product", V6 / "verify.py")]:
        proc = subprocess.run([sys.executable, "-B", exe, directory, "--anchor", anchor],
                              capture_output=True, text=True, timeout=30)
        out[label] = {"status": "PASS" if proc.returncode == 0 else "FAIL", "exit": proc.returncode,
                      "detail": (proc.stdout + proc.stderr).strip()}
    return out


def publish_report(work, plan):
    rows = []
    for entry in plan["trials"]:
        world, records = paths(work, entry)
        scored = records / "graded.json"
        if scored.exists():
            grading = read(scored)
        elif (records / "admitted.json").exists():
            grading = {"status": "ADMITTED"}
        else:
            grading = {"status": "NOT RUN"}
        rows.append({**grading, "slot": entry["slot"], "case": entry["packet_id"], "block": entry["block"],
                     "seeded": (records / "seed.json").is_file(), "costs": meter(world, records)})
    identities = [r for r in rows if r.get("turn_id")]
    for key in ("run_id", "thread_id", "turn_id", "decision_id"):
        if len({r[key] for r in identities}) != len(identities):
            raise ValueError("Repeated paid identity: " + key)
    blocks = {}
    for block in sorted({r["block"] for r in rows}):
        members = [r for r in rows if r["block"] == block and r["case"] in CASE_IDS[:3]]
        blocks[block] = grouped_verdict(work, block, members) if all(r.get("bundle") for r in members) else {"status":"NOT RUN"}
    result = {"protocol": plan["design_digest"], "source": plan["source"]["git_head"],
              "recorded_utc": live.now(), "seeded": sum(r["seeded"] for r in rows),
              "per_case": {c: tally([r for r in rows if r["case"] == c]) for c in CASE_IDS},
              "held_out": tally([r for r in rows if r["case"] in CASE_IDS[3:]]),
              "all": tally(rows), "public_triplets": blocks, "worlds": rows,
              "original_full_verifier_heldout": "NOT APPLICABLE: the original full verifier expects the three V6 public packet digests",
              "live_state": "NOT RUN" if not any(r["status"] != "NOT RUN" for r in rows)
                            else "COMPLETE" if all(r["status"] in ("PASS", "FAIL") for r in rows) else "PARTIAL"}
    write(work / "report.json", result)
    print(json.dumps({"seeded": result["seeded"], "live_state": result["live_state"], "all": result["all"]}))


def execute(work, row, plan):
    world, records = paths(work, row)
    # An exclusive marker survives a process loss. Never reopen this slot with a new model call.
    with (records / "admitted.json").open("x") as stream:
        json.dump({"utc": live.now(), "reserved_tokens": TURN_RESERVATION, "protocol": plan["design_digest"]}, stream)
    try:
        env = live.live_environment(world / "native")
        if not live.preflight(env, records):
            raise RuntimeError("Official provider preflight failed; consult preflight.json")
        trial.execute_world(world, records, env, TIME_LIMIT)
        packet = read(records / "intervention.json")
        if digest(packet) != plan["design"]["suite"]["packet_digests"][row["packet_id"]]:
            raise ValueError("Packet changed after registration")
        rng = records / "bundle"
        grade = score(records, packet, plan["design"]["suite"], rng)
        grade["bundle"] = str(rng.relative_to(work))
    except (RuntimeError, ValueError, OSError, KeyError, TypeError, IndexError, sqlite3.Error, subprocess.SubprocessError) as exc:
        grade = {"status": "FAIL", "failure": str(exc)[:400], "error_type": type(exc).__name__}
    write(records / "graded.json", grade)


def run_live(work, plan, parallel):
    # Serialize launch managers. Each GX Run owns its normal token/call quota.
    with (work / "launch.lock").open("a") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for row in plan["trials"]:
            if not (paths(work, row)[1] / "seed.json").is_file():
                raise ValueError("Finish all unpaid seeds before the first scored call")
        with ThreadPoolExecutor(max_workers=parallel) as executor:
            while True:
                remaining, exposure = [], 0
                for row in plan["trials"]:
                    world, records = paths(work, row)
                    if not (records / "admitted.json").exists():
                        remaining.append(row)
                        continue
                    cost = meter(world, records)
                    value = metered_exposure(cost)
                    # Even when admission crashed ahead of a receipt, carry the slot's original exposure.
                    if not (records / "graded.json").exists() or not cost or not cost["lines"]["api_calls"]["booked"]:
                        value = max(value, TURN_RESERVATION)
                    exposure += value
                available = plan["cumulative_token_admission_cap"] - exposure
                wave = remaining[:min(parallel, max(0, available // TURN_RESERVATION))]
                if not wave:
                    break
                futures = [executor.submit(execute, work, r, plan) for r in wave]
                for future in futures:
                    future.result()
                publish_report(work, plan)
        publish_report(work, plan)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("design")
    for cmd in ("prepare", "seed", "run", "report"):
        p = sub.add_parser(cmd)
        p.add_argument("--work", type=Path, required=True)
        if cmd == "prepare":
            p.add_argument("--token-cap", type=int, required=True, help="Approved cumulative admission limit")
            p.add_argument("--plugin", action="append", help="Explicitly installed package pin, if needed")
            p.add_argument("--rehearsal", action="store_true", help="Seed a local scripted provider for offline rehearsal")
        if cmd == "run":
            p.add_argument("--parallel", type=int, default=3, choices=range(1, 11), metavar="1..10")
    args = parser.parse_args()
    if args.cmd == "design":
        print(json.dumps(design()[1], indent=2)); return
    work = args.work.resolve()
    if args.cmd == "prepare":
        prepare(work, args.token_cap, args.plugin, args.rehearsal); return
    if args.cmd == "run" and (work / "rehearsal-only").exists():
        raise ValueError("The paid campaign requires untouched workspaces; this cohort is a rehearsal")
    plan = plan_for(work, freeze=args.cmd != "report")
    if args.cmd == "seed":
        if plan["rehearsal"]:
            parser.error("Use controls_rehearse.py to seed and exercise the same active local provider")
        seed(work, plan)
    elif args.cmd == "report":
        publish_report(work, plan)
    else:
        run_live(work, plan, args.parallel)


if __name__ == "__main__":
    main()
