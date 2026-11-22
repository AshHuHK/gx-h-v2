"""Read-only offline review-contract replay on a retained historical GX project."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "gx-h"))
from gx_core.artifacts import ArtifactService
from gx_core.errors import GxError
from gx_core.orchestration import service


def replay(state):
    path = state.resolve() / "gx.db"
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, isolation_level=None)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        registry = service.default_registry()
        workflows = {v: registry.get("tl1a_geometry", v) for v in ("1", "2")}
        assets = ArtifactService(conn)
        records = []
        for row in conn.execute("SELECT t.task_id,t.run_id,s.lease_id,s.submission_id,s.attempt,s.result,"
                    "s.artifact_refs_json,s.validation_json,r.brief FROM submissions s JOIN tasks t USING(task_id) "
                    "JOIN p0_roots r ON r.lease_id=s.lease_id WHERE t.stage='assessment' ORDER BY s.rowid"):
            run = conn.execute("SELECT workflow_id,workflow_version,workflow_sha256,status FROM runs WHERE run_id=?",
                               (row["run_id"],)).fetchone()
            if run["workflow_id"] != "tl1a_geometry":
                continue
            inputs = json.loads(row["brief"].split("\n", 1)[1])["input"]
            refs = json.loads(row["artifact_refs_json"])
            task = {"input": inputs, "task_id": row["task_id"], "run_id": row["run_id"],
                    "lease_id": row["lease_id"], "submission_artifact_refs": refs}
            product = assets.read_json(refs[0])
            validations = {}
            for version, workflow in workflows.items():
                try:
                    workflow.task_product_validator(conn, task, None)
                    validations[version] = {"accepted": True, "workflow_sha256": workflow.digest()}
                except GxError as exc:
                    validations[version] = {"accepted": False, "code": exc.code, "reason": exc.message,
                                            "workflow_sha256": workflow.digest()}
            records.append({"run_id": row["run_id"], "original_run": dict(run), "attempt": row["attempt"],
                            "task_id": row["task_id"], "lease_id": row["lease_id"], "submission_id": row["submission_id"],
                            "original_receipt": row["result"], "original_validation": json.loads(row["validation_json"]),
                            "artifact_ref": refs[0], "artifact_sha256": assets.describe(refs[0])["sha256"],
                            "limitations_shape": type(product["limitations"]).__name__,
                            "original_findings_keys": list(product["findings"]), "validation_replay": validations})
        return {"kind": "offline_validation_replay", "store_query_only": True, "database_changes": conn.total_changes,
                "model_calls": 0, "new_jobs": 0, "reviews": records}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    report = replay(parser.parse_args().state)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["reviews"] or not all(r["validation_replay"]["2"]["accepted"] for r in report["reviews"]):
        raise SystemExit(1)
