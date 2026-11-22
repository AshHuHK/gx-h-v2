"""Join the paid host counters and local Job lifetimes to each formal submission."""

import argparse
import hashlib
import json
import sys
from bisect import bisect_right
from contextlib import closing
from datetime import datetime
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent
PERFORMANCE_DIR = OUTPUT_DIR.parent
sys.path.insert(0, str(PERFORMANCE_DIR))

from audit_workload import db_file
from dispatch_phases import ns


def workspace_job_intervals(install_dir):
    intervals = {}
    for wave in range(13):
        database = install_dir / f"state-{wave:02d}" / "gx.db"
        with closing(db_file(database)) as connection:
            jobs = connection.execute("SELECT created_at, finished_at FROM jobs")
            intervals[wave] = [(ns(job["created_at"]), ns(job["finished_at"])) for job in jobs]
    return intervals


def cpu_counters(reading):
    counters = {}
    for line in reading["cgroup_cpu_stat"].splitlines():
        name, value = line.split()
        counters[name] = int(value)
    return counters


def submission_coverage(submission, job_intervals, readings, reading_times, counters):
    start = submission["absolute_markers_ns"]["callback_start"]
    end = submission["absolute_markers_ns"]["callback_end"]
    last_job_end = max(job_end for _, job_end in job_intervals)
    overlapping_jobs = sum(
        max(job_start, start) < min(job_end, end) for job_start, job_end in job_intervals
    )
    result = {
        "call_id": submission["call_id"],
        "wave": submission["wave"],
        "lane": submission["lane"],
        "role": submission["role"],
        "outcome": submission["outcome"],
        "total_ms": submission["total_ms"],
        "overlapping_workspace_jobs": overlapping_jobs,
        "seconds_after_last_workspace_job": round((start - last_job_end) / 1_000_000_000, 6),
    }

    next_index = bisect_right(reading_times, start / 1_000_000_000)
    previous_index = next_index - 1
    if (
        previous_index >= 0
        and next_index < len(reading_times)
        and end / 1_000_000_000 <= reading_times[next_index]
    ):
        before = counters[previous_index]
        after = counters[next_index]
        result.update(
            host_sample_bounds_utc=[readings[previous_index]["at"], readings[next_index]["at"]],
            cgroup_throttle_count_delta=after["nr_throttled"] - before["nr_throttled"],
            cgroup_throttled_usec_delta=after["throttled_usec"] - before["throttled_usec"],
        )
    return result


def coverage(private_dir):
    source = OUTPUT_DIR / "historical-samples.json"
    submissions = json.loads(source.read_bytes())["samples"]
    results = {}
    arms = (
        ("baseline", "gate0", "v21-gate0-xhigh"),
        ("candidate", "p1", "p1-2476f2b-xhigh"),
    )
    for arm, evidence_dir, install_name in arms:
        host_log = PERFORMANCE_DIR / evidence_dir / "progress-load.jsonl"
        with host_log.open() as stream:
            readings = list(map(json.loads, stream))
        reading_times = [datetime.fromisoformat(reading["at"]).timestamp() for reading in readings]
        counters = [cpu_counters(reading) for reading in readings]
        install_dir = private_dir / install_name
        intervals = workspace_job_intervals(install_dir)
        samples = [
            submission_coverage(submission, intervals[submission["wave"]], readings, reading_times, counters)
            for submission in submissions
            if submission["arm"] == arm
        ]
        results[arm] = {
            "host_samples_sha256": hashlib.sha256(host_log.read_bytes()).hexdigest(),
            "formal_submits": len(samples),
            "zero_workspace_job_overlap": sum(sample["overlapping_workspace_jobs"] == 0 for sample in samples),
            "minimum_seconds_after_last_job": min(sample["seconds_after_last_workspace_job"] for sample in samples),
            "host_bracketed": sum("cgroup_throttle_count_delta" in sample for sample in samples),
            "brackets_with_zero_cgroup_throttling": sum(
                sample.get("cgroup_throttle_count_delta") == 0
                and sample.get("cgroup_throttled_usec_delta") == 0
                for sample in samples
            ),
            "wal_file_sizes_after_acquisition": {
                path.parent.name: path.stat().st_size for path in install_dir.glob("state-*/gx.db-wal")
            },
            "separate_submissions": samples,
        }
    (OUTPUT_DIR / "resource-coverage.json").write_text(json.dumps(results, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--private", type=Path, required=True)
    coverage(parser.parse_args().private)
