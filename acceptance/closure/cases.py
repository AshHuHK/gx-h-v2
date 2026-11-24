"""Phase 2A matrix entries and the existing executable probes that exercise them."""
from dataclasses import dataclass
from pathlib import Path

from acceptance.phase2a.regression.run import GATE_OPERATIONS
from acceptance.closure.archive import read


ROOT = Path(__file__).resolve().parents[2]
PHASE = ROOT / "acceptance/phase2a"
CLOSURE = ROOT / "acceptance/closure"
HARDEN = ROOT / "acceptance/hardening"
ORIGINAL = tuple(f"P2A-{index:02}" for index in range(1, 16))
USAGE = tuple(f"U{index:02}" for index in range(1, 9))
PROFILE = ("T01", "T02", "T03")
DATABASE = ("DB01", "DB02")
AUTOMATION = ("AC01", "AC02", "AC03")
IDLE = ("IDLE01", "IDLE02", "IDLE03")
CROSS = (*ORIGINAL[:7], "P2A-14")
MATRIX = (*ORIGINAL, *USAGE, *PROFILE, *DATABASE, *AUTOMATION, *IDLE, "REG01")
SUPPLEMENTAL = ("OFFICIAL-USAGE", "REAL_RESOURCES", "LEGACY-LEDGER")
HARDENING = ("DEPENDENCIES", "CHILD-DOUBLE-LOSS", "OPERATOR-USAGE", "ESTIMATE-RISK", "FIFO",
             "PROFILE-SCOPE", "T01", "T02", "REAL_RESOURCES", "TIMEOUT", "FAIRNESS", "SQLITE-BOUNDARIES", "T03")
SUITES = {"phase2a": (*MATRIX, "OFFICIAL-USAGE"), "all": (*MATRIX, *SUPPLEMENTAL), "hardening": HARDENING}
SCRIPT = ("SCRIPTED",)
OFFICIAL = ("SCRIPTED", "OFFICIAL_HOST_LOOPBACK")


@dataclass
class Step:
    name: str
    ids: tuple
    argv: list
    classes: tuple
    receipt: str
    kind: str = "exit"
    requirements: tuple = ()
    namespace: bool = False
    cwd: Path = ROOT
    artifacts: tuple = ()


def plan(args, selected, raw, revision):
    steps = []
    def add(name, ids, command, receipt, *, classes=SCRIPT, kind="exit", requires=(),
            namespace=False, cwd=ROOT, artifacts=()):
        requested = tuple(item for item in ids if item in selected)
        if requested:
            steps.append(Step(name, requested, [str(arg) for arg in command], classes, receipt,
                              kind, tuple(requires), namespace, cwd, tuple(artifacts)))

    py = [args.python, "-B"]
    def out(name):
        return raw / name / "evidence"

    add("cross-process", CROSS, [*py, PHASE / "cross_process.py", "--output", out("cross-process"),
        *(arg for case in CROSS if case in selected for arg in ("--check", case))],
        "evidence/observations.json", classes=OFFICIAL, kind="cross",
        requires=(args.binary,), namespace=True)
    add("performance", ("P2A-14",), [*py, PHASE / "performance.py", out("cross-process"),
        "--output", out("performance")], "evidence/summary.json")
    add("branch-failure", ("P2A-08",), [*py, PHASE / "branch_failure.py", "--output", out("branch-failure")],
        "evidence/evidence.json")
    gates = [gate for gate in GATE_OPERATIONS if gate in selected]
    add("v31-regression", GATE_OPERATIONS, [*py, PHASE / "regression/run.py", "--phase", "full",
        "--revision", revision, "--closure-usage", "--cache", args.cache, "--source-db", args.source_db,
        "--historical-briefs", args.historical_briefs, "--catalog", args.catalog, "--out", out("v31-regression"),
        *(arg for gate in gates for arg in ("--gate", gate))], "evidence/execution.json", classes=OFFICIAL,
        kind="regression", namespace=True,
        requires=(args.binary, args.cache, args.source_db, args.historical_briefs, args.catalog))
    add("jobs", ("P2A-15",), [*py, PHASE / "job_unavailable.py", "--output", out("jobs")],
        "evidence/result.json")
    selected_usage = [case for case in USAGE if case in selected]
    usage_filter = [] if len(selected_usage) == len(USAGE) else [
        arg for case in selected_usage for arg in ("--case", case)]
    add("usage", USAGE, [*py, CLOSURE / "usage.py", "--output", out("usage"), *usage_filter],
        "evidence/result.json", kind="matrix")
    for case in (*PROFILE, "PROFILE-SCOPE", "REAL_RESOURCES"):
        extra = ["--original-failures", args.original_failures] if case == "T03" and args.original_failures else []
        add(case, (case,), [*py, CLOSURE / "profile_cases.py", "--case", case, "--out", out(case),
                           "--python", args.python, "--binary", args.binary, "--commit", revision, *extra],
            "evidence/result.json", classes=("REAL_COMPUTE",) if case == "REAL_RESOURCES" else SCRIPT,
            kind="profile", namespace=case == "T03", requires=(args.binary,) if case == "T03" else ())
    add("sqlite", DATABASE, [*py, PHASE / "sqlite_telemetry.py", "--output", out("sqlite")],
        "evidence/observations.json", kind="checks")
    for case in AUTOMATION:
        add(case, (case,), [*py, CLOSURE / "test_acceptance.py", f"Automation.test_{case.lower()}",
                            "-v"], "console.log", kind="unittest")
    for case in IDLE:
        add(case, (case,), [*py, "-m", "unittest", "-v", "-k", f"test_{case.lower()}_", "tests.test_p2a_idle"],
            "console.log", kind="unittest", cwd=ROOT / "gx-h")
    add("official-usage", ("OFFICIAL-USAGE",), [*py, CLOSURE / "official_usage.py", "--runtime",
        args.cache, "--output", raw / "official-usage/result.json"], "result.json", classes=OFFICIAL,
        requires=(args.binary,), namespace=True)
    add("legacy-ledger", ("LEGACY-LEDGER",), [*py, CLOSURE / "legacy_ledger.py", "--out",
        raw / "legacy-ledger/result.json"], "result.json", kind="hardening")
    add("dependencies", ("DEPENDENCIES",), [args.python, "-I", "-B", ROOT / "scripts/acceptance_dependencies.py",
        "--check-controls", "--work", args.dependency_control_work, "--python", args.seed_python,
        *(["--node", args.node] if args.node else []),
        "--output", raw / "dependencies/result.json"], "result.json", kind="hardening")
    add("child-double-loss", ("CHILD-DOUBLE-LOSS",), [*py, CLOSURE / "child_double_loss.py", "--out",
        out("child-double-loss"), "--binary", args.binary], "evidence/receipt.json", classes=OFFICIAL,
        kind="hardening", requires=(args.binary,), namespace=True, artifacts=tuple("evidence/" + name for name in
        ("manifest.json", "official-wire.jsonl", "lost-start.json", "before-recovery.json", "after-history.json",
         "cold-history.jsonl", "post-grace.json", "repeat-driver.json", "provider-requests.json", "ledger.sqlite.gz")))
    add("operator-usage", ("OPERATOR-USAGE",), [*py, CLOSURE / "unresolved.py", "--output",
        out("operator-usage")], "evidence/result.json", kind="hardening")
    add("operator-usage-official", ("OPERATOR-USAGE",), [*py, CLOSURE / "unresolved_host.py", "--output",
        out("operator-usage-official")], "evidence/result.json", kind="hardening", classes=OFFICIAL,
        requires=(args.binary,), namespace=True, artifacts=("evidence/ledger.db",))
    add("estimate-risk", ("ESTIMATE-RISK",), [*py, HARDEN / "estimate_risk.py", "--output",
        out("estimate-risk")], "evidence/result.json", kind="hardening")
    add("fifo", ("FIFO",), [*py, "-m", "unittest", "-v", "tests.test_p2a1_fifo"], "console.log",
        kind="unittest", cwd=ROOT / "gx-h", artifacts=tuple("evidence/" + name for name in
        ("handoff.json", "fallback.json", "fallback.db", "path-validation.json", "read-race.json", "idle-overhead.json")))
    add("timeout", ("TIMEOUT",), [*py, "-m", "unittest", "-v",
        "tests.test_v21_compute.ComputeTest.test_existing_job_limits_stop_bounded_compute_load"],
        "console.log", kind="unittest", cwd=ROOT / "gx-h", artifacts=("timeout.json",))
    add("fairness", ("FAIRNESS",), [*py, HARDEN / "cpu_fairness.py", "--output", out("fairness")],
        "evidence/receipt.json", kind="hardening", classes=("SCRIPTED", "REAL_COMPUTE"),
        artifacts=("evidence/events.jsonl", "evidence/gx.db.gz"))
    add("sqlite-boundaries", ("SQLITE-BOUNDARIES",), [*py, ROOT / "acceptance/p2a1/sqlite_boundaries.py",
        "--output", out("sqlite-boundaries")], "evidence/result.json", kind="hardening",
        artifacts=("evidence/summary.json", "evidence/transactions.jsonl", "evidence/source-audit.json"))
    if args.suite == "hardening":
        add("T03-intake", ("T03",), [*py, HARDEN / "test_historical_intake.py", "-v"],
            "console.log", kind="unittest")
        steps.sort(key=lambda step: HARDENING.index(step.ids[0]))
    return steps


def row(status, evidence, reason=None):
    return {"status": status, "evidence": evidence, **({"reason": reason} if reason else {})}


def supporting_files(step, data, case):
    if step.name == "branch-failure":
        return data["files_sha256"].keys()
    if step.name == "jobs":
        return data["evidence_files"]
    if step.name == "official-usage":
        return [item["name"] for item in data["snapshots"]]
    if step.name == "usage":
        return [data["cases"][case][key] for key in ("ledger", "events", "observations")]
    if step.name == "operator-usage":
        return [item[key] for item in data["cases"].values() for key in ("ledger", "events", "observations")]
    if step.name == "estimate-risk":
        return [item[key] for item in data["cases"] for key in ("status_path", "usage_path", "events_path")]
    if step.kind == "profile":
        return data["evidence"]
    if step.name == "sqlite":
        return ("gx.db", "metrics.jsonl", "summary.json")
    if step.name == "performance":
        return [data[key]["file"] for key in ("raw_owner_samples", "raw_case_observations")]
    return ()


def hardening_result(step, case, data, returncode, evidence):
    if "status" in data:
        status = data["status"]
    else:
        status = "PASS" if data["passed"] is True else "FAIL"
    if status not in {"PASS", "FAIL", "PARTIAL", "BLOCKED"}:
        raise ValueError(f"unexpected {case} status {status!r}")
    if status != "PASS":
        return row(status, evidence, data.get("reason") or data.get("issue"))
    if returncode != 0 or data.get("passed", True) is not True:
        return row("FAIL", evidence, "contradictory subprocess exit or pass flag")
    if case == "CHILD-DOUBLE-LOSS" and not (data["closure"] == "CLOSED" and data["run_id"]
            and data["checks"] and all(value is True for value in data["checks"].values())):
        return row("FAIL", evidence, "child double-loss receipt lacks a closed run")
    if step.name == "operator-usage" and not (data["cases"] and all(
            item["status"] == "PASS" for item in data["cases"].values())):
        return row("FAIL", evidence, "operator probe reports incomplete cases")
    if case == "ESTIMATE-RISK" and {item["name"] for item in data["cases"] if item["status"] == "PASS"} != {
            "parent", "parent_with_child"}:
        return row("FAIL", evidence, "missing parent or child exposure observation")
    if case == "LEGACY-LEDGER":
        restored = data["closure_ledgers"]
        if not (len(data["cases"]) == 2 and len(data["rollback_revision"]) == 40 and set(restored) == {"U05", "B05"}
                and all(len(item["restored_backup_sha256"]) == 64 and item["restored_token_balance"] == item["token_balance"]
                        for item in restored.values())):
            return row("FAIL", evidence, "missing original ledger or verified rollback balance")
    return row(status, evidence)


def observed(step, directory, returncode):
    source = directory / step.receipt
    evidence = f"{step.name}/{step.receipt}"
    if not source.is_file():
        return {case: row("FAIL", f"{step.name}/console.log", f"missing {step.receipt}") for case in step.ids}
    missing = [name for name in step.artifacts
               if not (directory / name).is_file() or not (directory / name).stat().st_size]
    if missing:
        return {case: row("FAIL", evidence, "missing raw evidence: " + ", ".join(missing)) for case in step.ids}
    if step.kind == "unittest":
        import re
        output = source.read_text()
        ok = returncode == 0 and re.search(r"^Ran [1-9]\d* tests? in ", output, re.M)
        if step.name in {"fifo", "timeout", "T03-intake"}:
            ok = ok and re.search(r"^OK$", output, re.M)
        return {case: row("PASS" if ok else "FAIL", evidence) for case in step.ids}
    data = read(source)
    if not isinstance(data, dict) or not data:
        raise ValueError(f"empty receipt: {step.receipt}")
    results = {}
    for case in step.ids:
        if step.kind == "regression":
            operations = data["operations"]
            failed = [name for name in GATE_OPERATIONS[case]
                      if operations.get(name, {}).get("returncode") != 0]
            if case == "REG01" and operations.get("extra-child-observation", {}).get("returncode") != 0:
                failed.append("extra-child-observation")
            if failed:
                results[case] = row("FAIL", evidence, "failed/missing operations: " + ", ".join(failed))
            elif data.get("errors") or data.get("tracked_product_changes"):
                results[case] = row("FAIL", evidence, "regression runner reported an execution or source error")
            else:
                results[case] = row("PASS", evidence)
            if case == "REG01":
                supplement = source.parent / "extra-child/observation.json"
                if supplement.exists():
                    child = read(supplement)
                    results[case]["supplemental_child_observation"] = {
                        "status": child["observed_status"], "returncode": child["returncode"],
                        "evidence": f"{step.name}/evidence/extra-child/observation.json"}
                    if child["observed_status"] != "PASS" or child["returncode"] != 0:
                        results[case].update(status="FAIL", reason="double-loss child did not settle")
                else:
                    results[case].update(status="FAIL", reason="missing double-loss child observation")
        elif step.kind == "cross":
            result = data["matrix"][case]
            lock = read(ROOT / "release-assets/lock.json")["linux_x64"]
            ok = (data["codex_sha256"] == lock["binary_sha256"] and result["state"] == "PASS"
                  and (source.parent / result["evidence"]).is_file())
            results[case] = row("PASS" if ok else "FAIL", evidence)
        elif step.kind == "profile":
            status = data["status"]
            expected_code = {"PASS": 0, "PARTIAL": 2, "FAIL": 1}
            if data["case_id"] != case or expected_code.get(status) != returncode:
                status = "FAIL"
            results[case] = row(status, evidence, data.get("issue") or data.get("reason") or data.get("reasons"))
        elif step.kind == "matrix":
            status = data["cases"][case]["status"]
            if status not in {"PASS", "FAIL", "PARTIAL", "BLOCKED"}:
                raise ValueError(f"unexpected {case} result: {status}")
            results[case] = row(status, evidence)
        elif step.kind == "checks":
            ok = returncode == 0 and data["checks"][case] is True
            results[case] = row("PASS" if ok else "FAIL", evidence)
        elif step.kind == "hardening":
            results[case] = hardening_result(step, case, data, returncode, evidence)
        else:
            ok = returncode == 0 and data.get("passed", True) and data.get("exit_code", 0) == 0
            results[case] = row("PASS" if ok else "FAIL", evidence)
        if results[case]["status"] == "PASS" or (step.kind == "profile" and results[case]["status"] == "PARTIAL"):
            missing = [name for name in supporting_files(step, data, case)
                       if not (source.parent / name).resolve().is_relative_to(source.parent.resolve())
                       or not (source.parent / name).is_file()]
            if missing:
                results[case] = row("FAIL", evidence, "missing evidence files: " + ", ".join(missing))
    if returncode and all(record["status"] == "PASS" for record in results.values()):
        return {case: row("FAIL", evidence, f"probe exited {returncode}") for case in step.ids}
    return results
