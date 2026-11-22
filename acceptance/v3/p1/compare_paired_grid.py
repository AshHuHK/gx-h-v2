"""Verify a repeated paired-grid acceptance against the preceding staged candidate."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return sha(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def journal(directory):
    state = directory / "state"
    with closing(sqlite3.connect((state / "gx.db").resolve().as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        contents = {}
        inventory = Counter()
        for item in db.execute("SELECT artifact_ref,kind,namespace,rel_path,sha256,bytes FROM artifacts"):
            raw = (state / "artifacts" / item["namespace"] / item["rel_path"]).read_bytes()
            assert len(raw) == item["bytes"] and sha(raw) == item["sha256"], item["artifact_ref"]
            contents[item["artifact_ref"]] = raw
            inventory[item["kind"]] += 1
        settled = db.execute(
            "SELECT s.round_index,s.path_id,s.direction,s.kind,j.job_id,j.job_attempt,j.scheduler_kind,j.status,"
            "j.exit_code,j.capability_id,j.capability_version,e.artifact_refs_json,e.frozen_sha "
            "FROM science_settlements s JOIN jobs j USING(job_id) JOIN evaluations e USING(eval_id) "
            "ORDER BY s.round_index,s.path_id")
        outputs, receipt_ids = [], []
        for row in settled:
            observed_ref, eval_ref = json.loads(row["artifact_refs_json"])
            measured = json.loads(contents[observed_ref])
            evaluation = json.loads(contents[eval_ref])
            assert evaluation["input_ref"] == observed_ref and evaluation["input_sha256"] == sha(contents[observed_ref])
            receipt_ids.append({"job_id": row["job_id"], "operation_id": measured["operation_id"],
                                "observations_ref": observed_ref, "observations_sha256": sha(contents[observed_ref]),
                                "evaluation_ref": eval_ref, "evaluation_sha256": sha(contents[eval_ref])})
            outputs.append({"round": row["round_index"], "path": row["path_id"], "direction": row["direction"],
                            "kind": row["kind"], "job": {k: row[k] for k in (
                                "job_attempt", "scheduler_kind", "status", "exit_code", "capability_id", "capability_version")},
                            "frozen_sha": row["frozen_sha"],
                            "measured": {k: v for k, v in measured.items() if k != "operation_id"},
                            "evaluation": {k: v for k, v in evaluation.items()
                                           if k not in ("attempt_id", "input_ref", "input_sha256")}})
        assert len(outputs) == len({r["job_id"] for r in receipt_ids}) == 4
        assert all(r["job"]["exit_code"] == 0 and r["job"]["status"] == "SUCCEEDED" for r in outputs)
        return {"artifact_kinds": dict(inventory), "routes": outputs}, receipt_ids


def load(path):
    proof = json.loads(path.read_bytes())
    assert proof["result"] == "PASS" and proof["exit_code"] == 0
    work = Path(proof["command"][proof["command"].index("--work") + 1])
    return proof, work


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    old, baseline = load(args.before)
    new, candidate = load(args.after)
    args.out.mkdir(parents=True, exist_ok=False)
    report = {"candidate": new["candidate_install"], "comparator": old["candidate_install"],
              "command": [sys.executable, "-B", *sys.argv], "probe_sha256": sha(Path(__file__).read_bytes()),
              "staged_command": [".venv/bin/python", *new["command"][1:]], "case_receipts": {},
              "candidate_summary": {"file": str(args.after), "sha256": sha(args.after.read_bytes())},
              "previous_summary": {"file": str(args.before), "sha256": sha(args.before.read_bytes())}}
    keys = {"reference-v21", "cold-v21", "legacy", "composed"}
    assert set(old["cases"]) == set(new["cases"]) == keys
    for name in sorted(keys):
        first, second = old["cases"][name], new["cases"][name]
        assert first["evidence"] == second["evidence"]
        assert first["scientific_result_sha256"] == second["scientific_result_sha256"] == canonical(second["evidence"])
        assert first["terminal_replay_new_calls"] == second["terminal_replay_new_calls"] == 0
        assert first["ledger"] == second["ledger"] and first["artifact_count"] == second["artifact_count"] == 58
        before, before_ids = journal(baseline / name)
        after, after_ids = journal(candidate / name)
        assert before == after, name
        raw = (candidate / name / "requests.jsonl").read_bytes()
        trace = args.out / (name + ".jsonl")
        assert sha(raw) == second["requests_sha256"]
        trace.write_bytes(raw)
        report["case_receipts"][name] = {
            "responses_trace": {"file": str(trace), "sha256": sha(raw), "responses": len(raw.splitlines())},
            "score_and_decision_sha256": second["scientific_result_sha256"],
            "job_artifact_projection_sha256": canonical(after), "exact_job_outputs_and_scores_equal": True,
            "artifact_kind_counts": after["artifact_kinds"], "routes": after["routes"],
            "previous_run_receipts": before_ids, "corrected_run_receipts": after_ids,
            "terminal_replay_extra_responses": second["terminal_replay_new_calls"]}
    report["result"] = "PASS"
    (args.out / "index.json").write_text(json.dumps(report, sort_keys=True, indent=2) + "\n")
