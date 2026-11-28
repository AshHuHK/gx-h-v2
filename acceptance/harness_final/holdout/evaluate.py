"""Score one ordinary catalog lookup per frozen research question."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ITEM_FIELDS = ("reference", "provider", "kind", "status", "capability_facts",
               "installation_status", "execution_granted")


def score(case, result, statuses):
    top = [{k: v for k, v in item.items() if k in ITEM_FIELDS} for item in result["items"]]
    relevant = [item for item in top if item["reference"] in case["relevant"]]
    if case["kind"] == "method":
        passed = any(item.get("capability_facts", {}).get("installed")
                     and item["status"] in statuses for item in relevant)
    elif case["kind"] == "zero":
        passed = not top and result["total"] == 0
    elif case["kind"] == "reference_only":
        passed = any(item["status"] == "reference_only" and not item.get("execution_granted")
                     for item in relevant)
    else:
        raise ValueError(f"Unknown case type {case['kind']}.")
    return {
        "case": case["id"], "lang": case["lang"], "kind": case["kind"],
        "total": result["total"], "top5": top, "positive": passed,
        "only_reference_hits": bool(top) and all(item["status"] == "reference_only" for item in top),
    }


def evaluate(site, frozen):
    spec = json.loads(frozen)
    os.environ["GX_CODEX_OFFLINE"] = "1"
    # Import the real CLI and callback dispatch functions from the site being examined.
    sys.path[:0] = [str(site), str(site / "gx-h"), str(site / "gx-h/tests"),
                    str(site / "acceptance/v62a")]
    from acceptance.harness_refactor.catalog.test_open_discovery import OpenDiscovery

    control = OpenDiscovery()
    rows = []
    try:
        study, lead = control.admit(spec["ordinary_goal"])
        pending_scope = study.brief(lead).get("pi_method_scope", {}).get("status") == "pending"
        for index, case in enumerate(spec["cases"]):
            request = {"kind": spec["query_kind"], "query": case["query"], "limit": spec["top"]}
            if pending_scope and index == 0:
                # The unchanged frozen Goal contains no restrictions on research methods.
                request["pi_method_scope"] = []
            result = study.call(lead, "gx_query_catalog", request, "holdout-" + case["id"])
            rows.append(score(case, result, spec["eligible_execution_status"]))
    finally:
        control.doCleanups()

    languages = {}
    pairs = {}
    for row in rows:
        if row["kind"] != "method":
            continue
        counts = languages.setdefault(row["lang"], {"top5": 0, "eligible": 0})
        counts["eligible"] += 1
        counts["top5"] += int(row["positive"])
        pair_id, _ = row["case"].rsplit("-", 1)
        pairs.setdefault(pair_id, {})[row["lang"]] = row["positive"]
    return {
        "sha256": hashlib.sha256(frozen).hexdigest(),
        "commit": subprocess.check_output(["git", "-C", str(site), "rev-parse", "HEAD"], text=True).strip(),
        "run_id": study.run_id,
        "class": "SCRIPTED_CALLBACK",
        "source": "bin/gx-p0 research CLI handler + native gx_query_catalog with the authorized Lead identity",
        "by_language": languages,
        "successful_pairs": sum(bool(p.get("zh") and p.get("en")) for p in pairs.values()),
        "reference_only_pass": next(r["positive"] for r in rows if r["kind"] == "reference_only"),
        "zero_pass": next(r["positive"] for r in rows if r["kind"] == "zero"),
        "rows": rows,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path)
    parser.add_argument("frozen", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = evaluate(args.site.resolve(), args.frozen.read_bytes())
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({k: output[k] for k in (
        "by_language", "successful_pairs", "reference_only_pass", "zero_pass"
    )}))
