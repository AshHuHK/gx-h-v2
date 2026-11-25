#!/usr/bin/env python3
"""Seal the V6.2A public acceptance files, or verify a copy offline (no Git required)."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

DIRECTORY = Path(__file__).resolve().parent
ROOT = DIRECTORY.parents[1]
INDEX = "receipts/index.json"
REQUIRED = {"receipts/A1/result.json", "receipts/A1/network.json", "upgrade/result.json",
            *(f"receipts/A{i}.json" for i in range(2, 8))}
RUNTIME = ("gx-h/gx_core", "gx-h/plugins", "gx-h/contracts", "gx-h/config")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_bytes())


def path_at(base, name):
    path = (base / name).resolve()
    require(isinstance(name, str) and not Path(name).is_absolute() and path.is_relative_to(base.resolve()),
            f"Path outside receipt set: {name}")
    return path


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def aggregate(index):
    data = json.dumps(index, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()


def seal(directory, root, source):
    """Ask Git only for the build's tracked runtime file names. The published index embeds the list."""
    require(re.fullmatch("[0-9a-f]{40}", source or ""), "Supply the full tested source commit")
    names = subprocess.check_output(["git", "-C", str(root), "ls-tree", "-r", "--name-only", "-z", source,
                                     "--", *RUNTIME]).decode().rstrip("\0").split("\0")
    require(names and names[0], "Source commit contains no GX runtime")
    runtime = {name: digest(path_at(root, name)) for name in names}
    candidates = (p for part in ("receipts", "prerequisites", "upgrade", "regression")
                  for p in (directory / part).rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    files = {p.relative_to(directory).as_posix(): digest(p) for p in sorted(candidates)
             if p.relative_to(directory).as_posix() != INDEX}
    return {"format": "gx.v62a.acceptance/1", "source": source, "files": files, "runtime_files": runtime,
            "external_evidence": "A1 raw native transcripts and state are held outside the public tree. "
                                 "Their per-file SHA-256 values are recorded in receipts/A1/result.json."}


def same_hashes(base, recorded):
    for name, expected in recorded.items():
        require(re.fullmatch("[0-9a-f]{64}", expected) and digest(path_at(base, name)) == expected,
                f"File digest mismatch: {name}")


def report_checks(directory, root, index):
    source = index["source"]
    require(index["format"] == "gx.v62a.acceptance/1" and re.fullmatch("[0-9a-f]{40}", source),
            "Unknown receipt format or invalid source commit")
    require(REQUIRED <= index["files"].keys() and INDEX not in index["files"], "Missing required receipt")
    same_hashes(directory, index["files"])
    runtime = index["runtime_files"]
    require(runtime and all(any(n.startswith(prefix + "/") for prefix in RUNTIME) for n in runtime),
            "Runtime file list is incomplete or out of scope")
    same_hashes(root, runtime)
    runtime_digest = aggregate(runtime)

    a1 = read(directory / "receipts/A1/result.json")
    net = read(directory / "receipts/A1/network.json")
    require(a1["case"] == "A1" and a1["status"] == "PASS" and a1["source_head"] == source
            and a1["paid_model_calls"] == 0, "A1 source, status or accounting differs")
    require(net["exit_code"] == 0 and net["outer_namespace"] != net["fixture_namespace"]
            and not net["ipv4_routes"], "A1 network isolation evidence differs")
    same_hashes(directory, a1["script_sha256"])
    require(set(a1["arms"]) == {"module", "quick"}, "A1 requires both matched arms")
    outputs = []
    for name, arm in a1["arms"].items():
        require(arm["paid_model_calls"] == 0 and arm["provider_tokens"] is None and
                arm["cold_resume"]["additional_jobs"] == arm["cold_resume"]["additional_requests"] == 0,
                f"A1 {name} cost or resume differs")
        pairs = {(j["input_sha256"], j["sha256"]) for j in arm["jobs"]}
        require(len(pairs) == len(arm["jobs"]) == 2 and arm["turns"], f"A1 {name} lacks two measured outputs")
        outputs.append(pairs)
        for rel, expected in arm["raw_files_sha256"].items():
            path = path_at(directory / "receipts/A1", rel)
            require(rel.startswith(name + "/raw/") and re.fullmatch("[0-9a-f]{64}", expected),
                    f"Invalid raw A1 reference {rel}")
            if path.exists():
                require(digest(path) == expected, f"A1 raw mismatch: {rel}")
    require(outputs[0] == outputs[1] and a1["arms"]["module"]["report"]["author"]["kind"] == "MODULE",
            "A1 intervention failed to deliver the matched outputs and module-authored report")

    cases = {}
    for number in range(2, 8):
        key = f"A{number}"
        case = cases[key] = read(directory / f"receipts/{key}.json")
        provenance = case["observed_sources"]
        require(case["case"] == key and case["status"] == "PASS" and case["paid_model_calls"] == 0
                and case["provider_tokens"] is None and provenance["commit"] == source
                and provenance["runtime_sources_sha256"] == runtime_digest
                and provenance["runtime_file_count"] == len(runtime), f"{key} source, status or accounting differs")
        same_hashes(directory, provenance["acceptance_sources_sha256"])
    require({"lead_before_read", "lead_after_read", "cancel_after_read"} <= cases["A2"]["schedules"].keys(),
            "A2 lacks controlled lead/worker interleavings")
    require(len({r["module_instance_id"] for r in cases["A3"]["reports"]}) == 2
            and cases["A3"]["cas_conflict"], "A3 lacks independent reports and a CAS conflict")
    a4, a5, a6, a7 = (cases[f"A{i}"] for i in range(4, 8))
    require(a4["old_actor"]["scope"] == a4["successor"]["scope"]
            and a4["old_actor"]["lease_id"] != a4["successor"]["lease_id"]
            and len(a4["reports"]) == 1, "A4 did not continue the module on a new lease")
    require(a5["crash_exit"] == 83 and a5["old_lease"] != a5["new_lease"]
            and len(a5["confirmed_tool_executions"]) == len(a5["jobs_after"]) == len(a5["reports"]) == 1
            and a5["verified_reuse"]["original_job"] == a5["jobs_after"][0]["job_id"], "A5 recovery identity differs")
    exhausted, large = a6["exhausted"], a6["large_report"]
    require(exhausted["author"]["kind"] == "GX" and exhausted["remaining_module_model_tokens"] == 0
            and large["projection_bytes"] <= large["configured_limits"]["report_bytes"] < large["full_bytes"]
            and large["whole_lead_brief_bytes"] <= large["configured_limits"]["brief_bytes"]
            and a6["conditional_dispatch"]["held_after_review"], "A6 budget, projection or dispatch differs")
    require(a7["report_receipt"]["module_instance_id"] == a7["delegation_receipt"]["module_instance_id"]
            and a7["refusals"]["same_run_outside_input"] and a7["quick"]["cold_extra_jobs"] == 0,
            "A7 scope or quick recovery differs")

    upgrade = read(directory / "upgrade/result.json")
    require(upgrade["case"] == "A7-upgrade" and upgrade["passed"] is True and upgrade["paid_model_calls"] == 0
            and upgrade["candidate_git_head"] == source and upgrade["old_contract"] != upgrade["new_contract"]
            and upgrade["cases"] and all(case["full_rollback_matches"] for case in upgrade["cases"].values()),
            "Upgrade did not prove recovery and rollback on the selected source")
    return {"status": "PASS", "source": source, "cases": ["A1", *cases, "A7-upgrade"],
            "paid_model_calls": 0, "provider_tokens": None,
            "verified_files": len(index["files"]), "verified_runtime_files": len(runtime)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="Generate the publication index for captured results")
    parser.add_argument("--source", help="Full accepted code commit, required when writing; optional anchor when reading")
    parser.add_argument("--directory", type=Path, default=DIRECTORY, help="Acceptance and public receipts directory")
    parser.add_argument("--root", type=Path, default=ROOT, help="Corresponding product source directory")
    args = parser.parse_args()
    directory = args.directory.resolve()
    index_path = directory / INDEX
    try:
        index = seal(directory, args.root, args.source) if args.write else read(index_path)
        if args.source:
            require(index["source"] == args.source, "Index records a different source")
        report = report_checks(directory, args.root, index)
        if args.write:
            index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        print(json.dumps(report))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"Verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
