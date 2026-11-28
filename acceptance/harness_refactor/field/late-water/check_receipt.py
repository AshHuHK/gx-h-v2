"""Verify receipt invariants against the exported original and independent archive reference."""
import json
from pathlib import Path

from verify_reference import build_reference

HERE = Path(__file__).resolve().parent


def assess(data):
    source = build_reference()
    assert data["feedback"][0]["source_message_id"] == "filing-nwis-april-second-station"
    recovery = data["recovery"]
    target = recovery["pre-crash"]["selected_recipient"]["scope"]
    early = recovery["pre-crash"]["event"]
    run = data["run"]["run_id"]
    assert run == recovery["pre-crash"]["goal"]["run_id"] == recovery["queued"]["goal"]["run_id"]
    assert recovery["disconnect"]["exit"] == -9
    first = next(ref for ref,f in data["materials"].items() if f["name"] == "first-station.json")
    second = next(ref for ref,f in data["materials"].items() if f["name"] == "later-station.json")
    assert all(f["raw_matches_accepted_input"] for f in data["materials"].values())
    assert data["materials"][second]["sha256"] == source["stations"]["01651000"]["attachment_sha256"]
    grant, = [e for e in data["timeline"] if e["kind"] == "v621.module.material_granted" and
              e["receipt"]["artifact_ref"] == second]
    assert grant["receipt"]["scope"] == target and grant["event_id"] > early
    assert len(recovery["queued"]["messages"]) == 1
    reports = data["reports"]
    assert all(r["whole_report_verified"] and r["official_worker"] for r in reports)
    early_reports = [r["event_id"] for r in reports if r["event_id"] <= early]
    assert early_reports and all(first in r["source_refs"] for r in reports if r["event_id"] in early_reports)
    original_target_reads = [r["call_id"] for r in data["calls"] if r["official_turn"] and r["scope"] == target and
                             r["tool"] == "gx_read_artifact" and r["ref"] == second and r["full_document_match"]]
    source_reads = [r["call_id"] for r in data["calls"] if r["official_turn"] and r["scope"] == target and
                    r["tool"] == "gx_read_source" and r.get("ref") == second and r.get("receipt_id")]
    late_reports = [r["event_id"] for r in reports if r["scope"] == target and r["event_id"] > grant["event_id"]]
    assert late_reports, "no post-grant target report in this capture"
    refs_by_scope = {role:{r["ref"] for r in data["calls"] if r.get("full_document_match") and r["scope"] == role}
                     for role in {r["scope"] for r in data["calls"]}}
    return {"run_id":run, "recorded_status":data["run"]["status"], "scope":target, "crash_exit":-9,
            "pre_crash_high_water":early, "prior_reports_preserved":early_reports, "late_grant_event":grant["event_id"],
            "target_report_events_after_grant":late_reports, "original_target_late_document_reads":original_target_reads,
            "original_target_late_source_reads":source_reads,
            "all_model_scopes_reading_late_document":sorted(k for k,v in refs_by_scope.items() if second in v),
            "observed_jobs":len(data["jobs"]), "route_events":[x["event_id"] for x in data["timeline"]
                                                              if x["kind"] == "v6.route.revised"],
            "peer_events":[x["event_id"] for x in data["timeline"]
                           if x["kind"].startswith(("research.topic.", "v62.module.peer"))]}


if __name__ == "__main__":
    print(json.dumps(assess(json.loads((HERE / "receipt.json").read_bytes())),indent=2))
