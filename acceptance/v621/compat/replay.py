"""Capture a released V6.2 research project, cold-open it twice on a candidate, and check rollback.

A release checkout stays at the same physical path. The caller switches that private checkout
between phases; this program only reads the named deployment and writes its own external trial.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "acceptance/v62a/upgrade"))
import probe

BASE = "321b3b9542e986e749b4eab08495b288d09ba4be"
B_RULE = "gx.research.v6.2b.1"
PROTOCOL = ("acceptance/v62b/study/protocol.json", "acceptance/v62b/study/not-run.json")
PUBLICATION = ("acceptance/v62b/receipts/engineering/summary.json",
               "acceptance/v62b/receipts/engineering/public-check.json",
               "acceptance/v62b/receipts/engineering/public-evidence.tar.gz")


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def git(deploy, *args):
    return subprocess.check_output(["git", "-C", str(deploy), *args], text=True).strip()


def json_sha(value):
    from hashlib import sha256
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def read_db(state):
    conn = sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return closing(conn)


def bindings(deploy, project, packages):
    expected = {f"{p['id']}@{p['version']}": p["sha256"] for p in packages}
    verified = probe.cli(deploy, project, None, "plugin", "verify", *expected)
    found = {f"{p['id']}@{p['version']}": p["sha256"] for p in verified["plugins"]}
    require(found == expected and not verified["failures"], "Historical plugin lost its installed content pin")
    return found


def records(project):
    state = project / "state"
    old = probe.snapshot(state)  # verifies original artifact bytes, SQLite integrity and foreign keys
    with read_db(state) as db:
        def query(sql):
            return [dict(row) for row in db.execute(sql)]
        run_inputs = json.loads(db.execute("SELECT inputs_json FROM runs").fetchone()[0])
        report, = query("SELECT operation_key,report_ref,receipt_json FROM v62_module_reports")
        receipt = json.loads(report["receipt_json"])
        body = query("SELECT namespace,rel_path,sha256 FROM artifacts WHERE artifact_ref=" +
                     "(SELECT report_ref FROM v62_module_reports)")[0]
        require(probe.digest(state / "artifacts" / body["namespace"] / body["rel_path"]) ==
                body["sha256"] == receipt["report_sha256"], "Stored module report bytes changed")
        claims = query("SELECT revision,sha256,document_json FROM hypothesis_revisions ORDER BY rowid")
        require(all(json_sha(json.loads(c["document_json"])) == c["sha256"] for c in claims),
                "Stored claim revision differs from its content digest")
        manifests = query("SELECT job_id,admission_sha256,completion_sha256,admission_json,completion_json "
                          "FROM p0_execution_manifests ORDER BY job_id")
        for m in manifests:
            for phase in ("admission", "completion"):
                value = json.loads(m.pop(phase + "_json"))
                require(json_sha(value) == m[phase + "_sha256"], "Stored Job manifest digest mismatch")
        history = {}
        for table in ("v6_decisions", "v6_nodes", "v62_module_wakes", "research_contexts", "p0_root_calls"):
            rows = query(f"SELECT * FROM {table} ORDER BY rowid")
            history[table] = {"count": len(rows), "sha256": json_sha(rows)}
        models = query("SELECT thread_id,effective_profile_json FROM p0_roots ORDER BY thread_id")
        bindings_used = [{"thread_id": m["thread_id"], "model": p["model"], "effort": p["effort"],
                          "provider": p["provider"]} for m in models for p in [json.loads(m["effective_profile_json"])]]
    actual_claims = []
    for c in claims:
        doc = json.loads(c["document_json"])
        actual_claims.append({**{k: c[k] for k in ("revision", "sha256")},
                              **{k: doc[k] for k in ("claim_revision_id", "statement", "level",
                                                     "evidence_refs", "maturity")}})
    return {"run": old["run"], "run_input_sha256": json_sha(run_inputs),
            "research_evidence_version": run_inputs["inputs"]["research_evidence_version"],
            "package_pins": run_inputs["plugin_binding"]["packages"], "native_models": bindings_used,
            "report": {k: receipt[k] for k in ("module_instance_id", "operation_key", "report_ref", "report_sha256",
                                                "event_id", "wake_event_id", "source_refs")},
            "original_receipt_sha256": json_sha(receipt), "claims": actual_claims,
            "graph": old["graph"], "jobs": old["jobs"], "execution_manifests": manifests,
            "artifact_pins": old["outputs"], "native_turns": old["turns"],
            "budgets": old["budgets"], "history": history, "state_marker_sha256": old["marker_sha256"]}


def cursor(project):
    with read_db(project / "state") as db:
        row = db.execute("SELECT event_cursor FROM v62_maturity_cursors").fetchone()
        return row[0] if row else 0


def observe(deploy, project, index):
    reading = project / f"sensor-{index}.json"
    acq = project / "operator-supplied.json"
    probe.save(reading, {"schema": "gx.public-sensor/1", "value": 40 + index})
    probe.save(acq, {"data": "public-migration-series", "acquisition": "sensor-a", "sample": "public-control",
                     "sampling": None, "publication": [], "source": "public-sensor", "processing": [],
                     "applicability": "public operator-interface migration check"})
    return probe.cli(deploy, project, project / "state", "observe", "--source", f"catalog-reading-{index}",
                     "--input", reading, "--acquisition", acq)


def opening(deploy, project):
    project.mkdir()
    config = project / "grant.json"
    probe.save(config, {"project": "historical-sensor-ledger", "capabilities": [], "plugins": [],
                        "research_effort": "deep", "research_modules": []})
    return probe.cli(deploy, project, project / "state", "start", "--goal", "Track the published sensor series.",
                     "--goal-config", config, "--run-key", "compatibility")


def capture(deploy, work):
    require(git(deploy, "rev-parse", "HEAD") == BASE, "Capture needs the released V6.2 checkout")
    require(not (work / "before.json").exists(), "A baseline capture already exists")
    baseline = probe.load(work / "old/result.json")
    require(baseline["status"] == "PASS" and baseline["source"]["head"] == BASE and
            baseline["paid_model_calls"] == 0, "Run the original offline B engineering trial first")
    project = work / "old/change"
    fact = records(project)
    require(len(fact["jobs"]) == 2 and len(fact["claims"]) == 1 and
            fact["research_evidence_version"] == B_RULE, "The original deep study is incomplete")
    pending = work / "pending-check"
    rid = opening(deploy, pending)["run_id"]
    first = observe(deploy, pending, 1)
    probe.cli(deploy, pending, pending / "state", "resume", "--timeout", "1", expected=(0, 75))
    require(cursor(pending) == first["event_id"], "Original cursor did not consume the first observation")
    for name, path in (("original-state", project / "state"), ("original-pending", pending / "state")):
        probe.backup(path, work / name)
    shutil.copytree(project / "codex", work / "original-host")
    seed = {"revision": BASE, "source_kind": "Published V6.2 code and original scripted official host with real CPU Jobs",
            "original_results": fact, "business_sha256": json_sha(fact), "old_schema": probe.snapshot(project / "state")["schema"],
            "old_maturity_cursor": cursor(project), "verified_packages": bindings(deploy, project, fact["package_pins"]),
            "original_scientific_grading": {p: probe.digest(deploy / p) for p in (*PROTOCOL, *PUBLICATION)},
            "pending_run": rid, "first_observation": first, "paid_model_calls": 0, "provider_tokens": None}
    probe.save(work / "before.json", seed)
    return {"status": "PASS", "phase": "capture", "baseline": BASE, "run_id": fact["run"]["run_id"],
            "reports": 1, "jobs": 2, "claim_sha256": fact["claims"][0]["sha256"],
            "old_schema": seed["old_schema"], "original_record_sha256": probe.digest(work / "before.json")}


def check(deploy, work, candidate):
    require(git(deploy, "rev-parse", "HEAD") == candidate and candidate != BASE, "Check needs the frozen candidate")
    require(not (work / "check.json").exists(), "A candidate result already exists in this trial")
    seed = probe.load(work / "before.json")
    project, pending = work / "old/change", work / "pending-check"
    original = seed["original_results"]
    require(records(project) == original, "Saved original state changed before candidate first open")
    require(all(probe.digest(deploy / path) == sha for path, sha in seed["original_scientific_grading"].items()),
            "Previously published scientific results or protocol changed")
    reopen = []
    for i in range(2):
        result = probe.cli(deploy, project, project / "state", "resume", "--timeout", "5")
        facts = records(project)
        require(facts == original and result["goal_status"] == "CLOSED", "Cold open changed historical science or charges")
        reopen.append({"ordinal": i + 1, "business_sha256": json_sha(facts), "maturity_cursor": cursor(project)})
    new_schema = probe.snapshot(project / "state")["schema"]
    require(new_schema["contract_sha256"] != seed["old_schema"]["contract_sha256"], "Store did not migrate")
    require(bindings(deploy, project, original["package_pins"]) == seed["verified_packages"], "Plugin bindings changed")
    fresh = work / "new-goal"
    opened = opening(deploy, fresh)
    # Policy belongs to the saved Run, including old Runs reopened in the same deployment.
    from gx_core.p0 import v621_policy
    with read_db(project / "state") as old_db, read_db(fresh / "state") as new_db:
        require(not v621_policy.enabled(old_db, original["run"]["run_id"]), "Old Run acquired a new evidence policy")
        require(v621_policy.enabled(new_db, opened["run_id"]), "New Goal did not enroll in the repair policy")
    sys.path.insert(0, str(deploy / "acceptance/v62b/engineering"))
    from run import replay_report
    wire = probe.load(project / "raw/wire.json")
    replay = replay_report(project, original["run"]["run_id"], wire)
    require(replay["original_receipt_sha256"] == original["original_receipt_sha256"] and records(project) == original,
            "Old exact report replay changed content or accounting")
    observed = observe(deploy, pending, 2)
    probe.cli(deploy, pending, pending / "state", "resume", "--timeout", "1", expected=(0, 75))
    require(cursor(pending) == observed["event_id"] > seed["first_observation"]["event_id"], "Late cursor failed to advance")
    again = observe(deploy, pending, 2)
    require(again == observed, "Replaying the original receipt created a second observation")
    result = {"status": "PASS", "baseline": BASE, "candidate": candidate, "run_id": original["run"]["run_id"],
              "input_baseline_record_sha256": probe.digest(work / "before.json"), "original_schema": seed["old_schema"],
              "new_schema": new_schema, "unchanged_packages": seed["verified_packages"], "cold_reopens": reopen,
              "retained_report": original["report"], "retained_claims": original["claims"],
              "retained_manifests": original["execution_manifests"], "retained_graph_versions": len(original["graph"]),
              "additional_jobs": 0, "new_native_turns": 0, "additional_charges": 0, "replay": replay,
              "pending_run": seed["pending_run"], "cursor_before": seed["first_observation"]["event_id"],
              "cursor_after": observed["event_id"], "new_run_under_repaired_policy": opened["run_id"],
              "paid_model_calls": 0, "provider_tokens": None}
    probe.save(work / "check.json", result)
    return result


def rollback(deploy, work):
    require(git(deploy, "rev-parse", "HEAD") == BASE, "Restore needs the matching old release checkout")
    require((work / "check.json").is_file() and not (work / "restored.json").exists(), "Complete one candidate check first")
    project, pending = work / "old/change", work / "pending-check"
    for path, backup in ((project / "state", "original-state"), (pending / "state", "original-pending")):
        path.rename(work / ("after-" + backup))
        probe.backup(work / backup, path)
    (project / "codex").rename(work / "after-host")
    shutil.copytree(work / "original-host", project / "codex")
    result = probe.cli(deploy, project, project / "state", "resume", "--timeout", "5")
    seed = probe.load(work / "before.json")
    require(result["goal_status"] == "CLOSED" and records(project) == seed["original_results"] and
            cursor(pending) == seed["first_observation"]["event_id"], "Rollback differs from original state")
    out = {"status": "PASS", "baseline": BASE, "candidate": probe.load(work / "check.json")["candidate"],
           "restored_business_sha256": seed["business_sha256"], "restored_schema": probe.snapshot(project / "state")["schema"],
           "restored_cursor": cursor(pending), "paid_model_calls": 0}
    probe.save(work / "restored.json", out)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("capture", "check", "rollback"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--candidate", help="Exact frozen candidate SHA when checking")
    args = parser.parse_args()
    work = args.work.resolve()
    deploy = work / "release"
    require(not work.is_relative_to(ROOT), "Use an external acceptance trial")
    sys.path.insert(0, str(deploy / "gx-h"))
    if args.phase == "capture":
        receipt = capture(deploy, work)
    elif args.phase == "check":
        receipt = check(deploy, work, args.candidate)
    else:
        receipt = rollback(deploy, work)
    print(json.dumps({"phase": args.phase, "status": receipt["status"], "work": str(work)}))
