"""Extract original timing markers for formal submissions during the two paid runs."""

import argparse
import gzip
import hashlib
import json
import sys
from collections import defaultdict
from contextlib import closing
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent
PERFORMANCE_DIR = OUTPUT_DIR.parent
sys.path.insert(0, str(PERFORMANCE_DIR))

from audit_workload import db_file, quantile
from dispatch_phases import ns


def artifact_details(connection, workspace, reference):
    artifact = connection.execute(
        "SELECT created_at, sha256, namespace, rel_path FROM artifacts WHERE artifact_ref = ?",
        (reference,),
    ).fetchone()
    publication = connection.execute(
        "SELECT ts FROM events WHERE subject_id = ? AND kind = 'artifact.published'",
        (reference,),
    ).fetchone()
    path = workspace / "artifacts" / artifact["namespace"] / artifact["rel_path"]
    stat = path.stat()
    whole_second_timestamps = (
        stat.st_mtime_ns % 1_000_000_000 == 0 and stat.st_ctime_ns % 1_000_000_000 == 0
    )
    return {
        "sha256": artifact["sha256"],
        "durable_ns": ns(artifact["created_at"]),
        "published_ns": ns(publication["ts"]),
        "whole_second_timestamps": whole_second_timestamps,
    }


def submission_sample(connection, workspace, callback, errors):
    submission = connection.execute(
        "SELECT * FROM submissions WHERE submission_id = ?", (callback["submission_id"],)
    ).fetchone()
    references = [
        *json.loads(submission["artifact_refs_json"]),
        submission["records_artifact_ref"],
    ]
    product, verdict = [
        artifact_details(connection, workspace, reference) for reference in references[:2]
    ]

    # The original SQLite timestamps have millisecond precision.
    callback_start = callback["callback_start_ns"]
    callback_end = callback["callback_end_ns"]
    transitions = connection.execute(
        "SELECT ts, new_state FROM events "
        "WHERE subject_id = ? AND kind = 'task.transition' ORDER BY event_id",
        (callback["task_id"],),
    )
    transition_times = {}
    for transition in transitions:
        timestamp = ns(transition["ts"])
        if callback_start - 1_000_000 <= timestamp <= callback_end + 1_000_000:
            transition_times.setdefault(transition["new_state"], timestamp)
    if not transition_times:
        errors.append((callback["call_id"], "no transitions"))
        return None

    lease = connection.execute(
        "SELECT released_at FROM leases WHERE lease_id = ?", (submission["lease_id"],)
    ).fetchone()
    outcome_time = transition_times[submission["result"]]
    last_lease_or_outbox = max(ns(lease["released_at"]), outcome_time)
    if submission["result"] == "ACCEPTED":
        outbox = connection.execute(
            "SELECT created_at, payload_json FROM outbox WHERE run_id = ?", (callback["run_id"],)
        )
        acceptance_times = [
            ns(event["created_at"])
            for event in outbox
            if json.loads(event["payload_json"]).get("submission_id") == callback["submission_id"]
        ]
        if acceptance_times:
            last_lease_or_outbox = max(last_lease_or_outbox, min(acceptance_times))

    markers = {
        "callback_start": callback_start,
        "product_durable": product["durable_ns"],
        "product_event": product["published_ns"],
        "submitted": transition_times["SUBMITTED"],
        "validating": transition_times["VALIDATING"],
        "verdict_durable": verdict["durable_ns"],
        "verdict_event": verdict["published_ns"],
        "verdict_row": ns(submission["created_at"]),
        "outcome_event": outcome_time,
        "last_lease_or_outbox": last_lease_or_outbox,
        "writer_end": callback["writer_samples"][-1]["end"],
        "callback_end": callback_end,
    }
    phases = {}
    previous_timestamp = callback_start
    for name, timestamp in list(markers.items())[1:]:
        phases[name] = (timestamp - previous_timestamp) / 1_000_000
        previous_timestamp = timestamp
    if min(phases.values()) < -1.02:
        errors.append([callback["call_id"], phases])
        return None

    return {
        "arm": callback["arm"],
        "wave": callback["wave"],
        "lane": callback["lane"],
        "role": callback["role"],
        "attempt": callback["attempt"],
        "outcome": submission["result"],
        "call_id": callback["call_id"],
        "lease_id": submission["lease_id"],
        "submission_id": callback["submission_id"],
        "task_id": callback["task_id"],
        "turn_id": callback["turn_id"],
        "run_id": callback["run_id"],
        "total_ms": callback["callback_ms"],
        "args_bytes": callback["canonical_argument_bytes"],
        "product_bytes": callback["product_bytes"],
        "product_sha256": product["sha256"],
        "phases": phases,
        "fs_resolution_s": product["whole_second_timestamps"] and verdict["whole_second_timestamps"],
        "absolute_markers_ns": markers,
        "published_artifact_refs": references,
        "thread_id": callback["thread_id"],
    }


def extract(private_dir):
    source = PERFORMANCE_DIR / "callback-samples.jsonl.gz"
    samples_by_arm = defaultdict(list)
    samples = []
    errors = []
    with gzip.open(source, "rt") as stream:
        for callback in map(json.loads, stream):
            if callback["tool"] != "gx_submit_product" or not callback.get("submission_id"):
                continue
            install = "p1-2476f2b-xhigh" if callback["arm"] == "candidate" else "v21-gate0-xhigh"
            workspace = private_dir / install / f"state-{callback['wave']:02d}"
            with closing(db_file(workspace / "gx.db")) as connection:
                sample = submission_sample(connection, workspace, callback, errors)
            if sample is not None:
                samples.append(sample)
                samples_by_arm[callback["arm"]].append(sample)

    phase_statistics = {
        arm: {
            name: quantile(sample["phases"][name] for sample in arm_samples)
            for name in arm_samples[0]["phases"]
        }
        for arm, arm_samples in samples_by_arm.items()
    }
    report = {
        "stage": "13 completed paid waves",
        "linked_formal_submissions": len(samples),
        "errors": errors,
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "precision": (
            "GX SQLite timestamps truncate wall clock to milliseconds. "
            "The writer metric emission follows COMMIT and its registered callbacks. "
            "Each duration is elapsed wall time between markers."
        ),
        "phase_statistics_ms": phase_statistics,
        "samples": samples,
    }
    (OUTPUT_DIR / "historical-samples.json").write_text(json.dumps(report, indent=2) + "\n")

    print("JOIN", len(samples), "exceptions", len(errors), str(errors)[:410])
    for arm, arm_samples in samples_by_arm.items():
        coarse_timestamps = sum(sample["fs_resolution_s"] for sample in arm_samples)
        percentiles = {name: stats["p95"] for name, stats in phase_statistics[arm].items()}
        print(arm, "N", len(arm_samples), "seconds coarse FS", coarse_timestamps, "quantiles", percentiles)
        slowest = sorted(arm_samples, key=lambda sample: sample["total_ms"], reverse=True)[:5]
        for sample in slowest:
            longer_phases = {
                name: round(duration, 1)
                for name, duration in sample["phases"].items()
                if duration >= 3
            }
            print(
                arm, sample["wave"], sample["lane"], sample["role"], sample["outcome"],
                round(sample["total_ms"], 1), sample["product_bytes"], longer_phases,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--private", type=Path, required=True)
    extract(parser.parse_args().private)
