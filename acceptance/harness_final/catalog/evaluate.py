"""Evaluate top-five receipts from an ordinary GX Goal and its native catalog tool."""
import argparse
import hashlib
import json
from pathlib import Path
import sys


def unrestricted_opening(study, lead):
    """The public fixture Goal has no PI method constraint. Old frozen contracts omit the field."""
    if study.brief(lead).get("pi_method_scope", {}).get("status") == "pending":
        return {"pi_method_scope":[]}
    return {}


def evaluate(repo, cases, *, kind="all"):
    # One invocation targets one release and thus imports only that release's CLI and fixtures.
    repo = repo.resolve()
    sys.path[:0] = [str(repo), str(repo/"gx-h"), str(repo/"gx-h/tests"), str(repo/"acceptance/v62a")]
    from acceptance.harness_refactor.catalog.test_open_discovery import OpenDiscovery
    control = OpenDiscovery()
    results = []
    try:
        study, lead = control.admit("Which public methods fit the supplied records?")
        first = unrestricted_opening(study, lead)
        for position, case in enumerate(cases):
            result = study.call(lead, "gx_query_catalog", {"query":case["query"], "kind":kind, "limit":5,
                               **(first if position == 0 else {})}, case["id"])
            hits = []
            for item in result["items"]:
                facts = item.get("capability_facts", {})
                hits.append({"reference":item["reference"], "provider":item.get("provider"), "status":item["status"],
                             "kind":item["kind"], "facts":facts, "reason":item["match_reason"]})
            relevant = set(case["relevant"])
            eligible = {h["reference"] for h in hits if h["kind"] == "method" and
                        h["status"] in {"available", "selectable"} and h["facts"].get("installed")
                        and h["facts"].get("dependency_ready") is True}
            results.append({"id":case["id"], "query":case["query"], "relevant":sorted(relevant),
                            "top5":hits, "total":result["total"], "ready_relevant":sorted(relevant & eligible),
                            "only_reference":bool(hits) and all(h["status"]=="reference_only" for h in hits),
                            "zero_matches":not result["total"]})
        positives = [c for c in results if c["relevant"]]
        return {"run":study.run_id,"cases":results,"metrics":{
            "eligible_positive_queries":len(positives),
            "queries_with_ready_relevant_at_5":sum(bool(c["ready_relevant"]) for c in positives),
            "relevant_methods":sum(len(c["relevant"]) for c in positives),
            "ready_relevant_methods_at_5":sum(len(c["ready_relevant"]) for c in positives),
            "only_reference":sum(c["only_reference"] for c in results),
            "zero_matches":sum(c["zero_matches"] for c in results)}}
    finally:
        control.doCleanups()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo",type=Path,required=True)
    parser.add_argument("--corpus",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args = parser.parse_args()
    raw = args.corpus.read_bytes()
    result=evaluate(args.repo,json.loads(raw))
    result["corpus_sha256"]=hashlib.sha256(raw).hexdigest()
    args.output.write_text(json.dumps(result,indent=2,ensure_ascii=False)+"\n")
    print(json.dumps(result["metrics"]))
