"""Describe both uninstrumented quiet control blocks and their combined common-submit tails."""
import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path

from audit_workload import quantile
from run_workload import save, sha, utc


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups["all"].append(row)
        groups[row["role"] + "/" + row["product_outcome"]].append(row)
    return {key: {metric: quantile(r[metric] for r in group) for metric in
        ("callback_ms", "writer_ms", "writer_lock_ms", "outside_writer_ms", "canonical_argument_bytes")}
        for key, group in sorted(groups.items())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", required=True, type=Path)
    parser.add_argument("--history", required=True, type=Path)
    args = parser.parse_args()
    studies = {}
    pooled = defaultdict(list)
    shape = None
    for folder, quiet_key in ((args.direct, "candidate"), (args.history, "candidate_quiet")):
        valid = json.loads((folder / "validity.json").read_bytes())
        sample = folder / "callback-samples.jsonl.gz"
        if valid["result"] != "PASS" or valid["sources_sha256"][sample.name] != sha(sample):
            raise ValueError("Control verification or raw callback digest differs")
        selected = {"baseline": "baseline", quiet_key: "corrected_quiet"}
        by_arm, by_trial = defaultdict(list), defaultdict(Counter)
        with gzip.open(sample, "rt") as stream:
            for source in map(json.loads, stream):
                if source["arm"] not in selected or source["tool"] != "gx_submit_product":
                    continue
                row = {**source, "study": folder.name}
                arm = selected[row["arm"]]
                by_arm[arm].append(row)
                pooled[arm].append(row)
                by_trial[row["trial"]][row["lane"], row["role"], row["product_outcome"],
                    row["canonical_argument_bytes"]] += 1
        for trial, counts in by_trial.items():
            if sum(counts.values()) != 6:
                raise ValueError("Incomplete fixed scientific products in " + trial)
            if shape is None:
                shape = counts
            if counts != shape:
                raise ValueError("Scientific role, outcome or complete request size differs in " + trial)
        studies[folder.name] = {"sample_sha256": sha(sample), "matched_quiet_workspaces": len(by_trial),
            "groups": {k: summarize(v) for k, v in by_arm.items()}}
    if any(len(rows) != 108 for rows in pooled.values()):
        raise ValueError("Expected 108 complete fixed products per revision")
    tails = {}
    fields = ("study", "trial", "arm", "lane", "role", "product_outcome", "call_id", "callback_ms",
        "writer_ms", "outside_writer_ms", "writer_count", "writer_lock_ms", "canonical_argument_bytes")
    for arm, rows in pooled.items():
        tails[arm] = [{k: row[k] for k in fields} for row in sorted(rows, key=lambda r: r["callback_ms"], reverse=True)[:6]]
    save(args.history / "quiet-replications.json", {"captured": utc(), "result": "PASS",
        "scope": "Exploratory pooled summary after the independently reported 72- and 36-product samples. The scientific fixture, emitted actions and six lane-specific complete product sizes match in every selected workspace. The second control additionally computed a portable hash at the Responses peer outside the GX callback. The paid gate result and its bounds remain recorded independently.",
        "scientific_shape": [{"lane": k[0], "role": k[1], "verdict": k[2], "argument_bytes": k[3],
            "count_per_workspace": n} for k, n in sorted(shape.items())],
        "studies": studies, "combined": {k: summarize(v) for k, v in pooled.items()}, "largest_observed": tails})


if __name__ == "__main__":
    main()
