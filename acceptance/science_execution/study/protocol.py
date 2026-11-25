"""Prospective choices and the operator's projection of a formally accepted comparison."""
import json

METHOD = "deposited-block-influence@2"
PLUGIN = "org.gx.block-influence@2.0.0"
SERIES = "GX-V624-INDEPENDENT-DEPOSITION-01"
LIMITS = {"angle_deg": {"unit": "deg", "threshold": 0.5, "tolerance": 0.02},
          "rmsd_A": {"unit": "Å", "threshold": 0.15, "tolerance": 0.002}}
MASKS = ("common_ca", "common_backbone")
INTERVALS = ("21-40", "101-120")


def method_card(packet, method_text):
    # Whitelist fields explicitly so custody paths, accession, prior readings and adjudication stay private.
    windows = {key: {"eligible_masks": [m for m in MASKS if m in data["eligible_masks"]]}
               for key, data in packet["windows"].items() if key in INTERVALS}
    return {"schema":"gx.blinded-coordinate-options/1", "blind_id":packet["blind_id"], "windows":windows,
            "scope_notes":packet.get("scope_notes", []), "capability":METHOD, "endpoints":LIMITS, "method":method_text,
            "prior_study":"https://github.com/AshHuHK/gx-h-v2/blob/f9231023/acceptance/v623/V623_SCIENCE_REPORT.md",
            "prior_scope":"Published pilot: shared-backbone representative values concealed one pair for 2RE9; "
                          "all three exceeded the RMSD limit in the 2O0O reference interval 21–40. "
                          "These are distinct archived deposits from a shared paper; they cannot answer this new question.",
            "budget":"One primary comparison and, if informative, one additional CPU measurement. Artifact reads reuse results.",
            "commitment":"Use one open_branch and comparison_plan. Choose branch, material_role=structure, "
                         "method={capability, parameters:{interval,mask,endpoint},output_name:segment.json, "
                         "metric_id:median_delta_<endpoint>,unit}. Cite this card in evidence_refs. In prediction provide "
                         "blind_id, expected, on_expected and on_alternative in your own words. Describe both competing "
                         "interpretations, the observation that discriminates between them and the corresponding report actions. "
                         "After opening, commission with [structure,mapping]. Read the named output segment.json, "
                         "then cite it and your measured result when making or closing the scientific decision."}


def accepted(db, run_id):
    rows = db.execute("""SELECT d.decision_ref,d.request_json,d.receipt_json,d.thread_id,d.turn_id,d.call_id,e.ts
        FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id,run_id) JOIN events e
        ON e.event_id=json_extract(d.receipt_json,'$.event_id') JOIN v6_goals g USING(run_id)
        WHERE d.run_id=? AND t.scope='run' AND t.goal_version=g.goal_version ORDER BY e.event_id""", (run_id,))
    for row in rows:
        receipt = json.loads(row["receipt_json"])
        yield {"decision_ref":row["decision_ref"], "event_id":receipt["event_id"], "utc":row["ts"],
               "call_id":row["call_id"], "thread_id":row["thread_id"], "turn_id":row["turn_id"],
               "receipt":receipt, "decision":json.loads(row["request_json"])["decision"]}


def primary(record, card, card_ref):
    decision = record["decision"]
    plan = decision.get("comparison_plan") or {}
    bound = record["receipt"].get("comparison") or {}
    proposed = plan.get("prediction") or {}
    m = plan.get("method") or {}
    params = m.get("parameters") or {}
    limit = LIMITS.get(params.get("endpoint"))
    window = card["windows"].get(params.get("interval"), {})
    action_keys = ("expected", "on_expected", "on_alternative")
    if (not bound or bound.get("initial_event") != record["event_id"] or bound.get("decision_ref") != record["decision_ref"]
            or not bound.get("branch") or bound.get("method") != m or bound.get("material_role") != "structure"
            or bound.get("prediction") != proposed or proposed.get("blind_id") != card["blind_id"] or card_ref not in decision.get("evidence_refs", ())
            or not all(isinstance(proposed.get(k), str) and proposed[k].strip() for k in action_keys)
            or proposed["on_expected"].strip() == proposed["on_alternative"].strip()
            or not limit or m.get("capability") != METHOD or m.get("output_name") != "segment.json"
            or m.get("metric_id") != "median_delta_"+params["endpoint"] or m.get("unit") != limit["unit"]
            or params.get("mask") not in window.get("eligible_masks", ())):
        return None
    return {"decision_ref":record["decision_ref"], "event_id":record["event_id"], "utc":record["utc"],
            "goal_version":bound["goal_version"], "comparison_id":bound["comparison_id"], "branch":bound["branch"],
            "prediction":proposed, "method":m, "method_manifest_sha256":bound["method_manifest_sha256"],
            "scope":"All three pairings of the frozen mapped copies in the selected interval only",
            "decision_rule":"Any pair strictly above threshold while the change of medians remains within threshold",
            "operating_limits":limit, "source_ref":card_ref}


def first_primary(decisions, card, card_ref):
    return next((p for d in decisions if (p := primary(d, card, card_ref))), None)
