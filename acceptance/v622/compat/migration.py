"""Verify a real released public delegated Run in the same on-disk GX deployment across an upgrade."""
import argparse
from pathlib import Path
import json
import sys


BASE = "e5b7ee977ec33854362bc2440d8f9249464aed20"
PREVIOUS = "gx.evidence/6.2.1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("capture", "check", "rollback"))
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--candidate", help="Exact committed source ref for the check phase")
    args = parser.parse_args()
    work = args.work.resolve()
    deploy = work / "install"
    # The release and its marker identify the exact same physical path in all three processes.
    sys.path[:0] = [str(deploy), str(deploy / "gx-h"), str(deploy / "acceptance/v62a/upgrade")]
    from acceptance.v621.compat import replay
    from acceptance.v62a.upgrade import probe

    project = work / "old-run/02"
    state = project / "state"
    checkout = replay.git(deploy, "rev-parse", "HEAD")
    expected = args.candidate if args.phase == "check" else BASE
    if checkout != expected or replay.git(deploy, "status", "--porcelain"):
        raise ValueError("This step requires the requested clean checkout at the existing deployment path")
    target = work / (args.phase + ".json")
    if target.exists():
        raise FileExistsError(target)
    def read_policy():
        with replay.read_db(state) as conn:
            return json.loads(conn.execute("SELECT inputs_json FROM runs").fetchone()[0])["inputs"]["evidence_policy"]
    if args.phase == "capture":
        summary = probe.load(project / "summary.json")
        score = probe.load(work / "old-run/scores.json")
        old = replay.records(project)
        assert read_policy() == PREVIOUS and len(old["jobs"]) == 2 and len(old["claims"]) == 2
        assert summary["status"] == "PASS" and score["scripted_science_passes"] == 1
        assert summary["paid_model_calls"] == 0 and score["scripted_delegation_demonstrated"] == 1
        original_sources = {p: probe.digest(deploy / "acceptance/v62c" / p) for p in (
                     "protocol.json", "rate.py", "scoring.py", "witness.py", "receipts/scores.json")}
        probe.backup(state, work / "original-state")
        record = {"source_commit": BASE,"run_id":summary["run_id"],"evidence_policy":PREVIOUS,
                  "original_packages":replay.bindings(deploy, project, old["package_pins"]),
                  "scientific_state_sha256":replay.json_sha(old), "business":old,"previous_schema":probe.snapshot(state)["schema"],
                  "old_source_hashes":original_sources,"original_score_sha256":probe.digest(work / "old-run/scores.json")}
    elif args.phase == "check":
        seed = probe.load(work / "capture.json")
        old = seed["business"]
        assert all(probe.digest(deploy / "acceptance/v62c" / p) == v for p,v in seed["old_source_hashes"].items())
        assert old == replay.records(project) and read_policy() == PREVIOUS
        reopened = []
        for i in range(2):
            status = probe.cli(deploy, project, state, "resume", "--run-id", seed["run_id"], "--timeout", "5")
            assert status["goal_status"] == "CLOSED" and replay.records(project) == old
            reopened.append({"cold_process": i + 1, "business_sha256":replay.json_sha(replay.records(project))})
        sys.path.insert(0, str(deploy / "acceptance/v62b/engineering"))
        from run import replay_report
        repeated = replay_report(project, seed["run_id"], probe.load(project / "raw/wire.json"))
        assert repeated["redeliveries"] == 2 and repeated["original_receipt_sha256"] == old["original_receipt_sha256"]
        assert read_policy() == PREVIOUS and replay.records(project) == old
        packages = replay.bindings(deploy, project, old["package_pins"])
        assert packages == seed["original_packages"]
        current_schema = probe.snapshot(state)["schema"]
        contracts = probe.load(deploy / "gx-h/contracts/CONTRACT_SHA")
        assert current_schema == {"contract_sha256":contracts["__combined__"], "db_schema_sha256":contracts["db_schema.sql"]}
        assert current_schema != seed["previous_schema"]
        record = {"source_commit":checkout,"previous_commit":BASE,"run_id":seed["run_id"],
                  "old_science_policy":read_policy(),"cold_reopens":reopened,"published_report_replay":repeated,
                  "scientific_state_sha256":seed["scientific_state_sha256"],"previous_schema":seed["previous_schema"],
                  "current_schema":current_schema, "old_package_pins":packages,
                  "additional_jobs":0,"additional_model_turns":0,"additional_charges":0}
    else:
        check = probe.load(work / "check.json")
        seed = probe.load(work / "capture.json")
        if (work / "candidate-state").exists(): raise FileExistsError(work / "candidate-state")
        state.rename(work / "candidate-state")
        probe.backup(work / "original-state", state)
        result = probe.cli(deploy, project, state, "resume", "--run-id",seed["run_id"],"--timeout","5")
        assert result["goal_status"] == "CLOSED" and replay.records(project) == seed["business"]
        restored_schema = probe.snapshot(state)["schema"]
        assert restored_schema == seed["previous_schema"] and read_policy() == PREVIOUS
        record = {"source_commit":BASE,"candidate":check["source_commit"],"run_id":seed["run_id"],
                  "restored_schema":restored_schema,"restored_policy":read_policy(),
                  "restored_scientific_state_sha256":replay.json_sha(replay.records(project))}
    probe.save(target, {**record,"status":"PASS","paid_provider_calls":0})
    print(args.phase, checkout, record["run_id"], "PASS")


if __name__ == "__main__":
    main()
