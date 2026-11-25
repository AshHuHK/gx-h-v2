"""Check the A1–A7 rerun receipts against the fixed B source and the resulting SQLite projects."""

import argparse
import hashlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile

from upgrade import ROOT, db_read, digest


def load(path):
    return json.loads(path.read_bytes())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def revision_sources(revision):
    parts = ("gx-h/gx_core", "gx-h/plugins", "gx-h/contracts", "gx-h/config")
    data = subprocess.check_output(["git", "-C", ROOT, "archive", revision, *parts, "acceptance/v62a"])
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:") as archive:
        files = {item.name: hashlib.sha256(archive.extractfile(item).read()).hexdigest() for item in archive
                 if item.isfile() or item.issym()}
    runtime = {p: sha for p, sha in files.items() if p.startswith("gx-h/")}
    runners = {Path(p).name: h for p, h in files.items() if p.startswith("acceptance/v62a/interleavings") and p.endswith(".py")}
    return {"commit": revision, "runtime_file_count": len(runtime), "runtime_sources_sha256": digest(runtime),
            "acceptance_sources_sha256": runners}


def jobs_and_reports(state):
    with db_read(state) as conn:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok" and not conn.execute("PRAGMA foreign_key_check").fetchall()
        jobs = [dict(r) for r in conn.execute("SELECT j.job_id,j.status,j.runner_pid,b.budget_ref,b.status AS settlement "
                                               "FROM jobs j JOIN budget_reservations b USING(reservation_ref) ORDER BY job_id")]
        assert len(jobs) == conn.execute("SELECT count(*) FROM jobs").fetchone()[0]
        reports = []
        for record in conn.execute("SELECT r.report_ref,r.module_instance_id,a.namespace,a.rel_path,a.sha256 FROM v62_module_reports r "
                                   "JOIN artifacts a ON a.artifact_ref=r.report_ref ORDER BY r.report_ref"):
            assert sha(state / "artifacts" / record["namespace"] / record["rel_path"]) == record["sha256"]
            reports.append(record["report_ref"])
    for row in jobs:
        assert row["status"] == "SUCCEEDED" and row["settlement"] == "SETTLED"
        outcome = load(state / "jobs" / row["job_id"] / "outcome.json")
        assert outcome["started"] and outcome["exit_code"] == 0 and outcome["tool_pid"] > 0
    return {"jobs": jobs, "reports": reports}


def released_verifier(work, revision, paths, verifier):
    # The released A verifier consumes this original public directory layout.
    a = ROOT / "acceptance/v62a"
    staged = Path(tempfile.mkdtemp(prefix="a-protocol-", dir=work))
    for path in [a / "official_inventory.py", a / "module_peer.py", *a.glob("interleavings*.py")]:
        shutil.copyfile(path, staged / path.name)
    a1 = staged / "receipts/A1"
    a1.mkdir(parents=True)
    shutil.copyfile(paths["A1"], a1 / "result.json")
    shutil.copyfile(work / "a1-network.json", a1 / "network.json")
    for name, path in paths.items():
        if name != "A1":
            shutil.copyfile(path, staged / "receipts" / (name + ".json"))
    (staged / "upgrade").mkdir()
    shutil.copyfile(work / "v61-upgrade-original-probe.json", staged / "upgrade/result.json")
    cmd = [sys.executable, "-B", str(verifier), "--source", revision, "--root", str(ROOT), "--directory", str(staged)]
    result = json.loads(subprocess.check_output([*cmd, "--write"], text=True))
    assert result == json.loads(subprocess.check_output(cmd, text=True)) and result["status"] == "PASS"
    return {**result, "verifier_sha256": sha(verifier), "index_sha256": sha(staged / "receipts/index.json")}


def audit(work, revision, verifier):
    paths = {"A1": work / "a1/result.json"}
    paths.update({f"A{i}": work / "interleavings" / f"A{i}.json" for i in range(2, 7)})
    paths["A7"] = work / "isolation/A7.json"
    cases = {name: load(path) for name, path in paths.items()}
    sources = revision_sources(revision)
    for name, row in cases.items():
        assert row["case"] == name and row["status"] == "PASS" and row["paid_model_calls"] == 0
        if name == "A1":
            assert row["source_head"] == revision
            for filename in ("official_inventory.py", "module_peer.py"):
                original = subprocess.check_output(["git", "-C", ROOT, "show", f"{revision}:acceptance/v62a/{filename}"])
                assert row["script_sha256"][filename] == hashlib.sha256(original).hexdigest()
        else:
            assert row["observed_sources"] == sources, (name, row["observed_sources"], sources)
    local = [work / "a1-network.json", work / "interleavings-network.json", work / "isolation/A7-state/quick-network.json"]
    for path in local:
        net = load(path)
        assert (net["exit_code"] == 0 and net["ipv4_routes"] == [] and net["ipv6_route_interfaces"] == ["lo"]
                and net["original_proxy_environment"] and all(net["isolated_proxy_listeners"].values()))
    summary = {}
    for arm in ("quick", "module"):
        source = cases["A1"]["arms"][arm]
        for label, expected in source["raw_files_sha256"].items():
            assert sha(work / "a1" / label) == expected, label
        facts = jobs_and_reports(work / "a1" / arm / "state")
        assert {r["job_id"] for r in facts["jobs"]} == {r["job_id"] for r in source["jobs"]} and len(facts["jobs"]) == 2
        assert facts["reports"] == ([source["report"]["artifact_ref"]] if arm == "module" else [])
        summary.setdefault("A1", {})[arm] = {**facts, "run_id": source["run_id"], "cold": source["cold_resume"]}
    root = work / "interleavings"
    for name, schedule in cases["A2"]["schedules"].items():
        facts = jobs_and_reports(root / "A2-state" / name / "state")
        if name.startswith("lead_"):
            assert facts["reports"] == [schedule["report_ref"]]
        elif name == "cancel_after_read":
            assert facts == {"jobs": [], "reports": []}
        elif name == "monitored_counterevidence":
            early, after = schedule["first_wake"], schedule["round_wake"]
            assert set(facts["reports"]) == set(early["report_refs"] + after["report_refs"])
            assert early["reason"] == "counterevidence" and set(early["report_refs"]) <= set(schedule["report_refs"])
        summary.setdefault("A2", {})[name] = facts
    for case, job_count in (("A3", 2), ("A4", 1), ("A5", 1)):
        row = cases[case]
        facts = jobs_and_reports(root / (case + "-state") / "state")
        assert len(facts["jobs"]) == job_count and {r["report_ref"] for r in row["reports"]} == set(facts["reports"])
        summary[case] = facts
    a3, a4, a5 = map(cases.__getitem__, ("A3", "A4", "A5"))
    assert a3["cas_conflict"]["conflict"]["code"] == "STALE_REVISION" and len(a3["round_wakes"]) == 1
    assert {c["budget_ref"] for c in a3["charges"]} == {j["budget_ref"] for j in summary["A3"]["jobs"]}
    assert a4["out_of_scope_refusal"]["code"] == "SCOPE_DENIED" and a4["successor"]["lease_id"] != a4["old_actor"]["lease_id"]
    completed = a5["verified_reuse"]
    assert a5["crash_exit"] == 83 and a5["producer_pid"] != a5["recovery_pid"] and len(a5["confirmed_tool_executions"]) == 1
    with db_read(root / "A5-state/state") as conn:
        actual = conn.execute("SELECT n.job_id,n.result_ref,a.reused_from FROM v6_nodes n JOIN v6_job_attempts a USING(node_id) "
                              "WHERE n.node_id=?", (completed["recovered_node"],)).fetchone()
        assert tuple(actual) == (completed["original_job"], completed["verified_output"], completed["source_node"])
    uncertain = a5["uncertain_admission"]
    actual = jobs_and_reports(root / "A5-state/uncertain-admission/state")
    assert uncertain["crash_exit"] == 84 and uncertain["additional_launches"] == 0 and len(actual["jobs"]) == 1
    assert actual["jobs"][0]["runner_pid"] == uncertain["accepted_real_launcher"]["runner_pid"]
    summary["A5"].update(reused=completed, uncertain=actual, crash_exits=[83, 84], additional_launches=0)
    a6, a7 = cases["A6"], cases["A7"]
    summary["A6"] = {"remaining_module_tokens": a6["exhausted"]["remaining_module_model_tokens"],
                     "report_ref": a6["large_report"]["full_report_ref"], "conditional_jobs": len(a6["conditional_dispatch"]["jobs"])}
    assert summary["A6"]["remaining_module_tokens"] == 0 and summary["A6"]["conditional_jobs"] == 2
    summary["A7"] = {"old_quick_run": a7["quick"]["run_id"], "cold_extra_jobs": a7["quick"]["cold_extra_jobs"],
                     "cold_extra_native_calls": a7["quick"]["cold_extra_native_calls"], "scope_refusals": sorted(a7["refusals"])}
    assert summary["A7"]["cold_extra_jobs"] == summary["A7"]["cold_extra_native_calls"] == 0
    return {"status": "PASS", "b_revision": revision, "source": sources, "cases": summary,
            "original_a_verifier": released_verifier(work, revision, paths, verifier),
            "receipt_sha256": {p.relative_to(work).as_posix(): sha(p) for p in [*paths.values(), *local]},
            "paid_model_calls": 0, "provider_tokens": None}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--a-verifier", type=Path, default=ROOT / "acceptance/v62a/verify.py")
    args = parser.parse_args()
    result = audit(args.work.resolve(), args.revision, args.a_verifier.resolve())
    args.receipt.write_text(json.dumps(result, indent=2) + "\n")
