"""Judge a public comparison from its registered route and actual committed effects."""
from __future__ import annotations

from fractions import Fraction
import operator
import re

from acceptance.v62c import scoring as prior

need = prior.need
digest = prior.digest
SCHEMA = "gx.v622.public-action-score/2"


def eligible(job, assigned, rule):
    pin = {k:assigned[k] for k in ("sha256","artifact_ref")}
    first = job.get("first_module_grant") or {}
    ordinary = assigned["event_id"] <= job["grant_event"] <= job["actor_cursor"] < job["commission_event"]
    initial = (assigned["event_id"] <= job["actor_cursor"] < job["grant_event"] < job["commission_event"]
               and first.get("task_id")==job.get("actor_task") and first.get("scope")==job["scope"]
               and first.get("grant_event")==job["grant_event"] and first.get("grant_sha256"))
    return ((ordinary or initial) and job["commission_event"] < job["admission_event"] < job["settled_event"]
            and job["scope_authorized"] and job["original_inputs_grounded"] and job["original_observations"]
            and pin in job["data_refs"] and any(s.get("role")==assigned["role"] and
                all(s.get(k)==v for k,v in pin.items()) for s in job["source_inputs"])
            and job["capability"]==rule["capability"] and job["parameters"]==rule["parameters"]
            and (job["output_name"],job["nature"])==(rule["output_name"],rule["nature"]))


def delivered(trace,window):
    seen=[]
    for candidate in window["candidates"]:
        m=candidate["metric"]
        j=trace["measurements"].get(m.get("artifact_ref"))
        if not (j and j["run_id"]==trace["run_id"] and eligible(j,trace["assignment"],trace["assay"])
                and prior._rendered(m,j,trace["assay"]) and j["settled_event"]<=window["cursor"]):
            continue
        ref=candidate.get("report_ref")
        if j["scope"]=="run":
            if ref:
                continue
        elif not (ref and candidate.get("scope")==j["scope"] and candidate["native_publication"]
                  and j["settled_event"]<candidate["published_event"]<=candidate["released_event"]<=window["cursor"]
                  and j["artifact_ref"] in candidate["report_sources"]):
            continue
        seen.append({"metric":m,"job":j["job_id"],"scope":j["scope"],"report_ref":ref,
                     "source_refs":[j["artifact_ref"]]+([ref] if ref else [])})
    return seen


def comparison_branches(trace):
    family = trace.get("provisional_method_family", "registered reference comparison")
    return [op["branch"] for op in trace["initial_plan"]["ops"]
            if op["op"]=="open_branch" and op.get("method_family")==family]


def opportunity(trace):
    packet={"schema":SCHEMA,"initial_plan":trace["initial_plan"]["rationale"],"windows":[],
            "blinding":"Only the opening and delivered values are available at this stage."}
    cues=[]
    first=None
    for w in trace["lead_windows"]:
        cues=delivered(trace,w)
        packet["windows"].append({"window":w["window"],"brief_sha256":w["brief_sha256"],"measurements":[
            {k:c["metric"][k] for k in ("metric_id","value","numerator","denominator","unit")} for c in cues]})
        if cues:
            first=w["window"]
            break
    lock={"schema":SCHEMA,"reader":"registered-public-opportunity-reader","packet_sha256":digest(packet),
          "study_binding":digest({k:trace[k] for k in ("run_id","assay","assignment")}),
          "window":first,"state":("NO_ELIGIBLE_DELIVERY" if not first else "DELIVERED" if comparison_branches(trace)
                                 else "NO_REGISTERED_COMPARISON"),
          "measurements":cues,"source_refs":sorted({r for c in cues for r in c["source_refs"]})}
    lock["sha256"]=digest(lock)
    return packet,lock


def compare_window(trace, lock):
    _, measured = opportunity(trace)
    need(measured == lock, "The frozen reader decision or its underlying delivery changed")
    window = next((w for w in trace["lead_windows"] if w["window"] == lock["window"]), None)
    action = window["action"] if window else None
    initial = trace["initial_plan"]
    branches = comparison_branches(trace)
    return {"schema":SCHEMA, "lock_sha256":lock["sha256"], "initial_plan":initial["rationale"],
            "initial_decision":initial["decision_ref"], "original_branches":branches,
            "decision_ref":action["decision_ref"] if action else None,
            "rationale":action["rationale"] if action else "", "effects":action["effects"] if action else [],
            "cited":action["cited"] if action else [], "ops":action["ops"] if action else [],
            "event_id":action["event_id"] if action else None, "window_cursor":window["cursor"] if window else None,
            "automatic_nodes":window["automatic_nodes"] if window else [], "state":lock["state"]}


def independent_endpoint(trace, lock, protocol):
    count = trace["independent_count"]
    target = trace["assignment"]["sha256"]
    need(target == count["sha256"], "Independent reading is for a different assigned file")
    n,d = count["selected"],count["observed"]
    need(type(n) is int and type(d) is int and 0 <= n <= d and d > 0, "Unusable independent coverage counts")
    threshold = protocol["threshold"]
    expected = threshold["upper_action"] if Fraction(n,d) >= Fraction(
        threshold["numerator"],threshold["denominator"]) else threshold["lower_action"]
    scope = {"input_sha256":target,"mask":count["mask"],"entity":"1","model":"1","chains":["A","B","C"]}
    matching = bool(lock["measurements"]) and all(
        (m["metric"]["numerator"],m["metric"]["denominator"]) == (n,d)
        and m["metric"]["scope"] == scope for m in lock["measurements"])
    return expected, matching


def _original_job(job, rule, parent):
    return (job.get("capability")==rule["capability"] and job.get("parameters")==rule["parameters"]
            and job.get("input_sha256")==[parent])


def enacted(packet, trace, protocol):
    """Determine route continuity independently of condition and declared disposition."""
    e = packet["effects"]
    original = set(packet["original_branches"])
    assay = protocol["assay"]
    parent = protocol["materials"]["parent_sha256"]
    # A new node or branch ID does not constitute a different scientific comparison.
    same = next((x for x in e if x["kind"]=="completed_job" and _original_job(x,assay,parent)),None)
    if same:
        return "MAINTAIN", same
    if any(x["kind"]=="maintained_job" and _original_job(x,assay,parent) for x in e):
        return "AUTOMATIC_ONLY", None
    if any(_original_job(j,assay,parent) for j in trace["completed_jobs"]):
        return "UNRESOLVED", None
    stopped = next((x for x in e if x["kind"]=="withdrawn_branch" and x["ref"] in original),None)
    closed = next((x for x in e if x["kind"]=="closed_run"),None)
    closed_op = next((op for op in packet["ops"] if op["op"]=="close_run" and op.get("unresolved")),None)
    if stopped:
        return "CHANGE", stopped
    restricted = closed_op and any(re.search(r"compar|backbone|covered|coverage|threshold|比較|覆蓋",
                                             text,re.IGNORECASE) for text in closed_op["unresolved"])
    if closed and restricted and trace["closed"] and original:
        return "CHANGE", {**closed,"unresolved":closed_op["unresolved"]}
    if packet["automatic_nodes"] and not e:
        return "AUTOMATIC_ONLY", None
    return "MISSED", None


def reasoning(rationale):
    """The scripted readers recognize coverage justification and leave unknown wording unclassified."""
    text = rationale.casefold()
    grounded = re.search(r"cover|complet|shared|threshold|門檻|覆蓋|共同骨架",text)
    low = re.search(r"below|insufficien|deficit|inadequate|too few|falls? short|低於|不足",text)
    high = re.search(r"passes|exceeds|sufficient|enough|above|meets|高於|達到|足夠",text)
    if grounded and low:
        return "CHANGE"
    if grounded and high:
        return "MAINTAIN"
    return "UNRESOLVED"


def _verified_support(claim, trace, grounded):
    # A positive scoped prediction must stand on the independently parsed specimen.
    # The per-material scientific finding, clock and input identity must all agree.
    reg = (claim.get("maturity") or {}).get("registration") or {}
    pred = reg.get("prediction") or {}
    spec = (reg.get("method_pin") or {}).get("measurement_spec") or {}
    target = trace["assignment"]
    if (not grounded or reg.get("claim_scope")!=claim["scope"]
            or {d["sha256"] for d in reg.get("data",[])}!={target["sha256"]}
            or any(spec.get(k)!=trace["assay"][k] for k in ("capability","output_name","unit","metric_id","parameters"))
            or spec.get("input_role")!=target["role"]):
        return False
    comparison = {"lt":operator.lt,"le":operator.le,"gt":operator.gt,"ge":operator.ge,"eq":operator.eq}.get(pred.get("operator"))
    raw = trace["independent_count"]
    if not (comparison and isinstance(pred.get("threshold"),(int,float)) and type(reg.get("event_id")) is int):
        return False
    accepted_outputs = [j for j in trace["measurements"].values() if j["artifact_ref"] in
                        {g["source_ref"] for g in grounded}]
    return bool(accepted_outputs and reg["event_id"] < min(j["settled_event"] for j in accepted_outputs)
                and comparison(Fraction(raw["selected"],raw["observed"]),Fraction(str(pred["threshold"]))))


def formal_evidence(trace, lock):
    """Compare formal numerics to the actual delivered measurement. Keep absent claims absent."""
    by_id = {c["metric"]["metric_ref"]: c for c in lock["measurements"]}
    grounded, unsupported = [], []
    for claim in trace["claim_publications"]:
        pub = claim.get("publication") or {}
        claim_ground = []
        for m in claim.get("verified_metrics") or []:
            source = by_id.get(m.get("metric_ref"))
            if source and m == source["metric"] and set(source["source_refs"]).intersection(claim["evidence_refs"]):
                claim_ground.append({"source_ref":m["artifact_ref"],"claim_revision_id":claim["claim_revision_id"],"sha256":claim["sha256"],
                                 "metric_ref":m["metric_ref"],"display":m["display"],"level":claim["level"]})
        grounded.extend(claim_ground)
        if (claim["level"]=="limited_support" and not _verified_support(claim,trace,claim_ground)) or (pub and
                (pub.get("level")!=claim["level"] or pub.get("scope")!=claim["scope"])):
            unsupported.append(claim["claim_revision_id"])
    return grounded, unsupported


def rate(trace, lock, protocol):
    packet = compare_window(trace,lock)
    expected, exact = independent_endpoint(trace,lock,protocol)
    actual, effect = enacted(packet,trace,protocol)
    refs = set(packet["cited"])
    cited = bool(refs.intersection(lock["source_refs"]))
    linked = bool(cited and packet["event_id"] is not None and packet["event_id"] > packet["window_cursor"])
    scientific = reasoning(packet["rationale"]) if linked else "UNRESOLVED"
    reading,unsupported = formal_evidence(trace,lock)
    complete = lock["state"]=="DELIVERED" and linked and exact and not trace["missing"]
    realized = bool(effect and actual in ("CHANGE","MAINTAIN") and linked)
    science_pass = bool(complete and scientific==expected==actual and realized and not unsupported and trace["closed"])
    delegated = any(c["report_ref"] and c["report_ref"] in refs for c in lock["measurements"])
    demonstrated = bool(delegated and complete and realized and scientific==actual)
    designation = ("NOT_APPLICABLE" if not trace["offered"] else "DEMONSTRATED" if demonstrated
                   else "USED_INCOMPLETE" if trace["opened"] else "NOT_USED")
    review = {"schema":SCHEMA,"reader":lock["reader"],"adjudicator":"registered-public-action-reader",
              "lock_sha256":lock["sha256"],"expected":expected,"rationale":packet["rationale"],
              "judgment":scientific,"enacted":actual,"effect":effect,
              "independent_count":trace["independent_count"],"metric_matches_independent_counts":exact}
    decision = actual if lock["state"]=="DELIVERED" else "NO_OPPORTUNITY"
    result = {"schema":SCHEMA,"run_id":trace["run_id"],"first_window":lock["window"],"actor_class":trace["actor_class"],
              "opportunity":lock["state"],"condition":expected,"decision":decision,"scientific_judgment":scientific,
              "action_realized":realized,"science_pass":science_pass,"citation_matches":linked,"scope_appropriate":exact,
              "enactment_receipt":effect,"effect":effect,"decision_ref":packet["decision_ref"],"delegation":designation,
              "modules_opened":trace["opened"],"delegation_offered":trace["offered"],"costs":trace["costs"],
              "independent_quantity_mismatch":not exact and lock["state"]=="DELIVERED","formal_readings":reading,"claim_count":len(trace["claim_publications"]),"unsupported_support":bool(unsupported),
              "unsupported_claim_revisions":unsupported,"opportunity_sha256":lock["sha256"],
              "outcome_packet_sha256":digest(packet),"review_sha256":digest(review),
              "original_decision_ref":packet["initial_decision"],"paid_model_calls":0,"live_model_decisions":0,
              "provider_tokens":None,"provider_usd":None,"closed":trace["closed"],"missing":trace["missing"]}
    return packet, review, result
