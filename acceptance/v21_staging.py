"""Exercise a real v2 P0 project through a staged v2.1 upgrade, backup restore and cold resume."""
import argparse
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tarfile
import time
from unittest.mock import patch

from acc1_upgrade import inspect
from e2e_grid import fixture_env
import e2e_responses

ROOT = Path(__file__).resolve().parents[1]
BASE = "2314eed72822387e03378e8e676ad5fa9228a8d0"
PRODUCT = ("bin", "gx-h", "examples", "codex_adapter")
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", "node_modules", "var", ".venv", ".pytest_cache")


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def check(condition, message):
    if not condition:
        raise AssertionError(message)


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def stage_source(work, dest, revision, *, full_release=False):
    dest.mkdir()
    if revision == "WORKTREE":
        for dirname in PRODUCT:
            shutil.copytree(ROOT / dirname, dest / dirname, symlinks=True, ignore=SKIP)
        if full_release:
            for dirname in ("scripts", "release-assets"):
                shutil.copytree(ROOT / dirname, dest / dirname, ignore=shutil.ignore_patterns(
                    "__pycache__", "codex-0.169.0-linux-x64.tgz"))
            manifest = Path("acceptance/runs/schema/schema.sha256")
            (dest / manifest).parent.mkdir(parents=True)
            shutil.copyfile(ROOT / manifest, dest / manifest)
    else:
        archive = work / (revision + ".tar")
        with archive.open("wb") as stream:
            members = () if full_release else PRODUCT
            subprocess.run(["git", "archive", "--format=tar", revision, *members],
                           cwd=ROOT, stdout=stream, check=True)
        with tarfile.open(archive) as tar:
            check(tar.pax_headers.get("comment") == revision, "Git archive commit stamp differs")
            tar.extractall(dest, filter="data")
    digest = hashlib.sha256()
    count = 0
    paths = (path for dirname in PRODUCT for path in (dest / dirname).rglob("*"))
    for path in sorted(paths):
        if path.is_file() or path.is_symlink():
            value = ("symlink:" + os.readlink(path) if path.is_symlink() else sha(path))
            executable = "x" if path.stat().st_mode & 0o111 else "-"
            digest.update((path.relative_to(dest).as_posix() + "\0" + executable + value + "\n").encode())
            count += 1
    return {"git_commit": revision, "files": count, "runtime_tree_sha256": digest.hexdigest()}


def read_db(state):
    conn = sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    return conn


def original_facts(state):
    view = inspect(state)
    with closing(read_db(state)) as db:
        def rows(sql):
            return [dict(row) for row in db.execute(sql)]
        view["original_budget_lines"] = rows("SELECT budget_ref,run_id,spent_json,reserved_json FROM budgets ORDER BY rowid")
        view["tasks"] = rows("SELECT task_id,kind,status,attempt,attempts_consumed,brief_ref,brief_sha256,"
                             "budget_ref,owner,lease_id,created_at FROM tasks ORDER BY rowid")
        view["leases"] = rows("SELECT lease_id,task_id,owner,principal,issued_at,released_at,attempt FROM leases ORDER BY rowid")
        view["terminal_reservations"] = rows("SELECT reservation_ref,amounts_json,subject_kind,subject_id,status "
                                              "FROM budget_reservations WHERE status != 'HELD' ORDER BY rowid")
        view["usage"] = rows("SELECT thread_id,turn_id,total_tokens,terminal_status FROM p0_root_turn_usage ORDER BY rowid")
        for item in db.execute("SELECT artifact_ref,namespace,rel_path,bytes,sha256 FROM artifacts WHERE published=1"):
            file = state / "artifacts" / item["namespace"] / item["rel_path"]
            check(file.stat().st_size == item["bytes"] and sha(file) == item["sha256"], "changed artifact " + item["artifact_ref"])
    return view


def migrations(state):
    with closing(read_db(state)) as db:
        check(db.execute("PRAGMA integrity_check").fetchall()[0][0] == "ok", "SQLite integrity check failed")
        check(not db.execute("PRAGMA foreign_key_check").fetchall(), "SQLite foreign-key check failed")
        return dict(db.execute("SELECT key,value FROM schema_meta WHERE key LIKE 'p0.migration.%' ORDER BY key"))


def quiescent_copy(source, dest):
    """Copy ordinary state + native thread files; use SQLite backup for every SQLite DB."""
    dest.mkdir()
    databases = {}
    omitted = []
    for path in source.rglob("*"):
        if path.is_symlink() or not stat.S_ISREG(path.lstat().st_mode):
            continue
        with path.open("rb") as stream:
            if stream.read(16) == b"SQLite format 3\0":
                databases[path] = path.relative_to(source)
    companions = {Path(str(path) + suffix) for path in databases for suffix in ("-wal", "-shm", "-journal")}
    for path in sorted(source.rglob("*")):
        out = dest / path.relative_to(source)
        if path in companions or path.name == ".native.lock":
            continue
        if path.is_symlink():
            out.symlink_to(os.readlink(path))
        elif path.is_dir():
            out.mkdir(exist_ok=True)
        elif path in databases:
            with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as src, closing(sqlite3.connect(out)) as dst:
                src.backup(dst)
                check(dst.execute("PRAGMA quick_check").fetchall() == [("ok",)], "Invalid snapshot " + path.name)
        elif path.is_file():
            shutil.copy2(path, out)
        else:
            omitted.append(path.relative_to(source).as_posix())
    return {"sqlite_online_backups": sorted(p.as_posix() for p in databases.values()), "omitted_special_entries": omitted}


def job_sidecars(state, jobs):
    receipts = {}
    for job in jobs:
        matches = list(state.glob("**/" + job["job_id"] + "/launch.claim"))
        check(len(matches) == 1, "Expected one launch claim for " + job["job_id"])
        folder = matches[0].parent
        outcome = json.loads((folder / "outcome.json").read_text())
        check(outcome["result"] == "exited" and outcome["exit_code"] == 0, "CPU tool failed")
        receipts[job["job_id"]] = {name: sha(folder / name) for name in ("launch.claim", "runner.json", "outcome.json")}
    return receipts


def assert_preserved(before, after):
    check({**before["run"], "status": after["run"]["status"]} == after["run"], "Old workflow pin or run changed")
    for field in ("roots", "usage", "submissions", "decisions", "settlements", "charges", "paid", "tasks", "leases",
                  "terminal_reservations"):
        for record in before[field]:
            check(record in after[field], "Original " + field + " entry no longer present")
    for ref, digest in before["artifacts"].items():
        check(after["artifacts"].get(ref) == digest, "Original artifact changed: " + ref)
    for job in before["jobs"]:
        check(any({**other, "status": job["status"]} == job for other in after["jobs"]),
              "Job, reservation, input, idempotency key or scheduler binding changed")


def receipt(view):
    result = dict(view)
    for section in ("artifacts", "charges", "terminal_reservations"):
        entries = result.pop(section)
        result[section + "_count"] = len(entries)
        canonical = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
        result[section + "_sha256"] = hashlib.sha256(canonical).hexdigest()
    return result


def run(work, revision, evidence):
    install = work / "install"
    state = work / "project"
    candidate = work / "candidate-install"
    evidence["baseline"] = stage_source(work, install, BASE)
    evidence["candidate"] = stage_source(work, candidate, revision, full_release=True)
    lock = json.loads((ROOT / "release-assets/lock.json").read_text())["linux_x64"]
    binary = work / "runtime/codex"
    binary.parent.mkdir()
    shutil.copyfile(ROOT / "vendor/codex-runtime/package" / lock["binary"], binary)
    binary.chmod(0o755)
    check(sha(binary) == lock["binary_sha256"], "Staged Codex executable differs from locked ELF")
    evidence["codex_sha256"] = sha(binary)
    archive = candidate / "release-assets" / lock["file"]
    shutil.copyfile(ROOT / "release-assets" / lock["file"], archive)
    check(sha(archive) == lock["sha256"], "Candidate carries a different official npm archive")
    setup = subprocess.run([sys.executable, "-B", "scripts/release_assets.py", "all"], cwd=candidate,
                           capture_output=True, text=True, check=True)
    installed = json.loads(setup.stdout)
    staged_binary = install / Path(installed["codex"]["official_codex"]).relative_to(candidate)
    check(installed["codex"]["binary_sha256"] == sha(binary), "Staged old and new Codex hashes differ")
    installed["codex"]["official_codex"] = staged_binary.relative_to(install).as_posix()
    evidence["installed_release_assets"] = installed
    node = subprocess.run(["node", "--experimental-strip-types", "--test", "runtime/plugin-host/host.test.ts"],
                          cwd=candidate / "gx-h", capture_output=True, text=True, timeout=45)
    check(node.returncode == 0 and "# pass 2" in node.stdout, "Staged optional Cordis evaluation failed")
    evidence["optional_cordis"] = {"command":["node", "--experimental-strip-types", "--test", "runtime/plugin-host/host.test.ts"],
                                   "cwd":"install/gx-h", "exit":node.returncode, "passed":2,
                                   "node":subprocess.check_output(["node","--version"],text=True).strip()}
    venv = work / "venv"
    subprocess.run([sys.executable, "-m", "venv", "--copies", str(venv)], check=True, stdout=subprocess.DEVNULL)
    python = str(venv / "bin/python")
    subprocess.run([python, "-m", "pip", "--isolated", "--disable-pip-version-check", "install", "--no-index",
                    "--no-cache-dir", "--require-hashes", "--find-links", str(candidate / "release-assets/python"),
                    "-r", str(candidate / "release-assets/python/requirements.lock")], stdout=subprocess.DEVNULL, check=True)
    evidence["python"] = json.loads(subprocess.check_output([python, "-I", "-B", "-c",
        "import sys,Bio,numpy,sqlite3,json; print(json.dumps(dict(python=sys.version.split()[0],biopython=Bio.__version__,numpy=numpy.__version__,sqlite=sqlite3.sqlite_version)))"], text=True))
    from v21_closure import install_observer, remove_observer
    trace, stage_marker, observer = install_observer(venv, install, work)
    native_action = e2e_responses.fixture_action

    def pace(body, ident):
        event, facts = native_action(body, ident)
        if (facts.get("round"), facts.get("output_kind"), facts.get("tool")) == (0, "science_decision", "gx_submit_product"):
            args = json.loads(event["arguments"])
            for grid in args["product"]["next_grids"].values():
                grid["delay_seconds"] = 5
            event = {**event, "arguments": json.dumps(args)}
        return event, facts

    with (e2e_responses.local_responses(work / "requests.jsonl") as peer,
          patch.object(e2e_responses, "fixture_action", pace), ExitStack() as resources):
        resources.callback(remove_observer, observer)
        env = fixture_env(work, peer, str(binary))
        env["PATH"] = str(venv / "bin") + os.pathsep + env["PATH"]
        evidence["codex_version"] = subprocess.check_output([str(binary), "--version"], env=env, text=True).strip()
        check(evidence["codex_version"] == "codex-cli 0.169.0", "Official Codex version mismatch")
        def cmd(verb, args):
            return [str(install / "bin/gx-p0"), verb, "--state", str(state), *args]

        def invoke(stage, verb, *args):
            stage_marker.write_text(stage)
            env["GX_P0_CODEX"] = str(staged_binary if stage.startswith("candidate") else binary)
            started, count = time.monotonic(), peer.request_count
            proc = subprocess.run(cmd(verb, args), cwd=install, env=env, text=True, capture_output=True, timeout=180)
            entry = dict(stage=stage, command=["bin/gx-p0", verb, "--state", "project", *args], exit=proc.returncode,
                         seconds=round(time.monotonic()-started, 3), model_requests=peer.request_count-count)
            evidence["commands"].append(entry)
            if proc.returncode:
                raise RuntimeError(f"{stage} {verb} failed: {proc.stderr[-1500:]} {proc.stdout[-800:]}")
            reply = json.loads(proc.stdout)
            entry["status"] = reply.get("status")
            return reply

        begun = invoke("original-v2", "start", "--demo-grid")
        observed = time.monotonic()
        request_start = peer.request_count
        with subprocess.Popen(cmd("run", ["--timeout","100"]), cwd=install, env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as original:
            while True:
                active = inspect(state)
                if (len(active["decisions"]) == 1 and len(active["jobs"]) == 3
                        and active["jobs"][-1]["status"] in {"RUNNING", "SUBMITTED"}
                        and all(r["status"] == "SETTLED" for r in active["paid"])):
                    break
                if original.poll() is not None or time.monotonic() - observed >= 90:
                    raise RuntimeError("v2 did not reach a third runnable Job, last run: " + str(active["run"]))
                time.sleep(.05)
            # Keep an old *reader* open. The committed pause is newer than its view and remains in WAL.
            reader = resources.enter_context(closing(read_db(state)))
            old_view = reader.execute("SELECT status FROM runs").fetchone()[0]
            check(reader.execute("PRAGMA journal_mode").fetchone()[0] == "wal" and old_view != "PAUSED",
                  "No previous reader view for WAL backup")
            stopped = invoke("original-v2", "stop")
            stdout, stderr = original.communicate(timeout=35)
            check(original.returncode == 0 and json.loads(stdout)["status"] == stopped["status"] == "PAUSED", stderr[-800:])
            evidence["commands"].append(dict(stage="original-v2", command=["bin/gx-p0","run","--state","project",
                "--timeout","100"], exit=original.returncode, seconds=round(time.monotonic()-observed,3),
                status="PAUSED", model_requests=peer.request_count-request_start))
        before = original_facts(state)
        legacy_migrations = migrations(state)
        check(before["run"]["workflow_version"] == "2" and len(before["submissions"]) == 2, "Wrong original v2 project")
        check(len(before["jobs"]) == 3 and len(before["paid"]) == 3, "Expected paid turns and live Job in v2 DB")
        until = time.monotonic() + 40
        while len(list(state.rglob("outcome.json"))) < len(before["jobs"]):
            check(time.monotonic() < until, "Detached CPU Job never produced a terminal outcome")
            time.sleep(.1)
        original_sidecars = job_sidecars(state, before["jobs"])
        backup = work / "v2-backup"
        backup.mkdir()
        provider_state = Path(env["CODEX_HOME"])
        wal_bytes = (state / "gx.db-wal").stat().st_size
        check(wal_bytes > 0 and reader.execute("SELECT status FROM runs").fetchone()[0] == old_view,
              "Old read snapshot or committed WAL disappeared before backup")
        evidence["backup"] = {"source_wal_bytes":wal_bytes, "pre_pause_reader_status":old_view,
                              "backup_run_status":before["run"]["status"],
                              "project": quiescent_copy(state, backup / "project"),
                              "codex": quiescent_copy(provider_state, backup / "codex"),
                              "job_sidecar_digests": original_sidecars,
                              "database_sha256": sha(backup / "project/gx.db")}
        check(original_facts(backup / "project") == before, "Snapshot differs from paused v2")
        reader.close()
        evidence["original_project"] = receipt(before)
        ident = (state / "gx.db").stat().st_ino
        install.rename(work / "original-install")
        candidate.rename(install)
        for stage, entries in (("candidate-cli-help", ("bin/gx-p0",)),
                               ("candidate-compat-help", ("gx-h/bin/gx", "gx-h/bin/gx-native",
                                    "gx-h/bin/gx-project", "gx-h/bin/gx-web", "gx-h/bin/gx-science"))):
            stage_marker.write_text(stage)
            for entry in entries:
                start_clock = time.monotonic()
                proc = subprocess.run([str(install / entry), "--help"], cwd=install, env=env,
                                      capture_output=True, text=True, timeout=20)
                check(proc.returncode == 0 and "usage:" in proc.stdout.lower(), f"Staged {entry} help failed")
                evidence["commands"].append({"stage":stage, "command":[entry,"--help"], "exit":proc.returncode,
                                            "seconds":round(time.monotonic()-start_clock,3)})
        # A public writable operation applies and verifies the actual new migrations before rollback.
        migrate = invoke("candidate-before-rollback", "stop")
        upgraded_migrations = migrations(state)
        check(migrate["status"] == "PAUSED", "Candidate did not open original project")
        check(set(legacy_migrations.items()) <= set(upgraded_migrations.items()), "An existing migration changed")
        added = dict(sorted(set(upgraded_migrations.items()) - set(legacy_migrations.items())))
        check(added, "No v2.1 migration was applied to old DB")
        check(ident == (state / "gx.db").stat().st_ino, "Upgrade rewrote original DB file")
        after_migration = original_facts(state)
        assert_preserved(before, after_migration)
        for budget in before["original_budget_lines"]:
            check(budget in after_migration["original_budget_lines"], "Migration changed original paid or reserved balance")
        check(job_sidecars(state, before["jobs"]) == original_sidecars, "Upgrade changed a scheduler receipt")
        evidence["migration"] = {"new_marker_sha256": added, "original_marker_count": len(legacy_migrations),
                                 "same_database_inode": True, "original_budget_balances_exact": True}
        # Quiesced machines only. Retain the candidate image + entire upgraded state separately.
        install.rename(candidate)
        (work / "original-install").rename(install)
        state.rename(work / "retained-upgraded-project")
        provider_state.rename(work / "retained-upgraded-codex")
        shutil.copytree(backup / "project", state, symlinks=True)
        shutil.copytree(backup / "codex", provider_state, symlinks=True)
        check(sha(state / "gx.db") == evidence["backup"]["database_sha256"], "Restored DB differs from actual backup")
        first = invoke("restored-original-v2", "status")
        paused = invoke("restored-original-v2", "run", "--timeout", "15")
        check(first["status"] == paused["status"] == "PAUSED", "Original v2 did not reopen restored project")
        check(migrations(state) == legacy_migrations, "Restored v2 schema differs from original v2")
        check(original_facts(state) == before, "Original v2 changed paid / accepted work on cold reopen")
        check(job_sidecars(state, before["jobs"]) == original_sidecars, "Lost a scheduler receipt on restore")
        evidence["rollback"] = {"before_new_external_work": True, "v2_database_exactly_restored": True,
                                "original_schema_restored": True, "old_public_run_status": paused["status"],
                                "all_original_project_facts_equal": True, "all_job_launch_and_result_files_equal": True,
                                "retained_migrated_copy": "retained-upgraded-project"}
        # Final staging resumes the restored v2 state once. It consumes the existing third Job outcome.
        install.rename(work / "original-install")
        candidate.rename(install)
        optional = install / "gx-h/runtime/plugin-host/node_modules"
        parked = work / "optional-cordis-offline"
        with ExitStack() as restore_optional:
            optional.rename(parked)
            restore_optional.callback(parked.rename, optional)
            resumed = invoke("candidate-final", "resume", "--timeout", "145")
        evidence["optional_cordis"]["grid_resumed_with_plugin_node_modules_absent"] = True
        check(resumed["status"] == "COMPLETED" and resumed["run_id"] == begun["run_id"], str(resumed)[-500:])
        final = original_facts(state)
        check(migrations(state) == upgraded_migrations, "Final candidate did not apply the same migration set")
        assert_preserved(before, final)
        receipts = job_sidecars(state, final["jobs"])
        check(len(receipts) == len({j["idempotency_key"] for j in final["jobs"]}) == 4, "Duplicate/missing CPU Job")
        check(all(receipts[k] == v for k, v in original_sidecars.items()), "Cold continuation relaunched a Job")
        check(all(j["status"] == "SUCCEEDED" for j in final["jobs"]), "Original or new Job did not settle")
        check(len(final["submissions"]) == 6 and len(final["decisions"]) == 3, "Wrong number of accepted products")
        check(not resumed["unsettled_turns"] and resumed["report_ref"], "Missing final report or unresolved turn")
        count = peer.request_count
        replay = invoke("candidate-final", "run", "--timeout", "15")
        check(replay["status"] == "COMPLETED" and original_facts(state) == final and count == peer.request_count,
              "Terminal cold replay changed state or spent model calls")
        original_root = before["original_budget_lines"][0]["budget_ref"]
        run_budget, = [entry for entry in final["original_budget_lines"] if entry["budget_ref"] == original_root]
        spent = json.loads(run_budget["spent_json"])
        reservations = json.loads(run_budget["reserved_json"])
        usage = {}
        for turn in final["usage"]:
            usage[turn["thread_id"]] = max(usage.get(turn["thread_id"], 0), turn["total_tokens"])
        check(sum(usage.values()) == spent["tokens"] == count * 35, "Provider receipts and original root ledger differ")
        check(spent["api_calls"] == len(final["usage"]) and spent["job_submissions"] == len(receipts),
              "Original root ledger includes missing or duplicate calls/Jobs")
        check(not any(reservations.values()), "Original root ledger still holds reservations")
        evidence["final_accounting"] = {"original_budget_ref":original_root, "settled_tokens":spent["tokens"],
                                        "official_thread_cumulative_total":sum(usage.values()), "scripted_provider_reported":count * 35,
                                        "settled_native_turns":spent["api_calls"], "settled_job_submissions":spent["job_submissions"],
                                        "reserved":reservations}
        evidence["continuation"] = {"final_project": receipt(final), "final_report_ref": resumed["report_ref"],
                                    "original_job_receipts_preserved": len(original_sidecars),
                                    "total_cpu_jobs": len(receipts), "total_turns": len(final["usage"]),
                                    "total_scripted_responses": count, "terminal_replay_new_calls": peer.request_count-count,
                                    "terminal_replay_project_identical": True}
    from v21_closure import observe
    evidence["dependency_trace"] = observe(install, trace, work / "closure.json")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True, help="New staging directory. Holds private state and rollback copies.")
    parser.add_argument("--output", type=Path, required=True, help="Shareable redacted report")
    parser.add_argument("--candidate", default="HEAD", help="Git commit, or WORKTREE for an explicitly uncommitted trial")
    args = parser.parse_args()
    work = args.work.expanduser().resolve()
    work.mkdir(parents=True, exist_ok=False)
    candidate = args.candidate if args.candidate == "WORKTREE" else git("rev-parse", "--verify", args.candidate + "^{commit}")
    result = {"mode":"official pinned Codex App Server, scripted loopback Responses, real detached CPU Jobs and SQLite",
              "started": now(), "commands": [], "result": "FAIL"}
    try:
        run(work, candidate, result)
        result["result"] = "PASS"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        result["ended"] = now()
        save(args.output, result)


if __name__ == "__main__":
    main()
