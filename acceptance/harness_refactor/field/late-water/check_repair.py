"""Evaluate the targeted official engineering repeat, retaining its independent scientific outcome."""
import json
from pathlib import Path
import sys

from verify_reference import build_reference


def proof(data):
    target = data["recovery"]["pre-crash"]["selected_recipient"]
    if target is None:
        return {"admitted":True,"target_scope":None,"official_delivery":False,
                "why":"Lead appointed no live recipient before the registered cutoff"}
    scope = target["scope"]
    second = next(k for k,v in data["materials"].items() if v["name"] == "later-station.json")
    feedback = next(f for f in data["feedback"] if f["artifact_refs"] == [second])
    fid = feedback["feedback_id"]
    grant = next(e for e in data["timeline"] if e["kind"] == "v621.module.material_granted" and e["receipt"].get("scope") == scope and e["receipt"].get("artifact_ref") == second)
    eligible = [t for t in data["native_turns"] if t["scope"] == scope and t.get("official")
                and t["official"].get("provider") == "openai" and t["full_brief_acknowledged"]
                and t["event_cursor"] >= grant["event_id"]]
    acknowledged = [t for t in eligible if second in t["delivered_refs"]
                    and any(p.get("feedback_id") == fid and p.get("text") == feedback["text"]
                            and second in p.get("artifact_refs",[]) for p in t.get("pi_dialogue", []))]
    contexts = [c for c in data["calls"] if c["scope"] == scope and c["official_turn"] and c["tool"] == "gx_task_context"
                and second in c["present_materials"] and any(p.get("feedback_id") == fid and p.get("text") == feedback["text"]
                and second in p.get("artifact_refs",[]) for p in c["pi_dialogue"])]
    whole_reads = [c for c in data["calls"] if c["scope"] == scope and c["official_turn"] and c["tool"] == "gx_read_artifact"
                   and c.get("ref") == second and c["full_document_match"]]
    citations = [r for r in data["reports"] if r["scope"] == scope and r["whole_report_verified"]
                 and r["official_worker"] and second in r["source_refs"]]
    disclosed = feedback.get("disposition") or {}
    considered = [r for r in data["reports"] if r["scope"] == scope and r["whole_report_verified"]
                  and r["official_worker"] and disclosed.get("product_ref") == r["report_ref"]]
    linked = [d for d in data["decisions"] if d["scope"] == "run" and set(d["evidence_refs"]).intersection(
              r["report_ref"] for r in citations + considered) and d["task_status"] == "ACCEPTED"]
    return {"run_id":data["run"]["run_id"],"goal_state":data["run"]["status"],"target_scope":scope,
            "official_delivery":bool(acknowledged or contexts),
            "grant_event":grant["event_id"], "superseded_task_id":grant["receipt"].get("previous_task_id"),
            "first_scope_turn_after_grant":eligible[0]["turn_id"] if eligible else None,
            "first_new_scope_brief_contains_PI_message_and_ref":bool(eligible and eligible[0] in acknowledged),
            "acknowledged_briefs":[t["turn_id"] for t in acknowledged],
            "native_contexts":[c["call_id"] for c in contexts],
            "actual_full_late_reads":[c["call_id"] for c in whole_reads],
            "worker_report_events_citing_late_file":[r["event_id"] for r in citations],
            "worker_report_events_disposing_feedback":[r["event_id"] for r in considered],
            "worker_disposition": disclosed.get("outcome"),
            "accepted_Lead_decisions_citing_these_reports":[d["event_id"] for d in linked],
            "jobs":len(data["jobs"]), "waiting_for":data["waiting_for"],
            "independent_difference_percentage_points":build_reference()["difference_percentage_points"]}


if __name__ == "__main__":
    print(json.dumps(proof(json.loads(Path(sys.argv[1]).read_bytes())),ensure_ascii=False,indent=2))
