"""Check the archived native studies and write a compact PI-facing index."""
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

COLLECTION = Path(__file__).resolve().parent / "live"
sys.path.insert(0, str(COLLECTION.parents[2] / "gx-h"))
from gx_core.p0.research_page import complete_decision

ADMISSION = {
    "top7": "895ad98", "macrocycle": "895ad98", "bglb": "895ad98", "trehalose": "895ad98",
    "sucrose": "956ab21", "record-coverage": "956ab21", "record-successor": "ebb5ec0",
}


def study(name):
    folder = COLLECTION / name
    receipt = json.loads((folder / "receipt.json").read_bytes())
    result = json.loads((folder / "result.json").read_bytes())
    report = hashlib.sha256((folder / "REPORT.md").read_bytes()).hexdigest()
    decision_text = (folder / "DECISION.md").read_bytes()
    closure = result["closure"]
    closing = result["final_decision"]
    if (report != receipt["report_sha256"] or result["goal_status"] != "CLOSED"
            or receipt["run_id"] != result["run_id"] or closure["decision_ref"] != receipt["decision_ref"]
            or closing["decision_ref"] != closure["decision_ref"]
            or decision_text != complete_decision(result).encode("utf-8")
            or receipt["reexport_added"] != {"jobs": 0, "host_turns": 0}):
        raise ValueError(f"{name}: closed report, decision, or reexport mismatch")
    turns = [turn for turn in receipt["turns"] if turn.get("classification") == "NATIVE_MODEL"]
    if not any((turn["thread_id"], turn["turn_id"]) == (closing["thread_id"], closing["turn_id"])
               for turn in turns):
        raise ValueError(f"{name}: missing official closing turn")
    after_close = [turn for turn in turns if turn.get("start") and turn["start"] > closure["closed_utc"]]
    job_ids = [job["job_id"] for job in result["jobs"]]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError(f"{name}: duplicate Job in archive")
    return {
        "study": name, "run": result["run_id"], "admitted_on": ADMISSION[name],
        "exported_on": receipt["release"], "official_turns_at_export": len(turns),
        "official_turns_started_after_close": len(after_close),
        "other_acknowledged_turns": len(receipt["turns"]) - len(turns),
        "jobs_by_status": dict(sorted(Counter(job["status"] for job in result["jobs"]).items())),
        "closing_decision": closure["decision_ref"], "committed_event": closure["committed_event"],
        "closed_event": closure["closed_event"], "closed_utc": closure["closed_utc"],
        "report_sha256": report, "decision_md_sha256": hashlib.sha256(decision_text).hexdigest(),
        "reexport_added": receipt["reexport_added"],
        "files": [str((folder / file).relative_to(COLLECTION.parent))
                  for file in ("REPORT.md", "DECISION.md", "receipt.json", "result.json")],
    }


if __name__ == "__main__":
    rows = [study(name) for name in ADMISSION]
    if len({row["run"] for row in rows}) != len(rows):
        raise ValueError("Duplicated Run ID")
    jobs = Counter()
    for row in rows:
        jobs.update(row["jobs_by_status"])
    totals = {
        "closed_runs": len(rows),
        "official_turns_at_export": sum(row["official_turns_at_export"] for row in rows),
        "official_turns_started_after_close": sum(row["official_turns_started_after_close"] for row in rows),
        "jobs_by_status": dict(sorted(jobs.items())),
    }
    print(json.dumps({
        "scope": "Primary OAuth science studies. Focused regressions, controlled callbacks and standalone GPU calibration are counted separately.",
        "totals": totals,
        "studies": rows,
    }, ensure_ascii=False, indent=2))
