"""Compare identical submissions within each interleaved three-condition grid-history block."""
import argparse
from collections import Counter, defaultdict
import gzip
import json
from pathlib import Path

from audit_workload import quantile
from run_workload import save, sha, utc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", required=True, type=Path)
    args = parser.parse_args()
    folder = args.evidence
    validation = json.loads((folder / "validity.json").read_bytes())
    if validation["result"] != "PASS":
        raise ValueError("The scientific body, exact sequence and timing checks must pass")
    control = json.loads((folder / "control.json").read_bytes())
    sequence = {Path(r["private_trial"]).name: r["sequence"] for r in control["trials"]}
    schedule = control["order"]
    conditions = ("baseline", "candidate_quiet", "candidate")
    blocks = [schedule[i:i + 3] for i in range(0, len(schedule), 3)]
    if len(blocks) != 6 or any(set(block) != set(conditions) for block in blocks):
        raise ValueError("Each block must include all three conditions")
    submissions = defaultdict(dict)
    callbacks = defaultdict(list)
    grid = Counter()
    with gzip.open(folder / "callback-samples.jsonl.gz", "rt") as stream:
        for row in map(json.loads, stream):
            arm = row["arm"]
            if row["tool"] == "gx_grid_measurement":
                grid[arm, row["official_success"], row["writer_count"]] += 1
                continue
            if row["tool"] != "gx_submit_product":
                continue
            block = sequence[row["trial"]] // 3
            key = (block, row["lane"], row["role"], row["attempt"], row["product_outcome"])
            if arm in submissions[key]:
                raise ValueError("Duplicate submission in a matched block")
            submissions[key][arm] = row
            callbacks[arm, row["lane"], row["role"], row["product_outcome"]].append(row)
    if len(submissions) != 36 or any(set(r) != set(conditions) for r in submissions.values()):
        raise ValueError("Expected six identical matches in each of six blocks")
    measures = ("callback_ms", "writer_ms", "writer_lock_ms", "outside_writer_ms")
    differences = defaultdict(list)
    pairs = []
    for key, rows in sorted(submissions.items()):
        block, lane, role, attempt, verdict = key
        observation = {"block": block, "lane": lane, "role": role, "attempt": attempt, "verdict": verdict,
            "sequence_in_block": blocks[block], "conditions": {}}
        for arm, row in rows.items():
            observation["conditions"][arm] = {k: row[k] for k in
                ("trial", "call_id", "writer_count", "canonical_argument_bytes", *measures)}
        for first, last in (("baseline", "candidate_quiet"), ("candidate_quiet", "candidate"), ("baseline", "candidate")):
            for measure in measures:
                delta = rows[last][measure] - rows[first][measure]
                for group in (("all", "all"), (role, verdict), (lane + "/" + role, verdict)):
                    differences[first, last, *group, measure].append(delta)
        pairs.append(observation)
    result = {"captured": utc(), "result": "PASS", "blocks": blocks,
        "method": "Adjacent three-workspace blocks use all condition orders. Match by lane, scientific role, Attempt and verdict; validate all six complete product hashes independently. Signed differences are end arm minus start arm, from the same matched block.",
        "sample_sha256": sha(folder / "callback-samples.jsonl.gz"), "validity_sha256": sha(folder / "validity.json"),
        "role_and_lane_ms": [{"condition": arm, "lane": lane, "role": role, "verdict": verdict,
            **{metric: quantile(r[metric] for r in values) for metric in measures}}
            for (arm, lane, role, verdict), values in sorted(callbacks.items())],
        "paired_differences_ms": [{"from": start, "to": end, "group": group, "verdict": verdict,
            "measure": measure, "difference": quantile(values)}
            for (start, end, group, verdict, measure), values in sorted(differences.items())],
        "measurement_writer_counts": [{"condition": arm, "success": ok, "outer_writers": writers, "calls": n}
            for (arm, ok, writers), n in sorted(grid.items())], "matches": pairs}
    save(folder / "effect.json", result)
    print(f"Verified {len(submissions)} three-condition submissions in {len(blocks)} blocks")


if __name__ == "__main__":
    main()
