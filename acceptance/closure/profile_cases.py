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
from xml.etree import ElementTree

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from acceptance.closure.profile import test_environment
from acceptance.closure import profile_scope_case

BASELINE = REPO / "acceptance/phase2a/regression/final-v2/batch/suite/python-full.log"
BASELINE_REVISION = "78a8b07766376d93a458e5175c41f1f327ff8f36"
PROBE = Path(__file__).with_name("profile_probe.py")
CASE_LINE = re.compile(r"^test_\w+ \(([^()]+)\) \.\.\. ?(.*)$", re.M)
FAILURE_LINE = re.compile(r"^(?:FAIL|ERROR): [^\n(]+ \(([^()]+)\)([^\n]*)$", re.M)
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
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        block = text[start.start(2):end].partition("\n=======")[0]
        found = OUTCOME.search(block)
        status = found[1] if found else "unrecorded"
        skip_reason = None
        if status.startswith("skipped "):
            skip_reason = ast.literal_eval(status.removeprefix("skipped "))
            status = "skipped"
        # On a subTest failure unittest may print the parent method again
        # before its final status. The summary count checks the unique methods.
        if name not in rows or status != "unrecorded":
            rows[name] = {"outcome": status, "skip_reason": skip_reason}
    for error in FAILURE_LINE.finditer(text):
        if error[1] in rows:
            rows[error[1]]["outcome"] = error[0].split(":", 1)[0]
    count = re.search(r"^Ran (\d+) tests? in ", text, re.M)
    if not count or int(count[1]) != len(rows):
        raise ValueError(f"incomplete suite log: counted {len(rows)} identities, summary {count and count[1]}")
    return rows


def retain_original_file(source_dir, reference, output, expected_sha=None):
    """Read one supplied supporting file and preserve the exact bytes used in adjudication."""
    file = (source_dir / reference).resolve()
    if not file.is_relative_to(source_dir.resolve()):
        raise ValueError(f"evidence lies outside the supplied directory: {reference}")
    raw = file.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha and digest != expected_sha:
        raise ValueError(f"original evidence hash mismatch: {reference}")
    result = {"evidence": str(reference), "evidence_sha256": digest}
    if output is not None:
        directory = output / "original-evidence"
        directory.mkdir(exist_ok=True)
        target = directory / (digest + file.suffix)
        if not target.exists():
            target.write_bytes(raw)
        result["archived_evidence"] = str(target.relative_to(output))
    return result


def original_failures(path, text=None, *, output=None):
    """Import original unittest/pytest logs, JUnit reports, or a JSON failure list."""
    if text is None:
        text = path.read_text()
    reviews = {}
    if path.suffix.lower() == ".json":
        document = json.loads(text)
        items = document["failures"] if isinstance(document, dict) else document
        if not isinstance(items, list):
            raise ValueError("supply a JSON list or an object with a failures list")
        values = [(item, item, "") if isinstance(item, str) else
                  (item.get("original_id", item["test_id"]), item["test_id"], item.get("original_error", ""))
                  for item in items]
        for item in items:
            if isinstance(item, dict) and item.get("classification"):
                category = item["classification"]
                if category not in {"environment", "fixture", "regression", "unresolved"}:
                    raise ValueError(f"unknown original failure classification {category!r}")
                if item.get("reason") and item.get("evidence"):
                    review = {"classification": category, "reason": item["reason"], "evidence": item["evidence"]}
                    try:
                        review.update(retain_original_file(path.parent, item["evidence"], output,
                                                           item.get("evidence_sha256")))
                    except (OSError, ValueError) as exc:
                        review.update(classification="unresolved", original_claim=category, reason=str(exc))
                    reviews[item.get("original_id", item["test_id"])] = review
    elif path.suffix.lower() == ".xml":
        values = []
        document = ElementTree.fromstring(text)
        for test in document.iter("testcase"):
            identity = ".".join(filter(None, [test.get("classname"), test.get("name")]))
            for error in test:
                if error.tag in {"failure", "error"}:
                    trace = "\n".join([error.get("message", ""), error.text or ""])
                    values.append((f"{error.tag}: {identity}", identity, trace))
    else:
        values = []
        for block in re.split(r"^={6,}\s*$", text, flags=re.M):
            error = FAILURE_LINE.search(block)
            if error:
                # unittest can report multiple subTest failures (and a teardown
                # error) for one method. Retain the original complete heading.
                values.append((error[0], error[1], block.strip()))
        if not values:
            values = [(m[1], m[1], m[2]) for m in re.finditer(r"^(?:FAILED|ERROR)\s+(\S+?::\S+)(?: - (.*))?$", text, re.M)]
    result = {}
    for original_id, name, trace in values:
        if "::" in name:
            module, *parts = name.split("::")
            name = ".".join([Path(module).stem, *parts]).split("[", 1)[0]
        # Some JUnit writers keep test-package prefixes and parameter suffixes.
        method = re.search(r"(?:^|\.)(test_\w+\.\w+\.test_\w+)(?:$|\[|\s)", name)
        if method:
            name = method[1]
        if original_id in result:
            raise ValueError(f"duplicate original failure {original_id}")
        result[original_id] = (name, trace or "", reviews.get(original_id))
    return result


def supporting_files(source, contents, out, imported):
    if source.suffix.lower() != ".json":
        return {}
    document = json.loads(contents)
    if not isinstance(document, dict):
        return {}
    files = {}
    for key in ("original_run_log", "environment_file"):
        if document.get(key):
            files[key] = retain_original_file(source.parent, document[key], out, document.get(key + "_sha256"))
    # Original handoffs may name the full test log in each case's evidence field.
    if "original_run_log" not in files and imported:
        reviews = [review for _, _, review in imported.values()]
        if all(review and review.get("archived_evidence") for review in reviews):
            cited = {review["archived_evidence"] for review in reviews}
            if len(cited) == 1:
                path = out / cited.pop()
                if path.suffix.lower() in {".log", ".txt"} and FAILURE_LINE.search(path.read_text()):
                    files["original_run_log"] = {k: reviews[0][k] for k in
                                                 ("evidence", "archived_evidence", "evidence_sha256")}
    if "original_run_log" in files:
        actual = original_failures(out / files["original_run_log"]["archived_evidence"])
        if Counter(case for case, _, _ in actual.values()) != Counter(case for case, _, _ in imported.values()):
            raise ValueError("classified identities differ from the supplied original failure log")
        # Unittest's long header includes a method name, a qualified ID and optional
        # subtest parameters. The reviewer's short header preserves status, ID and parameters.
        headings = {heading: heading for heading in actual}
        for heading in actual:
            if match := FAILURE_LINE.fullmatch(heading):
                short = heading.partition(":")[0] + ": " + match[1] + match[2]
                headings[short] = heading
        used = set()
        for original_id, (name, excerpt, review) in imported.items():
            heading = headings.get(original_id) or original_id
            if heading not in actual:
                matches = [key for key, (case, _, _) in actual.items() if case == name]
                if original_id not in {name, "tests." + name} or len(matches) != 1:
                    raise ValueError(f"original failure heading is ambiguous or differs: {original_id}")
                heading, = matches
            _, trace, _ = actual[heading]
            if heading in used or (excerpt and excerpt not in trace):
                raise ValueError(f"original traceback or heading differs: {original_id}")
            used.add(heading)
            if review:
                review.update(matched_original_heading=heading, matched_trace_sha256=hashlib.sha256(trace.encode()).hexdigest())
    return files


def failure_verdict(trace, now):
    if now["outcome"] == "ok":
        if "CLIENT_CAPABILITY_MISSING" in trace and "claude_code" in trace.lower():
            return "environment", "Original trace shows the Claude session gate. Isolated host detection passes (T02)."
        if re.search(r"jobs hold \d+ of the 1 threads jobs may use", trace):
            return "environment", "Original trace records a one-thread Job limit. The two-slot admission control passes (T01)."
    return "unresolved", "The supplied failure and current outcome do not establish its cause."


def reuse_python_suite(candidate, revision, out):
    summary = json.loads((candidate / "summary.json").read_text())
    lock = json.loads((REPO / "release-assets/lock.json").read_text())
    operations = summary["operations"]
    python = operations["python-full"]
    log = candidate / "python-full.log"
    requirements = {
        "revision": summary["commit"] == summary["verified_product_commit"] == revision,
        "profile": summary["test_resource_profile"] == "deterministic"
                   and summary["test_environment_sha256"] == sha(Path(__file__).with_name("profile.py")),
        "locked Codex": summary["native_binary"]["sha256"] == lock["linux_x64"]["binary_sha256"],
        "bootstrap": all(operations[name]["returncode"] == 0 for name in ("clash-preflight", "offline-cordis", "offline-wheels"))
                     and {"--no-index", "--require-hashes"} <= set(operations["offline-wheels"]["command"]),
        "discovery": python["command"] == ["$SCRATCH/venv/bin/python", "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
                     and python["cwd"] == "$CHECKOUT/gx-h" and type(python["returncode"]) is int,
        "raw log digest": summary["python_log_sha256"] == sha(log),
    }
    missing = [key for key, ok in requirements.items() if not ok]
    if missing:
        raise ValueError("suite evidence mismatch: " + ", ".join(missing))
    out.mkdir()
    for name in ("python-full.log", "summary.json"):
        shutil.copyfile(candidate / name, out / name)
    return {"mode": "reused", "suite_source": str(candidate), "source_summary_sha256": sha(candidate / "summary.json"),
            "argv": python["command"], "cwd": python["cwd"], "exit_code": python["returncode"], "commit": revision}


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
    receipt = None
    reuse_issue = None
    if args.suite_evidence:
        try:
            receipt = reuse_python_suite(args.suite_evidence.resolve(), commit, suite_dir)
        except (OSError, ValueError, KeyError, TypeError) as err:
            reuse_issue = str(err)
    if receipt is None:
        with (out / "suite-invocation.log").open("w") as log:
            run = subprocess.run(command, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
        receipt = {"mode": "executed", "argv": command, "cwd": str(REPO), "exit_code": run.returncode,
                   "commit": commit, "environment_names": sorted(env), "reuse_issue": reuse_issue}
    else:
        (out / "suite-invocation.log").write_text("Reused the matching Python discovery receipt and verified its raw log.\n")
    write_json(out / "suite-invocation.json", receipt)
    original_log = subprocess.check_output(
        ["git", "show", f"{BASELINE_REVISION}:{BASELINE.relative_to(REPO).as_posix()}"], cwd=REPO)
    reference = unittest_rows(original_log.decode())
    if len(reference) != 465:
        raise ValueError("historical Python baseline must contain exactly 465 unique identities")
    measured = unittest_rows((suite_dir / "python-full.log").read_text())
    observed_profile = json.loads((suite_dir / "summary.json").read_text()).get("test_resource_profile")
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
    additional = sorted(measured.keys() - reference.keys())
    additional_nonpassing = {name: measured[name] for name in additional if measured[name]["outcome"] != "ok"}
    source = args.original_failures
    imported = {}
    import_issue = None
    original_input = None
    original_digest = None
    supporting_inputs = {}
    original_environment = None
    if source:
        try:
            raw = source.read_bytes()
            original_input = "original-input" + (source.suffix if source.suffix.lower() in {".json", ".xml"} else ".log")
            (out / original_input).write_bytes(raw)
            original_digest = hashlib.sha256(raw).hexdigest()
            contents = raw.decode("utf-8-sig")
            imported = original_failures(source, contents, output=out)
            supporting_inputs = supporting_files(source, contents, out, imported)
            if source.suffix.lower() == ".json":
                doc = json.loads(contents)
                if isinstance(doc, dict):
                    original_environment = doc.get("source")
        except (OSError, KeyError, TypeError, ValueError, ElementTree.ParseError) as err:
            import_issue = f"Unable to read the supplied original failure records: {err}"
    verdicts = []
    for original_id, (name, trace, review) in imported.items():
        now = measured.get(name, {"outcome": "missing"})
        classification, reason = failure_verdict(trace, now)
        if review:
            classification, reason = review["classification"], review["reason"]
        if name not in reference:
            classification, reason = "unresolved", "Supplied identity is outside the historical set of 465."
        verdicts.append({"original_id": original_id, "test_id": name, "current_outcome": now["outcome"], "classification": classification,
                         "reason": reason, "source_review": review,
                         "original_trace_sha256": hashlib.sha256(trace.encode()).hexdigest() if trace else None})
    issue = None
    if import_issue:
        issue = import_issue
    elif source is None:
        issue = "The closure package reports 42 original failures without identifying them or supplying their tracebacks."
    elif len(imported) != 42:
        issue = f"The supplied source identifies {len(imported)} distinct failures of the expected 42."
    elif source.suffix.lower() != ".json" or any(row["source_review"] is None for row in verdicts):
        issue = "The original classification JSON with per-case reasons and evidence is required for historical sign-off."
    elif any(row["classification"] == "unresolved" for row in verdicts):
        issue = "Original failures without case-specific causal evidence remain unresolved."
    write_json(out / "original-42.json", {"expected": 42, "identified": len(imported),
                                         "distinct_test_ids": len({case for case, _, _ in imported.values()}),
                                         "source": str(source) if source else None,
                                         "archived_input": original_input, "source_sha256": original_digest,
                                         "supporting_inputs": supporting_inputs, "original_run_metadata": original_environment,
                                         "issue": issue, "cases": verdicts})
    failed_run = bool(failures or additional_nonpassing or receipt["exit_code"] or observed_profile != "deterministic")
    problems = []
    if failed_run:
        problems.append(f"Python suite exited {receipt['exit_code']}. {len(failures)} original identities changed or failed "
                        f"and {len(additional_nonpassing)} additional cases did not pass.")
    if observed_profile != "deterministic":
        problems.append(f"The staged product observed resource profile {observed_profile!r}.")
    if issue:
        problems.append(issue)
    return {"status": "FAIL" if failed_run else "PARTIAL" if issue else "PASS",
            "revision": commit, "baseline_revision": BASELINE_REVISION,
            "resource_profile": observed_profile,
            "baseline_log": str(BASELINE.relative_to(REPO)), "baseline_log_sha256": hashlib.sha256(original_log).hexdigest(),
            "baseline_count": len(reference), "baseline_outcomes": dict(Counter(r["measured"]["outcome"] for r in ledger)),
            "additional_ids": additional, "additional_nonpassing": additional_nonpassing, "failed_baseline_ids": failures,
            "original_ids_obtained": len(imported), "issue": " ".join(problems) or None,
            "evidence": ["baseline-465.json", "original-42.json", "suite/python-full.log", "suite/summary.json",
                         "suite-invocation.json", "suite-invocation.log", *([original_input] if original_input else [])]}


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
            assert row["shipped_policy_on_two_cpus"] == 1, row
            assert row["memory"]["available_mib"] == 4096 and row["resource_profile"]["mode"] == "deterministic", row
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
        assert isolated["run_started"] and isolated["stored_host"] == "other_verified", isolated
        assert not marked["run_started"] and marked["run_start_error"] == "CLIENT_CAPABILITY_MISSING", marked
        samples = [isolated, marked]
    else:
        row = measure("real-admission", env)
        assert row["resource_profile"]["source"] == "host" and row["memory"]["source"].startswith("cgroup"), row
        limits = json.loads((REPO / "gx-h/config/limits.json").read_text())["cpu"]
        quota = row["quota_cpus"] or row["logical_cpus"] or 1
        assert row["configured_limits"] == limits and row["cpu_source"] != "deterministic test profile", row
        assert row["allowed"] == max(1, min(limits["max_pool_workers"], int(quota * limits["max_quota_fraction"]))), row
        assert row["gpu_source"] == "nvidia-smi" and row["job_status"] == "SUCCEEDED" and row["output_count"] > 0, row
        samples = [row]
    return {"status": "PASS", "samples": samples, "resource_profile": profile, "evidence": evidence}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("T01", "T02", "T03", "REAL_RESOURCES", "PROFILE-SCOPE"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--commit", default="HEAD", help="Git revision whose isolated Python suite will run")
    parser.add_argument("--suite-evidence", type=Path, help="reuse a matching fresh deterministic Python suite receipt")
    parser.add_argument("--original-failures", type=Path, help="original unittest/pytest log, JUnit XML, or JSON identities")
    args = parser.parse_args()
    args.out = args.out.resolve()
    args.python = args.python.absolute()
    args.binary = args.binary.absolute() if args.binary else None
    args.out.mkdir(parents=True, exist_ok=True)
    if any(args.out.iterdir()):
        parser.error("output directory must be new or empty")
    try:
        with tempfile.TemporaryDirectory(prefix="gx-case-") as temp:
            handler = {"T03": baseline_run, "PROFILE-SCOPE": profile_scope_case.run}.get(args.case, probe_cases)
            result = handler(args, Path(temp))
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
