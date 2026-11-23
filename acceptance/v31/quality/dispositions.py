"""Describe actual publication, refusal and follow-up measurement independently of project closure."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from grade import digest


def read_dispositions(state):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")

        def read(ref):
            row = db.execute("SELECT kind,namespace,rel_path,sha256 FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone()
            raw = (state / "artifacts" / row["namespace"] / row["rel_path"]).read_bytes()
            if digest(raw) != row["sha256"]:
                raise ValueError("Artifact bytes differ from recorded version: " + ref)
            return json.loads(raw)

        record = {"run": dict(db.execute("SELECT run_id,status,workflow_sha256 FROM runs").fetchone()),
                  "planner_submissions": [], "measurements": [], "accepted_assessments": [],
                  "native_settlements": [dict(r) for r in db.execute(
                      "SELECT thread_id,turn_id,budget_status,confidence,settled_tokens FROM p0_meter_native_turns ORDER BY sequence")]}
        for row in db.execute(
            "SELECT s.submission_id,s.result,s.attempt,s.artifact_refs_json,s.validation_json,t.stage,t.input_json, "
            "l.principal FROM submissions s JOIN tasks t USING(task_id) JOIN leases l USING(lease_id) ORDER BY s.rowid"
        ):
            task = json.loads(row["input_json"])
            ref = json.loads(row["artifact_refs_json"])[0]
            product = read(ref)
            basic = {"round": task["round"], "attempt": row["attempt"], "submission_id": row["submission_id"],
                     "result": row["result"], "principal": row["principal"], "product_ref": ref}
            if row["stage"] == "assessment":
                if row["result"] == "ACCEPTED":
                    record["accepted_assessments"].append({**basic, "findings": product["findings"]})
                continue
            allowed = set(task["evidence_refs"])
            for evidence in task["evidence_refs"]:
                for r in read(evidence)["routes"]:
                    allowed.update(r[k] for k in ("input_ref","observations_ref","evaluation_ref") if r.get(k))
            record["planner_submissions"].append({
                **basic, "assignment": {"assessment_ref": task["assessment_ref"], "allowed_source_evidence_refs": sorted(allowed)},
                "reason": product["reason"], "grids_as_submitted": product.get("next_grids"),
                "source_citations": [{**j, "outside_source_evidence_scope": sorted(set(j["evidence_refs"]) - allowed)}
                                     for j in product.get("source_decisions", [])],
                "actual_refusal": [v["message"] for v in json.loads(row["validation_json"]).get("records", []) if not v["ok"]]})
        for row in db.execute("SELECT artifact_ref FROM artifacts WHERE kind='research_evidence' ORDER BY rowid"):
            bundle = read(row["artifact_ref"])
            for route in bundle["routes"]:
                if route.get("outcome_kind") != "valid_scientific_outcome":
                    continue
                original = read(route["observations_ref"])
                grid = read(route["input_ref"])
                proposal = read(bundle["proposal_ref"])
                job = dict(db.execute("SELECT status,submitted_at,finished_at FROM jobs WHERE job_id=?", (route["job_id"],)).fetchone())
                pts = original["observations"]
                record["measurements"].append({"round": proposal["round"], "path_id": route["path_id"], "job": job,
                    "job_id": route["job_id"], "observations_ref": route["observations_ref"], "points": pts,
                    "prospective_decision_ref": proposal["previous_decision_ref"],
                    "matches_selected_coords": grid["candidates"] == [{"x": p["x"], "candidate_id": p["candidate_id"]} for p in pts],
                    "recomputed_scores_match": all(p["score"] == -(p["x"] - 2)**2 for p in pts)})
        record["final_report_present"] = bool(db.execute("SELECT 1 FROM artifacts WHERE kind='research_report'").fetchone())
        return record


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run", type=Path)
    folder = p.parse_args().run
    state = Path(json.loads((folder / "freeze.json").read_bytes())["private_state"])
    result = read_dispositions(state)
    (folder / "dispositions.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
