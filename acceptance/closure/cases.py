"""Phase 2A matrix entries and the existing executable probes that exercise them."""
from dataclasses import dataclass
from pathlib import Path

from acceptance.phase2a.regression.run import GATE_OPERATIONS
from acceptance.closure.archive import read


ROOT = Path(__file__).resolve().parents[2]
PHASE = ROOT / "acceptance/phase2a"
CLOSURE = ROOT / "acceptance/closure"
ORIGINAL = tuple(f"P2A-{index:02}" for index in range(1, 16))
USAGE = tuple(f"U{index:02}" for index in range(1, 9))
PROFILE = ("T01", "T02", "T03")
DATABASE = ("DB01", "DB02")
AUTOMATION = ("AC01", "AC02", "AC03")
IDLE = ("IDLE01", "IDLE02", "IDLE03")
CROSS = (*ORIGINAL[:7], "P2A-14")
MATRIX = (*ORIGINAL, *USAGE, *PROFILE, *DATABASE, *AUTOMATION, *IDLE, "REG01")
SUPPLEMENTAL = ("OFFICIAL-USAGE", "REAL_RESOURCES", "LEGACY-LEDGER")
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


def plan(args, selected, raw, revision):
    steps = []
    def add(name, ids, command, receipt, *, classes=SCRIPT, kind="exit", requires=(),
            namespace=False, cwd=ROOT):
        requested = tuple(item for item in ids if item in selected)
        if requested:
            steps.append(Step(name, requested, [str(arg) for arg in command], classes, receipt,
                              kind, tuple(requires), namespace, cwd))

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
    for case in (*PROFILE, "REAL_RESOURCES"):
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
        raw / "legacy-ledger/result.json"], "result.json")
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
    if step.name == "sqlite":
        return ("gx.db", "metrics.jsonl", "summary.json")
    if step.name == "performance":
        return [data[key]["file"] for key in ("raw_owner_samples", "raw_case_observations")]
    return ()


def observed(step, directory, returncode):
    source = directory / step.receipt
    evidence = f"{step.name}/{step.receipt}"
    if not source.is_file():
        return {case: row("FAIL", f"{step.name}/console.log", f"missing {step.receipt}") for case in step.ids}
    if step.kind == "unittest":
        import re
        ok = returncode == 0 and re.search(r"^Ran [1-9]\d* tests? in ", source.read_text(), re.M)
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
        else:
            ok = returncode == 0 and data.get("passed", True) and data.get("exit_code", 0) == 0
            results[case] = row("PASS" if ok else "FAIL", evidence)
        if results[case]["status"] == "PASS":
            missing = [name for name in supporting_files(step, data, case) if not (source.parent / name).is_file()]
            if missing:
                results[case] = row("FAIL", evidence, "missing evidence files: " + ", ".join(missing))
    if returncode and all(record["status"] == "PASS" for record in results.values()):
        return {case: row("FAIL", evidence, f"probe exited {returncode}") for case in step.ids}
    return results
