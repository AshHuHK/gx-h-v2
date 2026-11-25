"""Cold-open released public V6.1 and A projects through the B CLI and restore the originals."""

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "acceptance/v62a/upgrade"))
LEGACY_ADMISSION = "gx.execution-manifest.admission/1"
LEGACY_COMPLETION = "gx.execution-manifest.completion/1"


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def db_read(state):
    conn = sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def check_source(repo, revision):
    subprocess.run(["git", "-C", repo, "merge-base", "--is-ancestor", revision, "HEAD"], check=True)
    # Accepted reports and release notes can be published after the tested code commit.
    runtime = ["gx-h/gx_core", "gx-h/config", "gx-h/contracts", "gx-h/adapters", "gx-h/plugins", "gx-h/runtime", "bin"]
    fixture = ["examples/v62a-data-inventory", "examples/v61-sensor-calibration", "examples/plugins/sensor_calibration",
               "examples/paired-grid.json", "release-assets/lock.json", "acceptance/v62a/official_inventory.py",
               "acceptance/v62a/module_peer.py", "acceptance/v62a/upgrade/probe.py", "acceptance/v61/adaptive/data/2O0O.cif"]
    subprocess.run(["git", "-C", repo, "diff", "--quiet", revision, "--", *runtime, *fixture], check=True)


def manifest_reader(deploy, state):
    """Exercise the installed manifest reader in its own interpreter, on a read-only database."""
    cmd = [sys.executable, "-B", str(Path(__file__).resolve()), "read", "--release", str(deploy), "--work", str(state)]
    return json.loads(subprocess.check_output(cmd, text=True))


def read_manifests(deploy, state):
    sys.path.insert(0, str(deploy / "gx-h"))
    from gx_core.p0 import manifest

    records = {}
    with db_read(state) as conn:
        for row in conn.execute("SELECT job_id,reservation_ref FROM jobs ORDER BY job_id"):
            job = row["job_id"]
            proof = manifest.read_manifest(conn, job)
            admission, completion = proof["admission"], proof["completion"]
            assert admission["schema"] == LEGACY_ADMISSION and "output_declarations" not in admission
            reservation, = conn.execute("SELECT reservation_ref,budget_ref,status FROM budget_reservations "
                                        "WHERE subject_kind='job' AND subject_id=?", (job,))
            assert reservation["reservation_ref"] == row["reservation_ref"]
            settlements = list(conn.execute("SELECT event_id,subject_id,payload_json FROM events WHERE kind='budget.settled' "
                                            "AND json_extract(payload_json,'$.reservation_ref')=?", (row["reservation_ref"],)))
            bill = {"budget_ref": reservation["budget_ref"], "reservation_ref": row["reservation_ref"], "event_id": None,
                    "actual": {"job_submissions": 0, "cpu_seconds": 0}}
            outputs = []
            if completion:
                assert completion["schema"] == LEGACY_COMPLETION and completion["status"] == "SUCCEEDED"
                settled, = settlements
                assert settled["subject_id"] == reservation["budget_ref"] and reservation["status"] == "SETTLED"
                actual = json.loads(settled["payload_json"])["actual"]
                assert actual == {"job_submissions": 1, "cpu_seconds": math.ceil(completion["measured"]["cpu_seconds"])}
                bill.update(event_id=settled["event_id"], actual=actual)
                for output in completion["outputs"]:
                    assert "nature" not in output
                    item = conn.execute("SELECT namespace,rel_path,sha256 FROM artifacts WHERE artifact_ref=? AND job_id=?",
                                        (output["artifact_ref"], job)).fetchone()
                    assert item and item["sha256"] == output["sha256"]
                    raw = state / "artifacts" / item["namespace"] / item["rel_path"]
                    with raw.open("rb") as stream:
                        assert hashlib.file_digest(stream, "sha256").hexdigest() == output["sha256"]
                    outputs.append({"ref": output["artifact_ref"], "sha256": output["sha256"]})
            else:
                assert not settlements and reservation["status"] == "HELD"
            records[job] = {"admission_schema": admission["schema"], "admission_sha256": proof["admission_hash"],
                            "completion_schema": completion and completion["schema"],
                            "completion_sha256": proof["manifest_hash"], "outputs": outputs, "settlement": bill}
    assert records
    return records


def module_facts(deploy, state):
    import probe

    record = probe.snapshot(state)
    with db_read(state) as conn:
        frozen = json.loads(conn.execute("SELECT inputs_json FROM runs").fetchone()[0])
        tables = ("v6_goals", "v6_decision_tasks", "v6_decisions", "v6_branches", "v6_nodes", "v6_job_attempts",
                  "v62_modules", "v62_module_reports", "v62_module_wakes", "p0_v6_native_calls", "budget_reservations")
        history = {}
        for table in tables:
            rows = [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            assert rows, table
            history[table] = {"rows": len(rows), "sha256": digest(rows)}
        report, = [dict(r) for r in conn.execute("SELECT module_instance_id,report_ref,version,request_sha256 FROM v62_module_reports")]
    return {**probe.closed_science(record), "history": history, "run_inputs_sha256": digest(frozen),
            "packages": frozen["plugin_binding"]["packages"], "report": report,
            "execution_manifests": manifest_reader(deploy, state)}


def verify_packages(deploy, work, packages):
    import probe

    expected = {f"{p['id']}@{p['version']}": p["sha256"] for p in packages}
    result = probe.cli(deploy, work, None, "plugin", "verify", *expected)
    observed = {f"{p['id']}@{p['version']}": p["sha256"] for p in result["plugins"]}
    assert result["failures"] == {} and expected == observed
    return observed


def seed_a(old, revision, binary, work):
    sys.path.insert(0, str(old / "acceptance/v62a/upgrade"))
    import probe

    check_source(old, revision)
    work.mkdir(mode=0o700, parents=True)
    deploy, project = work / "release", work / "module"
    probe.released(old, deploy, revision)
    sys.path.insert(0, str(old / "acceptance/v62a"))
    import official_inventory as a1

    assert probe.digest(binary) == probe.load(old / "release-assets/lock.json")["linux_x64"]["binary_sha256"]
    released_grant = old / "examples/v62a-data-inventory/grant.json"
    grant = probe.load(released_grant)
    supplied = work / "supplied-files"
    supplied.mkdir()
    for item in grant["input_files"]:
        original = (released_grant.parent / item["path"]).resolve()
        copied = supplied / original.name
        shutil.copyfile(original, copied)
        assert probe.digest(copied) == item["sha256"]
        item["path"] = copied.name
    probe.save(supplied / "grant.json", grant)
    # The frozen A1 peer drives the released CLI at the physical path B will occupy.
    a1.ROOT = deploy
    acquired = a1.run_arm(project, binary, supplied / "grant.json", "module")
    assert acquired["paid_model_calls"] == 0 and acquired["cold_resume"]["additional_jobs"] == 0
    probe.save(work / "original-a1.json", acquired)
    state = project / "state"
    with db_read(state) as conn:
        files = json.loads(conn.execute("SELECT inputs_json FROM runs").fetchone()[0])["files"]
        expected_files = {str(supplied / item["path"]): item["sha256"] for item in grant["input_files"]}
        assert {row["path"]: row["sha256"] for row in files} == expected_files
    before = module_facts(deploy, state)
    verified = verify_packages(deploy, project, before["packages"])
    assert len(before["jobs"]) == 2 and len(before["turns"]) == len(acquired["turns"])
    assert before["report"]["report_ref"] == acquired["report"]["artifact_ref"]
    probe.backup(state, work / "original-state")
    shutil.copytree(project / "codex", work / "original-codex")
    assert before == module_facts(deploy, work / "original-state")
    probe.save(work / "seed.json", {"revision": revision, "old_contract": probe.snapshot(state)["schema"]["contract_sha256"],
                                   "binary_sha256": probe.digest(binary), "packages": verified, "business": before,
                                   "supplied_sha256": {r["path"]: r["sha256"] for r in grant["input_files"]}})


def check_a(candidate, revision, work, receipt):
    import probe

    check_source(candidate, revision)
    seed = probe.load(work / "seed.json")
    before = seed["business"]
    project, deploy = work / "module", work / "release"
    state = project / "state"
    trial = Path(tempfile.mkdtemp(prefix="check-", dir=work))
    next_release, old_release = trial / "candidate", trial / "previous"
    probe.released(candidate, next_release, revision)
    target = probe.load(next_release / "gx-h/contracts/CONTRACT_SHA")["__combined__"]
    assert target != seed["old_contract"] and before == module_facts(deploy, state)
    statuses = []
    deploy.rename(old_release)
    next_release.rename(deploy)
    supplied = work / "supplied-files"
    supplied.rename(trial / "supplied-offline")
    try:
        for i in (1, 2):
            assert not any((supplied / name).exists() for name in seed["supplied_sha256"])
            reopened = probe.cli(deploy, project, state, "resume", "--run-id", before["run"]["run_id"], "--timeout", "5")
            status = probe.cli(deploy, project, state, "status", "--run-id", before["run"]["run_id"])
            assert reopened["goal_status"] == status["goal_status"] == "CLOSED"
            current = module_facts(deploy, state)
            assert current == before, [k for k in current if current[k] != before[k]]
            assert verify_packages(deploy, project, before["packages"]) == seed["packages"]
            db = probe.snapshot(state)
            assert db["schema"]["contract_sha256"] == target
            transitions = [(e["from_sha256"], e["to_sha256"]) for e in db["resync"]]
            assert transitions.count((seed["old_contract"], target)) == 1
            statuses.append({"reopen": i, "graph_head": status["graph_head"], "contract": target,
                             "business_sha256": digest(current), "resync_chain": transitions})
        assert statuses[0]["resync_chain"] == statuses[1]["resync_chain"]
    finally:
        (trial / "supplied-offline").rename(supplied)
        deploy.rename(next_release)
        old_release.rename(deploy)
        state.rename(trial / "upgraded-state")
        probe.backup(work / "original-state", state)
        (project / "codex").rename(trial / "upgraded-codex")
        shutil.copytree(work / "original-codex", project / "codex")
    cold = probe.cli(deploy, project, state, "resume", "--run-id", before["run"]["run_id"], "--timeout", "5")
    assert cold["goal_status"] == "CLOSED" and module_facts(deploy, state) == before
    assert {p: probe.digest(supplied / p) for p in seed["supplied_sha256"]} == seed["supplied_sha256"]
    assert probe.snapshot(state)["schema"]["contract_sha256"] == seed["old_contract"]
    assert verify_packages(deploy, project, before["packages"]) == seed["packages"]
    probe.save(receipt, {"status": "PASS", "source": "public A1 coordinate inventory, scripted loopback",
                         "a_revision": seed["revision"], "b_revision": revision, "old_contract": seed["old_contract"],
                         "original_business_sha256": digest(before), "reopens": statuses, "rollback_matches": True,
                         "unavailable_original_inputs_during_b_reopens": seed["supplied_sha256"],
                         "run_id": before["run"]["run_id"], "packages": seed["packages"], "report": before["report"],
                         "execution_manifests": before["execution_manifests"], "tables": before["history"],
                         "original_native_turns": len(before["turns"]), "original_budgets": before["budgets"],
                         "additional_jobs": 0, "additional_turns": 0, "additional_charges": 0, "paid_model_calls": 0,
                         "provider_tokens": None, "operator_transcript_sha256": probe.digest(project / "commands.jsonl")})


def check_v61(candidate, revision, baseline, binary, work, receipt):
    import probe

    check_source(candidate, revision)
    original = {name: manifest_reader(baseline, state) for name, state in probe.states(work).items()}
    trials = set(work.glob("trial-*"))
    retained = receipt.with_name(receipt.stem + "-original-probe.json")
    result = probe.check(candidate, baseline, binary, work, retained)
    assert result["passed"] and result["paid_model_calls"] == 0
    trial, = set(work.glob("trial-*")) - trials
    restored = {}
    for name, state in probe.states(work).items():
        old = original[name]
        upgraded = manifest_reader(trial / "candidate-install", trial / ("after-" + name))
        assert manifest_reader(baseline, state) == old and old.keys() == upgraded.keys()
        for job, before in old.items():
            after = upgraded[job]
            assert before["admission_sha256"] == after["admission_sha256"]
            assert before["settlement"]["reservation_ref"] == after["settlement"]["reservation_ref"]
            if before["completion_sha256"]:
                assert before == after
            else:
                assert name == "static" and after["completion_sha256"] and after["outputs"]
        case = result["cases"][name]
        assert case["equal_cold_open_snapshots"] and case["full_rollback_matches"]
        for account_before, account_after in zip(case["before"]["budgets"], case["after"]["budgets"], strict=True):
            account = account_before["budget_ref"]
            assert account == account_after["budget_ref"]
            for resource in ("job_submissions", "cpu_seconds", "tokens", "api_calls"):
                change = json.loads(account_after["spent_json"]).get(resource, 0) - json.loads(account_before["spent_json"]).get(resource, 0)
                applied = [sum(r["settlement"]["actual"].get(resource, 0) for r in data.values()
                               if r["settlement"]["budget_ref"] == account) for data in (old, upgraded)]
                assert change == applied[1] - applied[0], (name, account, resource, change, applied)
        restored[name] = {"run_id": case["before"]["run"]["run_id"], "original": old, "upgraded": upgraded,
                          "additional_jobs": case["additional_jobs"], "additional_turns": case["additional_native_turns"],
                          "additional_token_charges": case["additional_token_charges"], "twice_equal": True,
                          "original_manifest_and_state_rollback_matches": True}
    probe.save(receipt, {"status": "PASS", "v61_revision": result["baseline"], "b_revision": revision,
                         "v6_revision": result["prebaseline"], "old_contract": result["old_contract"],
                         "new_contract": result["new_contract"], "cases": restored, "paid_model_calls": 0,
                         "provider_tokens": None, "original_probe_sha256": probe.digest(retained)})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="phase", required=True)
    for name in ("seed-a", "check-a", "check-v61", "read"):
        command = sub.add_parser(name)
        command.add_argument("--work", type=Path, required=True)
        if name in {"seed-a", "read"}:
            command.add_argument("--release", type=Path, required=True)
        if name != "read":
            command.add_argument("--revision", required=True)
        if name in {"seed-a", "check-v61"}:
            command.add_argument("--binary", type=Path, required=True)
        if name in {"check-a", "check-v61"}:
            command.add_argument("--receipt", type=Path, required=True)
        if name == "check-v61":
            command.add_argument("--baseline", type=Path, required=True)
    args = parser.parse_args()
    work = args.work.resolve()
    if work.is_relative_to(ROOT):
        parser.error("use a separate validation directory for releases, state and receipts")
    if args.phase == "read":
        print(json.dumps(read_manifests(args.release.resolve(), work)))
    elif args.phase == "seed-a":
        seed_a(args.release.resolve(), args.revision, args.binary.resolve(), work)
    elif args.phase == "check-a":
        check_a(ROOT, args.revision, work, args.receipt.resolve())
    else:
        check_v61(ROOT, args.revision, args.baseline.resolve(), args.binary.resolve(), work, args.receipt.resolve())


if __name__ == "__main__":
    main()
