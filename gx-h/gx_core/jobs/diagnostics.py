"""Decision-safe failure details reconstructed from the Job owner's frozen records."""
import json

from adapters.scientific import pcna_archive_failure as archive

from ..artifacts import ArtifactService
from ..errors import ArtifactHashMismatch, ArtifactNotFound
from ..p0 import manifest


def at_settlement(conn, job_id, intent, observed, exit_code, stderr):
    if not manifest.acquires_material(conn, job_id):
        return None
    if intent.get("capability_version") == archive.METHOD:
        return archive.describe(intent["parameters"], observed, exit_code, stderr)
    return {"kind": "job_failure", "reason_code": "nonzero_exit" if observed == "nonzero_exit" else "process_failure",
            "summary": "The acquisition Job failed without a verified scientific exclusion."}


def attach_to_node(conn, run_id, node):
    if node["status"] != "FAILED" or not node["job_id"]:
        return
    settled = conn.execute("""SELECT json_extract(payload_json,'$.failure') FROM events WHERE run_id=?
        AND subject_kind='node' AND subject_id=? AND kind='v6.node.settled' AND new_state='FAILED'
        AND json_extract(payload_json,'$.job_id')=? ORDER BY event_id DESC LIMIT 1""",
        (run_id, node["node_id"], node["job_id"])).fetchone()
    detail = json.loads(settled[0]) if settled and settled[0] else preserved(conn, run_id, node["job_id"])
    if detail:
        node["failure"] = detail


def preserved(conn, run_id, job_id):
    """Read one failed archive Job. Historical runs can retain only a pinned stderr."""
    row = conn.execute("""SELECT capability_id,capability_version,json_extract(outcome_json,'$.failure.decision_detail')
        FROM jobs WHERE job_id=? AND run_id=? AND status='FAILED'""", (job_id, run_id)).fetchone()
    if not row:
        return None
    if row[2]:
        return json.loads(row[2])
    if f"{row[0]}@{row[1]}" != archive.METHOD:
        return None
    record = manifest.read_manifest(conn, job_id, missing_ok=True)
    if not record:
        return None
    admission, completion = record["admission"], record["completion"]
    capability = admission["capability"]
    if (admission["run_id"] != run_id or f"{capability['id']}@{capability['version']}" != archive.METHOD
            or not completion or completion["status"] != "FAILED"):
        return None
    failure = completion.get("failure") or {}
    data = b""
    log = next((row for row in completion["logs"] if row["kind"] == "job_stderr"
                and row["output_name"] == "stderr" and row["bytes"] <= archive.MAX_LOG_BYTES), None)
    if log and failure.get("observed") == "nonzero_exit":
        assets = ArtifactService(conn)
        try:
            meta = assets.describe(log["artifact_ref"])
            if (meta["run_id"], meta["job_id"], meta["kind"], meta["origin"]) == (run_id, job_id, "job_stderr", "JOB_OUTPUT"):
                data = assets.read_bytes(log["artifact_ref"], expected_sha256=log["sha256"], max_bytes=archive.MAX_LOG_BYTES)
        except (ArtifactHashMismatch, ArtifactNotFound, OSError):
            pass
    return archive.describe(admission["parameters"], failure.get("observed"), completion.get("exit_code"), data)
