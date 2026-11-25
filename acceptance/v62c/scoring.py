"""Public four-cell score. The first delivered measurement and the later action are judged separately."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from gx_core import contracts

SCHEMA = "gx.v62c.causal-score/1"
LEGACY = "gx.v62b.paired-decision-protocol/1"


def digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def need(ok, reason):
    if not ok:
        raise ValueError(reason)


def normalize_nature(nature, version=SCHEMA):
    need(version in (SCHEMA, LEGACY), "Unknown measurement selector version")
    if version == LEGACY and nature == "observation":
        nature = "observed"
    need(nature in contracts.label_values("output_nature"), "Unknown published output nature")
    return nature


def validate_assay(rule):
    """Normalize once at registration and again at scoring against the same product vocabulary."""
    required = {"capability", "output_name", "nature", "parameters", "metric_id", "numerator", "denominator", "unit"}
    need(set(rule) == required and all(isinstance(rule[k], str) and rule[k].strip() for k in required - {"parameters"}),
         "Expected a complete versioned named-output assay")
    need("@" in rule["capability"] and isinstance(rule["parameters"], dict) and
         all(rule[k].startswith("/") for k in ("numerator", "denominator")), "Invalid method, selectors or parameters")
    nature = normalize_nature(rule["nature"])
    need(nature in {"observed", "analysis"}, "A prediction does not satisfy the pilot's measurement requirement")
    return {**rule, "nature": nature}


def _pointer(value, pointer):
    for part in pointer[1:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list) and part.isdecimal():
            index = int(part)
            if index >= len(value):
                return None
            value = value[index]
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return value


def _qualified(job, assignment, rule):
    """The new Run, receipt and method version must meet the prespecified selective assay."""
    pin = {k: assignment[k] for k in ("sha256", "artifact_ref")}
    ordered = (assignment["event_id"] <= job["grant_event"] <= job["actor_cursor"] <
               job["commission_event"] < job["admission_event"] < job["settled_event"])
    has_source = any(s.get("role") == assignment["role"] and all(s.get(k) == v for k, v in pin.items())
                     for s in job["source_inputs"])
    return (ordered and has_source and pin in job["data_refs"] and job["original_inputs_grounded"]
            and bool(job["original_observations"]) and job["scope_authorized"]
            and job["capability"] == rule["capability"] and job["parameters"] == rule["parameters"]
            and (job["output_name"], job["nature"]) == (rule["output_name"], rule["nature"]))


def _rendered(metric, job, rule):
    """Check the signed GX fields visible in the packet against the actual named output bytes."""
    if not isinstance(metric, dict):
        return False
    target, total = (_pointer(job["document"], rule[k]) for k in ("numerator", "denominator"))
    return (metric in job["verified_metrics"] and metric.get("schema") == "gx.verified_metric/1" and metric.get("metric_id") == rule["metric_id"]
            and (metric.get("artifact_ref"), metric.get("sha256"), metric.get("job_id")) ==
                (job["artifact_ref"], job["sha256"], job["job_id"])
            and metric.get("output_name") == rule["output_name"] and metric.get("unit") == rule["unit"]
            and metric.get("admission_sha256") == job["admission_sha256"]
            and metric.get("manifest_sha256") == job["completion_sha256"]
            and metric.get("method") == job["method"] and bool(metric.get("metric_ref"))
            and metric.get("selectors") == {k: rule[k] for k in ("numerator", "denominator")}
            and type(target) is int and type(total) is int and 0 <= target <= total and total > 0
            and type(metric.get("numerator")) is int and type(metric.get("denominator")) is int
            and (metric["numerator"], metric["denominator"]) == (target, total)
            and metric.get("value") == target / total and metric.get("op") == "ratio/1"
            and all(metric.get("scope", {}).get(k) == v for k, v in rule["parameters"].items()))


def _cues(trace, turn):
    rule, assignment = trace["assay"], trace["assignment"]
    accepted = []
    for candidate in turn["candidates"]:
        metric = candidate["metric"]
        job = trace["measurements"].get(metric.get("artifact_ref"))
        if not job or not _qualified(job, assignment, rule) or not _rendered(metric, job, rule):
            continue
        if not job["settled_event"] <= turn["cursor"] or job["run_id"] != trace["run_id"]:
            continue
        report_ref = candidate.get("report_ref")
        if job["scope"] == "run":
            if report_ref is not None:
                continue
        elif not (report_ref and candidate.get("scope") == job["scope"] and candidate["native_publication"]
                  and job["settled_event"] < candidate["published_event"] <= candidate["released_event"] <= turn["cursor"]
                  and job["artifact_ref"] in candidate["report_sources"]):
            continue
        accepted.append({"metric": metric, "job": job["job_id"], "scope": job["scope"],
                         "report_ref": report_ref, "source_refs": [job["artifact_ref"]] + ([report_ref] if report_ref else [])})
    return accepted


def opportunity_packet(trace):
    """Reader sees the original plan and measured values in delivery order, ahead of the outcome key."""
    windows = []
    for turn in trace["lead_windows"]:
        cues = _cues(trace, turn)
        windows.append({"window": turn["window"], "brief_sha256": turn["brief_sha256"],
                        "measurements": [{"metric_id": c["metric"]["metric_id"], "value": c["metric"]["value"],
                                          "numerator": c["metric"]["numerator"], "denominator": c["metric"]["denominator"],
                                          "unit": c["metric"]["unit"]} for c in cues]})
        if cues:
            break
    return {"schema": SCHEMA, "initial_plan": trace["initial_plan"]["rationale"], "windows": windows,
            "blinding": "Assignment, arm, source identities, post-measurement choices and costs withheld. Scientific wording and delivery order may reveal conditions."}


def lock_opportunity(trace, *, reader="scripted-opportunity-reader", prior_accommodated_excerpt=None):
    packet = opportunity_packet(trace)
    if prior_accommodated_excerpt is not None:
        need(prior_accommodated_excerpt.strip() and prior_accommodated_excerpt in packet["initial_plan"],
             "An already accommodated gap requires the original plan's exact passage")
    first = next((w for w in packet["windows"] if w["measurements"]), None)
    actual = next((w for w in trace["lead_windows"] if first and w["window"] == first["window"]), None)
    cues = _cues(trace, actual) if actual else []
    state = ("PLAN_ALREADY_ACCOMMODATED" if first and prior_accommodated_excerpt else
             "DELIVERED" if first else "NO_ELIGIBLE_DELIVERY")
    identity = {k: trace[k] for k in ("run_id", "assay", "assignment")}
    record = {"schema": SCHEMA, "reader": reader, "packet_sha256": digest(packet), "study_binding": digest(identity),
              "window": first["window"] if first else None, "state": state,
              "prior_accommodated_excerpt": prior_accommodated_excerpt,
              "measurements": cues, "source_refs": sorted({ref for c in cues for ref in c["source_refs"]})}
    return {**record, "sha256": digest(record)}


def outcome_packet(trace, lock):
    """Blinded second-stage record. Ref labels are stable within a single Run."""
    expected = lock_opportunity(trace, reader=lock["reader"], prior_accommodated_excerpt=lock["prior_accommodated_excerpt"])
    need(lock == expected, "The locked opportunity or its original measured evidence changed")
    turn = next((w for w in trace["lead_windows"] if w["window"] == lock["window"]), None)
    action = turn["action"] if turn else None
    effects = [] if not action else [{k: v for k, v in effect.items() if k in ("ref", "kind", "branch", "capability", "parameters", "input_sha256")}
                                      for effect in action["effects"]]
    return {"opportunity_sha256": lock["sha256"], "initial_plan": trace["initial_plan"]["rationale"],
            "original_branches": [op["branch"] for op in trace["initial_plan"]["ops"] if op["op"] == "open_branch"],
            "claims": trace.get("claim_publications", []),
            "state": lock["state"], "rationale": action["rationale"] if action else None,
            "cited_assigned_measurement": bool(action and set(action["cited"]).intersection(lock["source_refs"])),
            "effects": effects}


def adjudicate(trace, lock, review):
    packet = outcome_packet(trace, lock)
    need(review["lock_sha256"] == lock["sha256"] and review["reviewer"] != lock["reader"]
         and review["reviewer"].strip() and review["reason"].strip(), "Lock and distinct reader/adjudicator are required")
    need(review["condition"] in ("CHANGE", "MAINTAIN") and review["disposition"] in
         ("CHANGE", "MAINTAIN", "MISSED", "AUTOMATIC_ONLY", "NO_OPPORTUNITY", "UNRESOLVED"),
         "Unknown blinded scientific disposition")
    need(type(review["unsupported_support"]) is bool and type(review["scope_appropriate"]) is bool,
         "Claim direction and scope need independent judgments")
    choice = review["disposition"]
    turn = next((w for w in trace["lead_windows"] if w["window"] == lock["window"]), None)
    action = turn["action"] if turn else None
    linked = bool(action and action["event_id"] > turn["cursor"] and packet["cited_assigned_measurement"])
    excerpt = review.get("rationale_excerpt", "").strip()
    plan_excerpt = review.get("prior_plan_excerpt", "").strip()
    justified = bool(linked and excerpt and excerpt in action["rationale"] and plan_excerpt and
                     plan_excerpt in packet["initial_plan"] and review["scope_appropriate"])
    selected = next((e for e in packet["effects"] if e["ref"] == review.get("effect_ref")), None)
    original_branches = {op["branch"] for op in trace["initial_plan"]["ops"] if op["op"] == "open_branch"}
    continuing = bool(selected and selected["kind"] in ("completed_job", "maintained_job") and
                      (not original_branches or selected.get("branch") in original_branches))
    realized = bool((choice == "CHANGE" and selected) or (choice == "MAINTAIN" and continuing))
    if choice == "NO_OPPORTUNITY":
        need(lock["state"] != "DELIVERED", "The first delivered value remains in the denominator")
    if choice == "AUTOMATIC_ONLY":
        need(turn and turn.get("automatic_nodes") and not (action and action["effects"]),
             "No automatic-only dispatch in this decision window")
    passed = (lock["state"] == "DELIVERED" and choice == review["condition"] and justified and realized
              and not review["unsupported_support"] and not trace["missing"] and trace["closed"])
    delegated = [c for c in lock["measurements"] if c["report_ref"] and
                 action and set(c["source_refs"]).intersection(action["cited"])]
    demonstrated = bool(delegated and linked and justified and realized and not trace["missing"])
    return {"schema": SCHEMA, "run_id": trace["run_id"], "opportunity": lock["state"], "first_window": lock["window"],
            "science_pass": passed, "decision": choice, "citation_matches": linked, "action_realized": realized,
            "effect": selected, "unsupported_support": review["unsupported_support"],
            "scope_appropriate": review["scope_appropriate"], "enactment_receipt": None if not selected else
                next(e for e in action["effects"] if e["ref"] == selected["ref"]),
            "delegation": ("NOT_APPLICABLE" if not trace["offered"] else
                           "DEMONSTRATED" if demonstrated else "USED_INCOMPLETE" if trace["opened"] else "NOT_USED"),
            "delegation_offered": trace["offered"], "modules_opened": trace["opened"],
            "actor_class": trace["actor_class"], "live_model_decisions": 0 if trace["actor_class"] == "SCRIPTED_REHEARSAL" else None,
            "opportunity_sha256": lock["sha256"], "review_sha256": digest(review), "outcome_packet_sha256": digest(packet),
            "costs": trace["costs"]}


def extract(slot_dir, run_id, assay, assignment, *, sessions=None):
    from acceptance.v62c.witness import extract_run
    return extract_run(Path(slot_dir), run_id, validate_assay(assay), assignment, sessions)
