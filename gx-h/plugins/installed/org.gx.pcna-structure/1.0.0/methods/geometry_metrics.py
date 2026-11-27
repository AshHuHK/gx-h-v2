"""Apply signed thresholds and resolution margins to already measured PCNA deletion and control fits."""
from collections import Counter
from math import isfinite

METHOD = "pcna-local-geometry@1"
TOLERANCES = {"angle_deg":0.02,"rmsd_A":0.002}  # Two released single-fit reproducibility tolerances.
UNITS = {"angle_deg":"deg","rmsd_A":"Å"}
PAIRS = ("12","23","31")
MISSED = {"near_zero","attenuated","opposed"}


def validate(parameters):
    required = {"mask","endpoint","effect_minimum","zero_limit","minimum_evaluable"}
    if not required <= parameters.keys() <= required | {"pair","block_index"}:
        raise ValueError("Supply mask, endpoint, effect_minimum, zero_limit, minimum_evaluable; optional pair and block_index")
    if parameters["mask"] not in ("common_ca","common_backbone") or parameters["endpoint"] not in UNITS:
        raise ValueError("Select common_ca/common_backbone and angle_deg/rmsd_A")
    e,z,k = (parameters[key] for key in ("effect_minimum","zero_limit","minimum_evaluable"))
    if not (all(type(x) in (float,int) and isfinite(x) for x in (e,z)) and 0<z<e and type(k) is int and k>=1):
        raise ValueError("Need finite effect_minimum > zero_limit > 0 and integer minimum_evaluable >= 1")
    if parameters.get("pair","12") not in PAIRS or type(parameters.get("block_index",-1)) is not int or parameters.get("block_index",-1)<-1:
        raise ValueError("Choose 12, 23, or 31 and a block_index >= -1")


def classify(rows, median, e, z):
    large = [p for p in PAIRS if abs(rows[p]["delta"])>=e]
    if not large:
        return "quiet"
    if abs(median)<z:
        return "near_zero"
    if abs(median)<e:
        return "attenuated"
    if any(rows[p]["delta"]*median<0 for p in large):
        return "opposed"
    return "represented"


def resolution(delta, median, control, e, tol):
    # Test the sign-sensitive omission over the full ±tol intervals for both observed deltas.
    # A chain is missed iff its signed effect passes e and the median fails to represent that direction.
    # Positive and negative branches are disjoint even when median is within tol of zero.
    if abs(delta)+tol < e:
        return False
    definite = possible = False
    for sign in (-1,1):
        d,m = sign*delta,sign*median
        if d+tol >= e and m-tol < e:
            possible |= control is None or abs(delta)+tol > max(0.,control-tol)
        if d-tol >= e and m+tol < e and control is not None:
            definite |= abs(delta)-tol > control+tol
    return True if definite else None if possible else False


def measured_presence(total, positive, uncertain, minimum):
    if total<minimum:
        return None
    if positive:
        return 1
    return None if uncertain else 0


def assess(table, parameters, *, include_rows=False):
    validate(parameters)
    if table["mask"]!=parameters["mask"] or table["endpoint"]!=parameters["endpoint"]:
        raise ValueError("Table lacks the registered physical selection")
    e,z,k = (parameters[key] for key in ("effect_minimum","zero_limit","minimum_evaluable"))
    ep = parameters["endpoint"]
    tol = TOLERANCES[ep]
    pair,index = parameters.get("pair","12"),parameters.get("block_index",-1)
    counts, annotations = Counter(), []
    count_robust = count_uncertain = count_border = 0
    local = {}
    local_reason = "whole_structure_requested" if index==-1 else "outside_declared_range"
    for block in table["blocks"]:
        record = {"block_index":block["block_index"]}
        if index==block["block_index"] and block["eligible"]:
            chosen = block["pairs"][pair]
            delta = chosen["delta"]
            local = {"pair_delta":delta,"median_delta":block["median"]["delta"],"control_max":chosen["control_max"],
                     "effect_margin":None if delta is None else abs(delta)-e}
            local_reason = None if all(v is not None for v in local.values()) else "one_or_more_local_readouts_unavailable"
        if not block["eligible"]:
            label, robust = "ineligible",None
            if index==block["block_index"]:
                local_reason = "ineligible_interval"
        elif block["median"]["delta"] is None:
            label, robust = "unmeasured",None
        else:
            rows = block["pairs"]
            middle = block["median"]["delta"]
            label = classify(rows,middle,e,z)
            decisions = {}
            margins = {}
            for p,obs in rows.items():
                delta = obs["delta"]
                control = obs["control_max"] if obs["control_count"]==20 else None
                decisions[p] = resolution(delta,middle,control,e,tol)
                margins[p] = {"effect_margin":abs(delta)-e,
                              "noise_margin":None if control is None else abs(delta)-control}
            robust = any(v is True for v in decisions.values()) or (None if None in decisions.values() else False)
            near = [p for p,v in margins.items() if abs(v["effect_margin"])<=tol]
            median_edges = [name for name,val in (("e",e),("z",z)) if abs(abs(middle)-val)<=tol]
            count_border += int(bool(near or median_edges))
            count_robust += int(robust is True)
            count_uncertain += int(robust is None)
            record.update(pair_resolved_missed=decisions,pair_margins=margins,
                          pair_effect_boundary=near,median_boundaries=median_edges)
        record.update(classification=label,noise_resolved_missed=robust)
        counts[label] += 1
        if include_rows:
            annotations.append(record)
    eligible = len(table["blocks"])-counts["ineligible"]
    evaluable = eligible-counts["unmeasured"]
    missed = sum(counts[k] for k in MISSED)
    result = {"selected_residues":table["selected_residues"], "observed_union_residues":table["observed_union_residues"],
              "blocks_total":len(table["blocks"]),"blocks_eligible":eligible,"blocks_evaluable":evaluable,
              "blocks_unmeasured":counts["unmeasured"],"blocks_missed":missed,
              "blocks_noise_resolved_missed":count_robust, "blocks_noise_uncertain":count_uncertain+counts["unmeasured"],
              "blocks_threshold_boundary":count_border,"missed_present":measured_presence(evaluable,missed,counts["unmeasured"],k),
              "noise_resolved_missed_present":measured_presence(evaluable,count_robust,count_uncertain+counts["unmeasured"],k),
              "class_counts":dict(counts),"local_unavailable_reason":local_reason}
    result["raw_unavailable_reason"] = (None if result["missed_present"] is not None else
                                        "too_few_evaluable_blocks" if evaluable<k else "unmeasured_eligible_blocks")
    result["noise_unavailable_reason"] = (None if result["noise_resolved_missed_present"] is not None else
                                         "too_few_evaluable_blocks" if evaluable<k else "uncertain_or_unmeasured_eligible_blocks")
    for endpoint,suffix in (("angle_deg","deg"),("rmsd_A","A")):
        values = {"baseline_pair":table["baseline"][pair] if endpoint==ep else None}
        values.update({fld:local.get(fld) if endpoint==ep else None for fld in ("pair_delta","median_delta","control_max","effect_margin")})
        result.update({name+"_"+suffix:v for name,v in values.items()})
    if include_rows:
        result["annotations"] = annotations
    return result


def sufficient_metric(parameters, metric_id, table_capability, output_name, table_document):
    if (table_capability,output_name,table_document.get("schema")) != (METHOD,"geometry-table.json","gx.pcna-geometry-table/1"):
        return False
    if any(table_document.get(k)!=parameters.get(k) for k in ("mask","endpoint")):
        return False
    result = assess(table_document,parameters)
    value = result.get(metric_id)
    return type(value) in (float,int) and isfinite(value)
