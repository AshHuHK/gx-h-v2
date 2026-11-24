"""Run the Phase 2A workloads and retain individually verifiable evidence."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid

from acceptance.closure import archive
from acceptance.closure.cases import IDLE, MATRIX, ORIGINAL, PHASE, ROOT, SUPPLEMENTAL, observed, plan, row
from acceptance.closure.profile import test_environment


CLI = ROOT / "bin/gx-acceptance"
DEFAULT_RUNS = ROOT / "var/acceptance"
STATUS_CODES = {"PASS": 0, "NO_FAILURES": 0, "FAIL": 1, "PARTIAL": 2, "BLOCKED": 2}
SOURCE_PATHS = ("gx-h", "bin", "codex_adapter", "examples", "scripts", "release-assets", ":(glob)acceptance/**/*.py")


def utc():
    return datetime.now(timezone.utc).isoformat()


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT)


def configure(args):
    default_cache = ROOT if (ROOT / "vendor/codex-runtime").is_dir() else ROOT.parent / "gx-h-v-3"
    args.cache = (args.cache or default_cache).resolve()
    lock = archive.read(ROOT / "release-assets/lock.json")["linux_x64"]
    args.binary = (args.binary or args.cache / "vendor/codex-runtime/package" / lock["binary"]).absolute()
    args.python = (args.python or args.cache / ".venv/bin/python").absolute()
    args.clash = (args.clash or shutil.which("mihomo") or ROOT.parent.parent / "clash/clash.new")
    args.clash = Path(args.clash).absolute()
    args.source_db = (args.source_db or args.cache / "var/v3-staging/frozen-a05dcef/snapshot/project/gx.db").resolve()
    args.historical_briefs = (args.historical_briefs or ROOT.parent /
                              "scratchpad/v3-real-performance/p1-2476f2b-xhigh").resolve()
    args.catalog = (args.catalog or args.historical_briefs / "codex-home/models_cache.json").resolve()
    if args.original_failures:
        args.original_failures = args.original_failures.resolve()


def environment(args):
    changes = hashlib.sha256(git("diff", "--binary", "HEAD", "--", *SOURCE_PATHS))
    for name in sorted(filter(None, git("ls-files", "--others", "--exclude-standard", "-z", "--", *SOURCE_PATHS).split(b"\0"))):
        changes.update(name + b"\0")
        changes.update(archive.sha256(ROOT / os.fsdecode(name)).encode())
    info = {
        "started_utc": utc(), "repo": str(ROOT), "sha": git("rev-parse", "HEAD").decode().strip(),
        "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
        "branch": git("branch", "--show-current").decode().strip(),
        "dirty_state": git("status", "--short").decode().splitlines(),
        "worktree_code_delta_sha256": changes.hexdigest(),
        "platform": platform.platform(), "machine": platform.machine(),
        "launcher": {"executable": sys.executable, "python": platform.python_version(), "sqlite": sqlite3.sqlite_version},
        "paths": {key: str(getattr(args, key)) for key in
                  ("cache", "python", "binary", "clash", "source_db", "historical_briefs", "catalog")},
        "original_failures": str(args.original_failures) if args.original_failures else None,
        "invocation": sys.argv, "invocation_cwd": os.getcwd(),
        "classes": ["SCRIPTED", "OFFICIAL_HOST_LOOPBACK", "LIVE_MODEL", "REAL_COMPUTE", "INDEPENDENT_REVIEW"],
    }
    if args.python.is_file() and os.access(args.python, os.X_OK):
        command = [args.python, "-I", "-c", "import json,platform,sqlite3; "
                   "print(json.dumps({'python':platform.python_version(),'sqlite':sqlite3.sqlite_version}))"]
        with tempfile.TemporaryDirectory(prefix="gx-acceptance-version-") as work:
            env = test_environment(Path(work), python=args.python)
            result = subprocess.run(command, capture_output=True, text=True, env=env)
        if result.returncode == 0:
            info["test_interpreter"] = json.loads(result.stdout)
    return info


def run_path(parent, run_id):
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}", run_id):
        raise ValueError("invalid acceptance run ID")
    return parent / run_id


def runout(steps):
    if not steps:
        return "NO_FAILURES"
    states = {record["status"] for record in steps.values()}
    if "FAIL" in states:
        return "FAIL"
    if states == {"BLOCKED"}:
        return "BLOCKED"
    if states != {"PASS"}:
        return "PARTIAL"
    return "PASS"


def prior_discovery(previous, prior, info, scratch):
    # Only reuse already passing discovery when the unfinished work is historical adjudication.
    item = prior["cases"].get("T03", {})
    if (item.get("status") != "PARTIAL" or prior.get("sha") != info["sha"]
            or prior.get("code_delta") != info["worktree_code_delta_sha256"]):
        return None
    prefix = "T03/evidence/suite/"
    names = ("summary.json", "python-full.log")
    catalog = archive.read_manifest(previous / "raw-files.sha256")
    if any(prefix + name not in catalog for name in names):
        return None
    dest = scratch / "prior-discovery"
    dest.mkdir()
    with tarfile.open(previous / "raw.tar.gz", "r:gz") as packed:
        for name in names:
            with packed.extractfile(prefix + name) as source, (dest / name).open("xb") as output:
                shutil.copyfileobj(source, output)
    return dest


def execute_step(args, step, raw, scratch, interrupted, previous_suite):
    directory = raw / step.name
    directory.mkdir()
    console = directory / "console.log"
    argv = step.argv
    if step.name == "T03":
        suite = raw / "v31-regression/evidence/suite"
        if not (suite / "summary.json").is_file() or not (suite / "python-full.log").is_file():
            suite = previous_suite
        if suite is not None:
            argv = [*argv, "--suite-evidence", str(suite)]
    if step.namespace:
        argv = [str(args.python), "-B", str(PHASE / "regression/loopback.py"), "--clash", str(args.clash),
                "--receipt", str(directory / "network.json"), "--", *argv]
    profile = "real" if "REAL_RESOURCES" in step.ids else "deterministic"
    env = test_environment(scratch / step.name, profile=profile, python=args.python, binary=args.binary)
    if step.name == "sqlite":
        env["PYTHONPATH"] = str(ROOT / "gx-h")
    if step.name in IDLE:
        env["GX_IDLE_EVIDENCE"] = str(directory / "evidence")
    receipt = {"name": step.name, "cases": list(step.ids), "classes": list(step.classes),
               "argv": argv, "cwd": str(step.cwd), "profile": profile, "started_utc": utc(),
               "source": "named Git commit" if step.name in {"v31-regression", "T03"} else "working tree",
               "environment": {k: v for k, v in env.items() if k in {
                   "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "GX_TEST_RESOURCE_PROFILE",
                   "CUDA_VISIBLE_DEVICES", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
                   "LANG", "TZ", "PIP_NO_INDEX"}},
               "console": f"{step.name}/console.log", "returncode": None, "executed": False}
    if step.namespace:
        receipt["network_receipt"] = f"{step.name}/network.json"
    archive.save(directory / "invocation.json", receipt)
    started = time.monotonic()
    missing = [str(path) for path in (args.python, *step.requirements, *((args.clash,) if step.namespace else ()))
               if not path.exists()]
    try:
        if interrupted:
            raise InterruptedError("not started after interruption")
        if missing:
            raise FileNotFoundError("missing local input: " + ", ".join(missing))
        with console.open("w") as log:
            receipt["executed"] = True
            completed = subprocess.run(argv, cwd=step.cwd, env=env, stdout=log, stderr=subprocess.STDOUT)
        receipt["returncode"] = completed.returncode
        if step.namespace and not (directory / "network.json").is_file():
            results = {case: row("BLOCKED", receipt["console"], "loopback namespace did not start")
                       for case in step.ids}
        else:
            results = observed(step, directory, completed.returncode)
    except KeyboardInterrupt:
        receipt["interrupted"] = True
        with console.open("a") as log:
            print("acceptance interrupted", file=log)
        results = {case: row("BLOCKED", receipt["console"], "interrupted during execution") for case in step.ids}
    except (FileNotFoundError, PermissionError, InterruptedError) as error:
        if receipt["returncode"] is None:
            receipt["executed"] = False
        with console.open("a") as log:
            print(str(error), file=log)
        results = {case: row("BLOCKED", receipt["console"], str(error)) for case in step.ids}
    except (OSError, ValueError, KeyError, TypeError) as error:
        with console.open("a") as log:
            print(f"receipt error: {error}", file=log)
        results = {case: row("FAIL", receipt["console"], f"invalid receipt: {error}") for case in step.ids}
    receipt.update(finished_utc=utc(), seconds=round(time.monotonic() - started, 6))
    for result in results.values():
        result.update(operation=step.name, classes=list(step.classes))
    archive.save(directory / "invocation.json", receipt)
    return receipt, results


def reproduction(args, run, failed):
    command = [str(CLI), "run", args.suite, "--runs-dir", str(run.parent), "--failed-only", run.name]
    for key in ("cache", "python", "binary", "clash", "source_db", "historical_briefs", "catalog", "original_failures"):
        if getattr(args, key):
            command.extend(["--" + key.replace("_", "-"), str(getattr(args, key))])
    return {"failed_or_incomplete": failed, "argv": command, "shell": shlex.join(command)}


def merge_observations(previous, current):
    checks = [previous, current]
    state = runout({"first": previous, "second": current})
    return {**row(state, previous["evidence"]), "operation": previous["operation"],
            "classes": sorted(set(previous["classes"] + current["classes"])), "checks": checks}


def finish_regression(cases, prior, run_id, info):
    if "REG01" not in cases:
        return
    record = cases["REG01"]
    references = {}
    if prior:
        references.update(prior["cases"].get("REG01", {}).get("original_receipts", {}))
        for case in ORIGINAL:
            if case in prior["cases"]:
                previous = prior["cases"][case]
                references[case] = {"status": previous["status"], "run_id": prior["run_id"],
                                    "sha": prior["sha"], "code_delta": prior.get("code_delta"),
                                    "evidence": previous["evidence"]}
    for case in ORIGINAL:
        if case in cases:
            references[case] = {"status": cases[case]["status"], "run_id": run_id,
                                "sha": info["sha"], "code_delta": info["worktree_code_delta_sha256"],
                                "evidence": cases[case]["evidence"]}
    record["original_receipts"] = references
    missing = [case for case in ORIGINAL if references.get(case, {}).get("sha") != info["sha"]
               or references[case].get("code_delta") != info["worktree_code_delta_sha256"]]
    failed = [case for case in ORIGINAL if references.get(case, {}).get("status") == "FAIL"]
    if record["status"] == "PASS" and failed:
        record.update(status="FAIL", reason="original regression failed: " + ", ".join(failed))
    elif record["status"] == "PASS" and (missing or any(r["status"] != "PASS" for r in references.values())):
        record.update(status="PARTIAL", reason="original regression needs receipts for this revision")


def run(args):
    runs = args.runs_dir.resolve()
    suite_ids = (*MATRIX, *(SUPPLEMENTAL if args.suite == "all" else ("OFFICIAL-USAGE",)))
    prior = None
    if args.failed_only:
        previous = run_path(runs, args.failed_only)
        archive.check(previous)
        prior = archive.read(previous / "summary.json")
        if args.suite != prior["suite"]:
            raise ValueError("retry the same suite recorded by the prior run")
        original_inputs = archive.read(previous / "environment.json")
        for key, value in {**original_inputs["paths"], "original_failures": original_inputs["original_failures"]}.items():
            if getattr(args, key) is None and value is not None:
                setattr(args, key, Path(value))
        selection = [case for case, item in prior["cases"].items() if item["status"] != "PASS"]
    else:
        selection = list(dict.fromkeys(args.case or suite_ids))
        if "REG01" in selection:
            selection = list(dict.fromkeys([*selection, *ORIGINAL]))
    unknown = set(selection) - set(suite_ids)
    if unknown:
        raise ValueError("case is outside this suite: " + ", ".join(sorted(unknown)))
    configure(args)
    info = environment(args)
    run_id = args.run_id or (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12])
    target = run_path(runs, run_id)
    target.mkdir(parents=True, exist_ok=False)
    raw = target / "raw"
    raw.mkdir()
    print(f"Run {run_id}  {target}", flush=True)
    archive.save(target / "environment.json", info)
    commands, cases = [], {}
    scope = "failed-only" if prior else ("selected" if args.case else "full")
    source = {"run_id": prior["run_id"], "manifest_sha256": archive.sha256(previous / "manifest.sha256")} if prior else None
    archive.save(raw / "selection.json", {"scope": scope, "cases": selection, "parent": source})
    interrupted = False
    with tempfile.TemporaryDirectory(prefix="gx-acceptance-runtime-") as work:
        previous_suite = prior_discovery(previous, prior, info, Path(work)) if prior and "T03" in selection else None
        for step in plan(args, selection, raw, info["sha"]):
            receipt, results = execute_step(args, step, raw, Path(work), interrupted, previous_suite)
            interrupted |= bool(receipt.get("interrupted"))
            commands.append(receipt)
            for case, result in results.items():
                cases[case] = merge_observations(cases[case], result) if case in cases else result
            archive.save(target / "commands.json", commands)
            print(f"{step.name}  " + "  ".join(f"{case}={record['status']}" for case, record in results.items()),
                  flush=True)
    if set(cases) != set(selection):
        raise RuntimeError("the selected acceptance matrix does not match the executable cases")
    finish_regression(cases, prior, run_id, info)
    failed = [case for case in selection if cases[case]["status"] != "PASS"]
    status = runout(cases)
    summary = {"run_id": run_id, "suite": args.suite, "scope": scope, "sha": info["sha"], "status": status,
               "started_utc": info["started_utc"], "finished_utc": utc(), "parent": source,
               "cases": cases, "failed_or_incomplete": failed, "code_delta": info["worktree_code_delta_sha256"],
               "original_count": len(set(cases) & set(ORIGINAL)), "matrix_count": len(set(cases) & set(MATRIX)),
               "supplemental_count": len(set(cases) & set(SUPPLEMENTAL))}
    archive.save(target / "summary.json", summary)
    archive.save(target / "commands.json", commands)
    archive.save(target / "reproduce.json", reproduction(args, target, failed))
    archive.package(target, raw)
    verification = archive.check(target)
    shutil.rmtree(raw)
    print(f"{status}  {run_id}  {verification['raw_files']} raw files  {verification['raw_sha256']}", flush=True)
    return 130 if interrupted else STATUS_CODES[status]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="action", required=True)
    execute = subs.add_parser("run", help="execute all selected cases and archive a new run")
    execute.add_argument("suite", choices=("phase2a", "all"))
    group = execute.add_mutually_exclusive_group()
    group.add_argument("--case", action="append", choices=(*MATRIX, *SUPPLEMENTAL), help="repeatable case selector")
    group.add_argument("--failed-only", "--only-failed", metavar="RUN_ID",
                       help="retry only failed, blocked or partial cases in a verified prior run")
    execute.add_argument("--run-id", help="optional new run ID; existing directories are preserved")
    for name in ("cache", "python", "binary", "clash", "source-db", "historical-briefs", "catalog", "original-failures"):
        execute.add_argument("--" + name, type=Path)
    report = subs.add_parser("report", help="read the verified result of a run")
    report.add_argument("run_id")
    report.add_argument("--json", action="store_true")
    verify = subs.add_parser("verify", help="check the package and each decompressed file")
    verify.add_argument("run_id")
    verify.add_argument("--extract-to", type=Path, help="unpack verified raw evidence into a new directory")
    for action in (execute, report, verify):
        action.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS)
    args = parser.parse_args(argv)
    try:
        if args.action == "run":
            return run(args)
        target = run_path(args.runs_dir.resolve(), args.run_id)
        verification = archive.check(target, getattr(args, "extract_to", None))
        if args.action == "verify":
            print(json.dumps(verification, ensure_ascii=False))
            return 0
        summary = archive.read(target / "summary.json")
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
        else:
            print(f"{summary['run_id']}  {summary['status']}  {summary['sha']}  {summary['scope']}")
            for case, record in summary["cases"].items():
                print(f"{case:16} {record['status']:8} {','.join(record['classes'])}  {record['evidence']}")
            if summary["failed_or_incomplete"]:
                print(archive.read(target / "reproduce.json")["shell"])
        return 0
    except (ValueError, OSError, EOFError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print(f"gx-acceptance: {error}", file=sys.stderr)
        return 2
