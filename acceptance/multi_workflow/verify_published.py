"""Read a published evidence directory and check its archived outputs and exported receipts."""
import argparse
import hashlib
import itertools
import json
from datetime import datetime
from pathlib import Path
import sys
import tarfile


def require(condition, message):
    if not condition:
        raise ValueError(message)


def verify_archive(directory, manifest):
    archive = directory / "published-evidence.tar.gz"
    require(manifest["archive"] == archive.name, "archive name differs from the index")
    with archive.open("rb") as stream:
        require(hashlib.file_digest(stream, "sha256").hexdigest() == manifest["archive_sha256"],
                "archive SHA-256 differs from the index")
    index = {row["artifact_ref"]: row for row in manifest["artifacts"]}
    require(len(index) == len(manifest["artifacts"]), "duplicate artifact reference in the index")
    decisions = {}
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
        names = {member.name for member in members}
        require(len(members) == len(names) == len(index), "archive and index member counts differ")
        for ref, row in index.items():
            name = row["member"]
            require(name == ref + ".json" and name in names, f"missing or mismatched archive member for {ref}")
            member = bundle.getmember(name)
            require(member.isfile() and member.size == row["bytes"], f"invalid member type or size for {ref}")
            with bundle.extractfile(member) as stream:
                if row["kind"] == "research_decision":
                    raw = stream.read()
                    digest = hashlib.sha256(raw).hexdigest()
                    decisions[ref] = json.loads(raw)
                else:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
            require(digest == row["sha256"], f"member SHA-256 mismatch for {ref}")
    return index, decisions


def verify_outputs(result, index):
    refs = set()
    for job in result["jobs"]:
        names = set()
        for output in job["outputs"]:
            name, ref = output["output_name"], output["artifact_ref"]
            require(name and name not in names, f"empty or duplicate output name in {job['job_id']}")
            row = index[ref]
            require(row["job_id"] == job["job_id"] and row["sha256"] == output["sha256"]
                    and row["bytes"] == output["bytes"], f"Job, digest or size mismatch for {name} ({ref})")
            # The result records the native publication location independently of the named output.
            location = output["location"]["rel_path"]
            if location.startswith("files/"):
                require(location.removeprefix("files/") == name, f"publication location differs from {name} ({ref})")
            require(ref not in refs, f"duplicate Job output reference: {ref}")
            names.add(name)
            refs.add(ref)
    require(refs == {ref for ref, row in index.items() if row["job_id"] is not None},
            "Job artifacts in the index and result differ")
    return len(refs)


def verify_closure(result, decisions, index):
    close, final = result["closure"], result["final_decision"]
    require(result["goal_status"] == "CLOSED" and close["goal_version"] == result["goal_version"],
            "closure differs from the reported Goal version or status")
    require(close["requested_event"] < close["committed_event"] < close["closed_event"],
            "closure events are out of order")
    ref = close["decision_ref"]
    require(ref == final["decision_ref"] and ref in decisions, "final decision lacks its archived closure")
    operation = close["operation"]
    encoded = json.dumps(operation, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    require(operation["op"] == "close_run" and operation in decisions[ref]["ops"]
            and hashlib.sha256(encoded).hexdigest() == close["op_digest"], "archived closing operation mismatch")
    require(final["rationale"] == decisions[ref]["rationale"] and final["reason"] == operation["reason"],
            "final closing reason or adopted rationale differs from the archive")
    for module in result["routes"]["modules"]:
        require(module["goal_version"] == close["goal_version"], "module belongs to a different Goal version")
        report = module["report"]
        if report:
            row = index[report["report_ref"]]
            require(row["kind"] == "module_report_bundle" and row["sha256"] == report["report_sha256"]
                    and report["run_id"] == result["run_id"] and report["goal_version"] == close["goal_version"],
                    f"module report differs from its receipt: {report['report_ref']}")


def verify_overlaps(routes):
    windows = []
    for module in routes["modules"]:
        for thread in module["threads"]:
            for turn in thread["turns"]:
                if turn["classification"] != "NATIVE_MODEL":
                    continue
                require(turn["thread_id"] == thread["thread_id"], "turn is attached to another thread")
                if not turn["start"] or not turn["end"]:
                    continue
                start, end = (datetime.fromisoformat(turn[k].replace("Z", "+00:00")) for k in ("start", "end"))
                require(start.tzinfo and end.tzinfo and start <= end, "invalid native turn time bounds")
                windows.append(((module["scope"], turn["thread_id"], turn["turn_id"]), start, end))
    expected = []
    for (left, start, end), (right, other_start, other_end) in itertools.combinations(windows, 2):
        if left[0] != right[0] and left[1] != right[1]:
            seconds = (min(end, other_end) - max(start, other_start)).total_seconds()
            if seconds > 0:
                expected.append((sorted((left, right)), seconds))
    recorded = []
    for row in routes["real_turn_overlaps"]:
        sides = [tuple(row[f"{side}_{field}"] for field in ("scope", "thread", "turn")) for side in ("left", "right")]
        recorded.append((sorted(sides), row["seconds"]))
    require(sorted(expected) == sorted(recorded), "reported model-turn overlaps differ from the time intervals")
    return [seconds for _, seconds in expected]


def verify(directory):
    def read(name):
        return json.loads((directory / name).read_bytes())

    result, manifest, reopen = (read(name) for name in ("result.json", "published-evidence.json", "warm-reopen.json"))
    run_id = result["run_id"]
    require(run_id == manifest["run_id"] == reopen["run_id"], "reopen or archive belongs to another Run")
    index, decisions = verify_archive(directory, manifest)
    outputs = verify_outputs(result, index)
    verify_closure(result, decisions, index)
    overlaps = verify_overlaps(result["routes"])

    before = reopen.get("original_counts", reopen.get("before"))
    after = reopen.get("reopened_counts", reopen.get("after"))
    require(reopen["status"] == "CLOSED" and isinstance(before, dict) and before == after
            and before["jobs"] == len(result["jobs"]) and ("host_turns" in before or "p0_root_deliveries" in before)
            and reopen["added"] == {"jobs": 0, "host_turns": 0}, "closed reopen counts differ")
    invocation = result["invocation"]
    if invocation["action"] == "export_closed":
        require(invocation["added"] == reopen["added"] and invocation["before"] == invocation["after"]
                and invocation["after"]["jobs"] == before["jobs"], "export_closed counters differ")
    return {"run_id": run_id, "goal_version": result["goal_version"], "closed_event": result["closure"]["closed_event"],
            "archive_sha256": manifest["archive_sha256"], "archive_members": len(index), "named_outputs": outputs,
            "module_count": len(result["routes"]["modules"]), "native_turn_overlap_seconds": overlaps,
            "reopen_added": reopen["added"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="directory containing result.json and published-evidence.tar.gz")
    args = parser.parse_args()
    try:
        print(json.dumps(verify(args.directory), indent=2))
    except (OSError, EOFError, ValueError, KeyError, TypeError, tarfile.TarError) as error:
        sys.exit(f"Evidence verification failed: {error}")
