"""Cold-open an actual V6.2.2 delegated study, replay the published receipt and restore its original state."""
import argparse
import json
from pathlib import Path
import sys

BASE = "e6520f53ca19ef8eae3ec0915af74e9bfc6dcdca"
POLICY = "gx.evidence/6.2.2"


def check(ok, reason):
    if not ok:
        raise ValueError(reason)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase", choices=("capture","check","rollback"))
    ap.add_argument("--install", type=Path, required=True)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--work", type=Path, required=True)
    ap.add_argument("--candidate")
    args = ap.parse_args()
    install, project, work = (p.resolve() for p in (args.install, args.project, args.work))
    sys.path[:0] = [str(install), str(install / "gx-h"), str(install / "acceptance/v62a/upgrade")]
    from acceptance.v621.compat import replay
    from acceptance.v62a.upgrade import probe
    phase = args.phase
    head = replay.git(install, "rev-parse", "HEAD")
    check(head == (args.candidate if phase == "check" else BASE), "Wrong checkout for this phase")
    check(not replay.git(install, "status", "--porcelain"), "Cold-open requires an unchanged checkout")
    dest = work / (phase + ".json")
    if dest.exists():
        raise FileExistsError(dest)
    state = project / "state"
    def policy():
        with replay.read_db(state) as db:
            return db.execute("SELECT json_extract(inputs_json,'$.inputs.evidence_policy') FROM runs").fetchone()[0]
    summary = probe.load(project / "summary.json")
    rid = summary["run_id"]
    if phase == "capture":
        check(summary["status"] == "CLOSED" and summary["paid_model_calls"] == 0 and policy() == POLICY,
              "Expected a completed offline V6.2.2 study")
        facts = replay.records(project)
        check(facts["report"] and len(facts["jobs"]) >= 2 and facts["claims"], "Missing original public study")
        probe.backup(state, work / "original-state")
        out = {"business": facts,"scientific_sha256":replay.json_sha(facts), "schema":probe.snapshot(state)["schema"],
               "packages":replay.bindings(install, project, facts["package_pins"])}
    elif phase == "check":
        before = probe.load(work / "capture.json")
        facts = before["business"]
        check(before["source_commit"] == BASE and before["run_id"] == rid and policy() == POLICY, "Original state differs")
        digests = []
        for _ in range(2):
            result = probe.cli(install, project, state, "resume", "--run-id", rid, "--timeout", "5")
            check(result["goal_status"] == "CLOSED" and policy() == POLICY, "Old policy or lifecycle changed")
            now = replay.records(project)
            check(now == facts, "Cold reopen changed original work")
            digests.append(replay.json_sha(now))
        sys.path.insert(0,str(install / "acceptance/v62b/engineering"))
        from run import replay_report
        receipt = replay_report(project, rid, probe.load(project/"raw/wire.json"))
        check(receipt["redeliveries"] == 2 and receipt["original_receipt_sha256"] == facts["original_receipt_sha256"]
              and replay.records(project) == facts, "Same callback changed stored report or business records")
        packages = replay.bindings(install, project, facts["package_pins"])
        check(packages == before["packages"], "Historical plugin bindings changed")
        out = {"baseline":BASE,"cold_reopens":digests,"scientific_sha256":before["scientific_sha256"],
               "report_replay":receipt,"packages":packages,"schema":probe.snapshot(state)["schema"],
               "original_jobs":len(facts["jobs"]),"original_turns":len(facts["native_turns"]),
               "additional_jobs":0,"additional_turns":0,"additional_charges":0}
    else:
        before = probe.load(work/"capture.json")
        qualified = probe.load(work/"check.json")
        check(args.candidate == qualified["source_commit"], "Unexpected candidate to roll back")
        migrated = work / "migrated-state"
        check(not migrated.exists(),"A migrated state has already been preserved")
        state.rename(migrated)
        probe.backup(work/"original-state",state)
        result = probe.cli(install,project,state,"resume","--run-id",rid,"--timeout","5")
        facts = replay.records(project)
        check(result["goal_status"] == "CLOSED" and policy() == POLICY and facts == before["business"]
              and probe.snapshot(state)["schema"] == before["schema"], "Rollback failed to restore the original study")
        out = {"candidate":args.candidate,"scientific_sha256":replay.json_sha(facts),"schema":before["schema"]}
    probe.save(dest,dict(out,source_commit=head, status="PASS", policy=policy(), run_id=rid, external_paid_calls=0))
    print(head,rid,phase,"PASS")


if __name__ == "__main__":
    main()
