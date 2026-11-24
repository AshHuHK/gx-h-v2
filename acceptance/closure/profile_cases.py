"""Run a P2A test-environment case and retain its measured results."""

import argparse
import ast
from collections import Counter
from contextlib import closing
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from acceptance.closure.profile import test_environment

BASELINE = REPO / "acceptance/phase2a/regression/final-v2/batch/suite/python-full.log"
PROBE = Path(__file__).with_name("profile_probe.py")
CASE_LINE = re.compile(r"^test_\w+ \(([^()]+)\) \.\.\. ?(.*)$", re.M)
FAILURE_LINE = re.compile(r"^(?:FAIL|ERROR): [^\n(]+ \(([^()]+)\).*$", re.M)
OUTCOME = re.compile(r"^(ok|FAIL|ERROR|skipped .+|expected failure|unexpected success)$", re.M)


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def unittest_rows(text):
    """Read unittest -v, including tests whose own output interrupts the status line."""
    starts = list(CASE_LINE.finditer(text))
    rows = {}
    for index, start in enumerate(starts):
        name = start[1]
        if name in rows:
            raise ValueError(f"duplicate unittest identity {name}")
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.start(2):end].partition("\n=======")[0]
        found = OUTCOME.search(block)
        status = found[1] if found else "unrecorded"
        skip_reason = None
        if status.startswith("skipped "):
            skip_reason = ast.literal_eval(status.removeprefix("skipped "))
            status = "skipped"
        rows[name] = {"outcome": status, "skip_reason": skip_reason}
    for error in FAILURE_LINE.finditer(text):
        if error[1] in rows:
            rows[error[1]]["outcome"] = error[0].split(":", 1)[0]
    count = re.search(r"^Ran (\d+) tests? in ", text, re.M)
    if not count or int(count[1]) != len(rows):
        raise ValueError(f"incomplete suite log: counted {len(rows)} identities, summary {count and count[1]}")
    return rows


def original_failures(path):
    """Import original unittest tracebacks, pytest summary lines, or a JSON list of identities."""
    text = path.read_text()
    if path.suffix.lower() == ".json":
        document = json.loads(text)
        items = document["failures"] if isinstance(document, dict) else document
        if not isinstance(items, list):
            raise ValueError("supply a JSON list or an object with a failures list")
        values = [(item, "") if isinstance(item, str) else (item["test_id"], item.get("original_error", ""))
                  for item in items]
    else:
        values = []
        for block in re.split(r"^={6,}\s*$", text, flags=re.M):
            error = FAILURE_LINE.search(block)
            if error:
                values.append((error[1], block.strip()))
        if not values:
            values = [(m[1], m[2]) for m in re.finditer(r"^(?:FAILED|ERROR)\s+(\S+?::\S+)(?: - (.*))?$", text, re.M)]
    result = {}
    for name, trace in values:
        if "::" in name:
            module, *parts = name.split("::")
            name = ".".join([Path(module).stem, *parts])
        if name in result:
            raise ValueError(f"duplicate original failure {name}")
        result[name] = trace or ""
    return result


def failure_verdict(trace, now):
    if now["outcome"] == "ok":
        if "CLIENT_CAPABILITY_MISSING" in trace and "claude_code" in trace.lower():
            return "environment", "Original trace shows the Claude session gate. Isolated host detection passes (T02)."
        if re.search(r"jobs hold \d+ of the 1 threads jobs may use", trace):
            return "environment", "Original trace records a one-thread Job limit. The two-slot admission control passes (T01)."
    return "unresolved", "The supplied failure and current outcome do not establish its cause."


def baseline_run(args, work):
    out = args.out
    commit = subprocess.check_output(["git", "rev-parse", "--verify", args.commit + "^{commit}"],
                                     cwd=REPO, text=True).strip()
    suite_dir = out / "suite"
    frozen = json.loads((BASELINE.parent.parent / "dependencies.json").read_text())
    binary = args.binary
    if binary is None:
        lock = json.loads((REPO / "release-assets/lock.json").read_text())
        relative = Path("vendor/codex-runtime/package") / lock["linux_x64"]["binary"]
        binary = next((candidate for candidate in (REPO / relative, Path(frozen["stage"]) / relative)
                       if candidate.is_file()), None)
    if binary is None:
        raise FileNotFoundError("supply --binary pointing to the Codex ELF pinned in release-assets/lock.json")
    command = [str(args.python), "-B", "acceptance/v21/regression.py", "--commit", commit,
               "--python-only", "--binary", str(binary), "--out", str(suite_dir)]
    env = test_environment(work, python=args.python, binary=binary)
    with (out / "suite-invocation.log").open("w") as log:
        run = subprocess.run(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    write_json(out / "suite-invocation.json", {"argv": command, "cwd": str(REPO), "exit_code": run.returncode,
                                               "commit": commit, "environment_names": sorted(env)})
    reference = unittest_rows(BASELINE.read_text())
    if len(reference) != 465:
        raise ValueError("historical Python baseline must contain exactly 465 unique identities")
    measured = unittest_rows((suite_dir / "python-full.log").read_text())
    failures = []
    ledger = []
    for name, previous in reference.items():
        now = measured.get(name, {"outcome": "missing", "skip_reason": None})
        matches = (now["outcome"] == "ok" or
                   (now["outcome"] == "skipped" and previous == now))
        ledger.append({"test_id": name, "reference": previous, "measured": now, "matches": matches})
        if not matches:
            failures.append(name)
    write_json(out / "baseline-465.json", ledger)
    source = args.original_failures
    imported = original_failures(source) if source else {}
    verdicts = []
    for name, trace in imported.items():
        now = measured.get(name, {"outcome": "missing"})
        classification, reason = failure_verdict(trace, now)
        if name not in reference:
            classification, reason = "unresolved", "Supplied identity is outside the historical set of 465."
        verdicts.append({"test_id": name, "current_outcome": now["outcome"], "classification": classification,
                         "reason": reason, "original_trace_sha256": hashlib.sha256(trace.encode()).hexdigest() if trace else None})
    issue = None
    if source is None:
        issue = "The closure package reports 42 original failures without identifying them or supplying their tracebacks."
    elif len(imported) != 42:
        issue = f"The supplied source identifies {len(imported)} distinct failures of the expected 42."
    elif any(row["classification"] == "unresolved" for row in verdicts):
        issue = "Original failures without case-specific causal evidence remain unresolved."
    write_json(out / "original-42.json", {"expected": 42, "identified": len(imported), "source": str(source) if source else None,
                                         "source_sha256": sha(source) if source else None, "issue": issue, "cases": verdicts})
    return {"status": "FAIL" if failures or run.returncode else "PARTIAL" if issue else "PASS",
            "revision": commit, "baseline_log": str(BASELINE.relative_to(REPO)), "baseline_log_sha256": sha(BASELINE),
            "baseline_count": len(reference), "baseline_outcomes": dict(Counter(r["measured"]["outcome"] for r in ledger)),
            "additional_ids": sorted(measured.keys() - reference.keys()), "failed_baseline_ids": failures,
            "original_ids_obtained": len(imported), "issue": issue,
            "evidence": ["baseline-465.json", "original-42.json", "suite/python-full.log", "suite/summary.json",
                         "suite-invocation.json", "suite-invocation.log"]}


def probe_cases(args, work):
    case = args.case
    invocations = []
    evidence = ["commands.json"]

    def measure(label, env, *extra):
        database = work / (label + ".db")
        command = [str(args.python), "-B", str(PROBE), case, str(database), *extra]
        run = subprocess.run(command, cwd=REPO, env=env, text=True, capture_output=True, timeout=60)
        (args.out / (label + ".log")).write_text(run.stdout + run.stderr)
        evidence.append(label + ".log")
        invocations.append({"argv": command, "label": label, "exit_code": run.returncode, "environment_names": sorted(env)})
        write_json(args.out / "commands.json", invocations)
        run.check_returncode()
        if case != "T02":
            # The probe closed its store. Backup folds any WAL pages into the single image.
            snapshot = work / (label + "-snapshot.db")
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(snapshot)) as copy:
                    source.backup(copy)
                source.row_factory = sqlite3.Row
                events = source.execute("SELECT event_id,ts,kind,run_id,subject_kind,subject_id,"
                                        "prev_state,new_state FROM events ORDER BY event_id")
                write_json(args.out / (label + "-events.json"), [dict(event) for event in events])
            with snapshot.open("rb") as raw, gzip.open(args.out / (label + ".sqlite.gz"), "wb") as archive:
                shutil.copyfileobj(raw, archive)
            evidence.extend((label + ".sqlite.gz", label + "-events.json"))
        return json.loads(run.stdout)

    profile = "real" if case == "REAL_RESOURCES" else "deterministic"
    env = test_environment(work, profile=profile, python=args.python, binary=args.binary)
    if case == "T01":
        samples = [measure("one-cpu-affinity", env, "1"), measure("full-affinity", env)]
        for row in samples:
            assert (row["logical_cpus"], row["quota_cpus"], row["allowed"], row["free"]) == (2, 2, 2, 2), row
            assert (row["held_when_full"], row["free_when_full"], row["free_after_cancel"]) == (2, 0, 1), row
            assert row["refusal"]["details"]["rule"] == "cpu.threads_free" and row["gpu_devices"] == [], row
            assert row["gpu_source"] == "supplied" and len(set(row["admitted_job_ids"])) == 3, row
    elif case == "T02":
        markers = {"CLAUDECODE": "1", "CLAUDE_CODE_ENTRYPOINT": "profile-test", "CLAUDE_CODE_SESSION_ID": "test-session",
                   "CLAUDE_PROJECT_DIR": "/fixture", "CLAUDE_CODE_EXECPATH": "/fixture/claude",
                   "GX_UNLISTED_HOST_MARKER": "present"}
        with patch.dict(os.environ, markers):
            clean = test_environment(work, python=args.python, binary=args.binary)
            isolated = measure("isolated", clean)
            # This child deliberately tests the positive branch of the detector.
            marked = measure("explicit-detection-control", {**clean, **markers})
            assert all(os.environ[key] == value for key, value in markers.items())
        assert (isolated["kind"], isolated["basis"], isolated["marker_names"]) == ("other_verified", "default", []), isolated
        assert not isolated["ambient_sentinel_present"] and isolated["detected"] is None, isolated
        assert (marked["kind"], marked["basis"]) == ("claude_code", "launch_environment"), marked
        assert not isolated["hook_marker"] and not isolated["tool_shell_marker"], isolated
        assert marked["hook_marker"] and marked["tool_shell_marker"], marked
        samples = [isolated, marked]
    else:
        row = measure("real-admission", env)
        limits = json.loads((REPO / "gx-h/config/limits.json").read_text())["cpu"]
        quota = row["quota_cpus"] or row["logical_cpus"] or 1
        assert row["configured_limits"] == limits and row["cpu_source"] != "deterministic test profile", row
        assert row["allowed"] == max(1, min(limits["max_pool_workers"], int(quota * limits["max_quota_fraction"]))), row
        assert row["gpu_source"] == "nvidia-smi" and row["job_status"] == "SUCCEEDED" and row["output_count"] > 0, row
        samples = [row]
    return {"status": "PASS", "samples": samples, "resource_profile": profile, "evidence": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("T01", "T02", "T03", "REAL_RESOURCES"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--commit", default="HEAD", help="Git revision whose isolated Python suite will run")
    parser.add_argument("--original-failures", type=Path, help="original dated unittest/pytest log or JSON identities")
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.python = args.python.absolute()
    args.binary = args.binary.absolute() if args.binary else None
    args.out.mkdir(parents=True, exist_ok=True)
    if any(args.out.iterdir()):
        parser.error("output directory must be new or empty")
    try:
        with tempfile.TemporaryDirectory(prefix="gx-case-") as temp:
            result = (baseline_run if args.case == "T03" else probe_cases)(args, Path(temp))
    except Exception as err:
        import traceback
        (args.out / "exception.log").write_text(traceback.format_exc())
        result = {"status": "FAIL", "issue": str(err), "evidence": ["exception.log"]}
    result.update(case_id=args.case, passed=result["status"] == "PASS",
                  classification="REAL_COMPUTE" if args.case == "REAL_RESOURCES" else "SCRIPTED")
    write_json(args.out / "result.json", result)
    print(json.dumps({"case_id": args.case, "status": result["status"], "result": str(args.out / "result.json")}))
    return {"PASS": 0, "PARTIAL": 2, "FAIL": 1}[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
