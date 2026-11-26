"""Compare measured signed-block exceptions across atom selections, using only recomputed Job records."""

from collections import Counter, defaultdict

SIGNED = "deposited-block-signed@1"
TABLE = "deposited-block-influence@1"
MISSED = {"near_zero", "attenuated", "opposed"}
PAIRS = ("AB", "AC", "BC")
RULE = ("effect_minimum", "zero_limit", "minimum_evaluable")


def _readout(job, block, endpoint):
    evidence = job["evidence"]
    signed = job["method"] == SIGNED
    readers = block if signed else block["metrics"].get(endpoint, {})
    delta_key = "delta" if signed else "signed_change"
    pairs = {p: readers.get("pairs", {}).get(p, {}).get(delta_key) for p in PAIRS}
    median = readers.get("median", {}).get(delta_key)
    result = {
        "job_id": job["job_id"], "method": job["method"], "mask": job["parameters"]["mask"],
        "matched_positions": evidence["matched_positions"], "observed_union_positions": evidence["union_positions"],
        "retained_positions": block["retained"] if signed else block["retained_count"],
        "matched_in_interval": block["matched_in_interval"] if signed else len(block["removed_positions"]),
        "evaluable": median is not None and all(v is not None for v in pairs.values()),
        "pair_signed_deltas": pairs, "median_signed_delta": median,
    }
    if signed:
        result["classification"] = block["classification"]
        result["thresholds"] = {key: evidence["parameters"][key] for key in RULE}
        result["effect_pairs"] = list(block.get("effect_pairs", []))
        if block["classification"] in MISSED:
            result["missed_pairs"] = [p for p in result["effect_pairs"]
                                      if block["classification"] != "opposed" or pairs[p] * median < 0]
        result["denominator"] = {key: evidence[key] for key in (
            "blocks_total", "blocks_eligible", "blocks_evaluable", "blocks_unmeasured")}
    else:
        result["eligible"] = block["eligible"]
        result["ineligible_reasons"] = list(block["reasons"])
        # The table's denominator is specific to the endpoint; no signed rule was run.
        total = len(evidence["blocks"])
        eligible = len(evidence["eligible_blocks"])
        evaluable = sum(b["metrics"].get(endpoint, {}).get("median", {}).get("signed_change") is not None
                        for b in evidence["blocks"])
        result["denominator"] = {"blocks_total": total, "blocks_eligible": eligible,
                                 "blocks_evaluable": evaluable, "blocks_unmeasured": eligible - evaluable}
    return result


def matched_comparisons(jobs):
    """Return a row for each missed interval of each verified signed Job.

    Counts refer to source rows. Each row lists all measurements of another mask
    for the identical material and endpoint, including repeated Jobs with different
    decision thresholds. Labels are copied only from a recomputed signed result.
    """
    by_material = defaultdict(list)
    signed_jobs, exceptions, excluded = [], [], []
    for job in jobs:
        method = job["method"]
        if method not in (SIGNED, TABLE):
            continue
        ev = job.get("evidence")
        if not ev or (method == SIGNED and ev.get("discrepancies") != []) or (method == TABLE and ev.get("status") != "PASS"):
            excluded.append({"job_id": job["job_id"], "reason": "missing_or_discrepant_recomputation"})
            continue
        sha = job["original"]["sha256"]
        mask = job["parameters"]["mask"]
        measured_mask = ev["parameters"]["mask"] if method == SIGNED else ev["mask"]
        if sha != ev["input_sha256"] or mask != measured_mask:
            excluded.append({"job_id": job["job_id"], "reason": "source_or_mask_mismatch"})
            continue
        key = (ev["entry"], sha)
        blocks = {(b["index"], tuple(b["interval"])): b for b in ev["blocks"]}
        if method == TABLE:
            endpoints = ev["units"]
        else:
            endpoint = ev["parameters"]["endpoint"]
            endpoints = [endpoint]
            flagged = [b for b in ev["blocks"] if b["classification"] in MISSED]
            signed_jobs.append({"job_id": job["job_id"], "entry": ev["entry"], "input_sha256": sha,
                                "mask": mask, "endpoint": endpoint, "missed_present": ev["missed_present"],
                                "blocks_evaluable": ev["blocks_evaluable"], "flagged_blocks": len(flagged)})
            exceptions.extend((key, endpoint, job, b) for b in flagged)
        for endpoint in endpoints:
            by_material[key, endpoint].append((job, blocks))

    rows = []
    coverage = Counter()
    for key, endpoint, job, block in exceptions:
        source = _readout(job, block, endpoint)
        block_key = (block["index"], tuple(block["interval"]))
        candidates = []
        for other, indexed in by_material[key, endpoint]:
            if other["parameters"]["mask"] == source["mask"]:
                continue
            if block_key not in indexed:
                candidates.append({"job_id": other["job_id"], "method": other["method"],
                                   "mask": other["parameters"]["mask"], "status": "interval_absent"})
                continue
            candidate = _readout(other, indexed[block_key], endpoint)
            candidate["status"] = "measured" if candidate["evaluable"] else "unevaluable"
            if other["method"] == SIGNED:
                changed = [k for k in RULE if candidate["thresholds"][k] != source["thresholds"][k]]
                candidate["changed_rule_fields"] = changed
                candidate["threshold_relation"] = ("different" if any(k != "minimum_evaluable" for k in changed)
                                                   else "same")
            else:
                candidate["threshold_relation"] = "no_signed_rule_measured"
            candidates.append(candidate)
        if any(c["status"] == "measured" for c in candidates):
            status = "measured"
        elif candidates:
            status = "no_evaluable_counterpart"
        else:
            status = "no_counterpart_job"
        coverage[status] += 1
        rows.append({"entry": key[0], "input_sha256": key[1], "index": block["index"],
                     "interval": list(block["interval"]), "endpoint": endpoint, "unit": job["evidence"]["unit"],
                     "status": status, "source": source, "counterparts": candidates})
    return {"signed_jobs": signed_jobs, "excluded_jobs": excluded, "flagged_blocks": len(rows),
            "measured_matches": coverage["measured"], "no_evaluable_counterpart": coverage["no_evaluable_counterpart"],
            "no_counterpart_job": coverage["no_counterpart_job"], "rows": rows}
