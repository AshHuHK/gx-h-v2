"""Public engineering surrogate; only its native GX packet and earlier tool replies enter the decision."""
from acceptance.v62c import pilot_peer

METHOD = "deposited-block-influence@1"
MASKS = ("common_backbone", "common_ca")
ENDPOINTS = ("max_angle_deg", "max_rmsd_A")
READOUTS = (*ENDPOINTS,"eligible_blocks")
# Preregistered operating tolerances of this transparent scripted actor.
ANGLE_LIMIT = 0.5
DISTANCE_LIMIT = 0.15
PLAN = ("Prioritize whether one continuous interval changes a pairwise fit enough to affect geometric reporting. "
        "Compare leave-interval refits, first with shared complete backbone, then with shared alpha carbons. "
        "I predict either that every eligible pair/interval stays within 0.5 degrees and 0.15 angstrom, "
        "or that at least one exceeds a corresponding tolerance. Under the first outcome I will retain the "
        "bounded summary. Under the second I will limit it to the analyzed pairs and report the exceptions. "
        "A median can hide an individual pair; missing measurements leave the question open.")


def actual_metrics(packet, replies, ref):
    observations = [o for o in packet["observations"] if o["artifact_ref"] == ref]
    items = [m for o in observations for m in o.get("verified_metrics", ())]
    pages = [value for (name, source, _), value in replies.pages.items()
             if name=="gx_read_artifact" and source==ref]
    items.extend(m for p in pages for m in p.get("verified_metrics", ()))
    metrics = {m["metric_id"]:m for m in items if m["metric_id"] in READOUTS and m["artifact_ref"] == ref}
    if set(metrics) == set(READOUTS):
        return metrics, None
    offset = pages[-1]["next_endpoint_offset"] if pages else 0
    if offset is not None:
        return {}, ("gx_read_artifact", {"ref":ref, "endpoint_offset":offset})
    return metrics, None


def observed_conclusion(metrics):
    if not set(ENDPOINTS).issubset(metrics):
        return "Indeterminate for the missing eligible endpoint; a general robustness claim remains open."
    sufficient=metrics.get("eligible_blocks",{}).get("value",0)>=3
    exceed=[]
    for key,limit,label in zip(ENDPOINTS,(ANGLE_LIMIT,DISTANCE_LIMIT),("rotation","RMSD"),strict=True):
        m=metrics[key]
        if m.get("value") is None:
            return "Indeterminate: no usable numerical endpoint for the declared domain."
        if m["value"] > limit:
            exceed.append(label)
    if exceed:
        return ("The observed " + " and ".join(exceed) +
                " exceeds the preregistered reporting tolerance. Preserve pair/interval detail; "
                "a representative value alone cannot establish uniform robustness.")
    if not sufficient:
        return "Too few eligible intervals for a deposited-structure-wide summary; preserve the observed cells as a limited result."
    return ("The delivered maxima remain within the preregistered tolerances for the measured "
            "pair/interval set. Keep any summary bounded by its eligibility and mask.")


def decision(ops, reason, refs=(), *, metrics=(), event="evidence_added", subject="run"):
    name,args=pilot_peer.decision(ops,reason,refs,event,subject)
    if metrics:
        args["decision"]["metric_refs"]=[m["metric_ref"] for m in metrics]
    return name,args


def commission(branch, material, mask):
    return {"op":"add_node","node":"local:measure","branch":branch,"capability":METHOD,
            "inputs":[material["artifact_ref"]],"parameters":{"mask":mask},"depends_on":[],
            "question":"What is the change in the within-deposition pair fits after interval omission for this atom mask?"}


def stage(ref, metrics):
    return ("gx_stage_claim", {"statement":" ".join("{{metric:"+key+"}}" for key in ENDPOINTS if key in metrics),
            "level":"open","scope":"Descriptive archived coordinate sensitivity under the cited mask and eligibility rules.",
            "criteria":"Compare the two unrounded named maxima with the reporting tolerances registered before measurement.",
            "limitations":"Observed positions within this archive only. Sample independence and behavior outside this structure remain unknown.",
            "rationale":"Both maxima derive from the cited refits; assess the eligibility and the individual table rows before generalizing.",
            "evidence_refs":[ref]})


def step(brief, replies):
    branch=next((b for b in brief["branches"] if b.get("spec",{}).get("method_family")=="Within-deposition block refits"),None)
    if not branch:
        if ("gx_query_catalog",None) not in replies:
            return "gx_query_catalog",{"kind":"method","reference":METHOD}
        catalog=replies["gx_query_catalog",None]["source_ref"]
        card=pilot_peer.ref_for(brief,"workflow_spec")
        for ref in (card,catalog):
            if ("gx_read_artifact",ref) not in replies:
                return "gx_read_artifact",{"ref":ref}
        op={"op":"open_branch","branch":"local:structure","hypothesis":"Reported geometry remains stable across eligible exclusions.",
            "alternative":"At least one pair is sensitive to a continuous interval.",
            "method_family":"Within-deposition block refits","discriminating_observation":"Within-mask changes in rotation and RMSD after exclusion.",
            "budget_limits":{"job_submissions":4,"cpu_seconds":480},
            "stop_condition":"Close within the measured scope or when eligibility prevents further discrimination."}
        return decision([op], PLAN)
    materials=sorted((m for m in brief["materials"] if m["role"]=="structure"),key=lambda m:m["assignment_event_id"])
    if not materials:
        return None
    material=materials[-1]
    nodes=[n for n in brief["nodes"] if n["commission"]["inputs"]==[material["artifact_ref"]]]
    nodes=sorted(nodes,key=lambda n:MASKS.index(n["commission"]["parameters"]["mask"]))
    if not nodes:
        op=commission(branch["branch_id"],material,MASKS[0])
        return decision([op],"Measure the eligible shared complete-backbone comparison on the newly assigned source.",
                        event="job_settled",subject=op["node"])
    node=nodes[-1]
    ref=node.get("result_ref")
    if not ref:
        return decision([],"Await this admitted analysis.",event="job_settled",subject=node["node_id"])
    metrics, request=actual_metrics(brief,replies,ref)
    if request:
        return request
    inference=observed_conclusion(metrics)
    staged=replies.get(("gx_stage_claim",None))
    if metrics and not staged:
        return stage(ref,metrics)
    refs=[ref]+([staged["claim_ref"]] if staged else [])
    if len(nodes)==1:
        op=commission(branch["branch_id"],material,MASKS[1])
        return decision([op],inference+" Test atom-selection sensitivity on the same source using the alpha-carbon mask.",
                        refs,metrics=metrics.values(),event="job_settled",subject=op["node"])
    if len(materials)==1:
        if (brief.get("previous_decision") or {}).get("rationale","").startswith("Second-deposition prediction."):
            return None
        if (any(metrics.get(key,{}).get("value") is None for key in ENDPOINTS) or
                metrics.get("eligible_blocks",{}).get("value",0)<3):
            prediction="My preceding scope was incomplete. I expect the comparison to remain unresolved unless the new source has enough qualified intervals."
        elif metrics[ENDPOINTS[0]]["value"]>ANGLE_LIMIT or metrics[ENDPOINTS[1]]["value"]>DISTANCE_LIMIT:
            prediction="Using the first alpha-carbon result as a working comparison, I predict at least one corresponding pair/interval endpoint will again exceed its reporting limit."
        else:
            prediction="Using the first alpha-carbon result as a working comparison, I predict the second alpha-carbon mask will stay within both reporting limits."
        reason=("Second-deposition prediction. "+inference+" "+prediction+
                " Test both masks on the distinct archive. "
                "If its unrounded maxima stay within both limits, retain a bounded summary of eligible positions; "
                "if either exceeds its limit, retain the associated pair-and-interval detail. Insufficient coverage "
                "leaves that comparison undetermined. A shared publication does not establish sampling independence.")
        return decision([],reason,refs,metrics=metrics.values())
    op={"op":"close_run","claim_refs":[staged["claim_ref"]] if staged else [],"reason":inference,
        "unresolved":["Sampling independence is unknown; individual pair and interval observations remain scoped to each deposition."]}
    return decision([op],inference,refs,metrics=metrics.values())
