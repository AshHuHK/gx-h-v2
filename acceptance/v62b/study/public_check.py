"""Replay the public 2RE9/2O0O scripted pair through the same receipt and scoring code."""
import argparse
from contextlib import closing
import json
from pathlib import Path

from receipts import artifact, database, digest, extract, need, parse
from score import adjudicate, brief_packet, key, lead_turns, lock_opportunities


def run(work, expected):
    with closing(database(work)) as db:
        rid, = db.execute("SELECT run_id FROM v6_goals").fetchone()
        evidence = extract(work, rid)
        need(evidence["turns"] and all(t["native"]["model_provider"] == "gx_loopback" for t in evidence["turns"]),
             "This replay accepts only the public scripted provider")
        turns = lead_turns(evidence)
        arrival = next(t for t in turns if t["reports"])
        report, = arrival["reports"]
        finding, = report["delivered"]["findings"]
        source, = finding["source_refs"]
        value = parse(artifact(db, work, rid, source)[1])
        condition = "change" if 5 * value["selected_residues"] < 3 * value["observed_union_residues"] else "maintain"
        need(value["entry"] == "2RE9" and condition == expected, "Public pilot measurement differs from its registered slot")
        choice = "common_ca" if condition == "change" else "common_backbone"
        effect, = arrival["decision"]["new_jobs"]
        job, = [j for j in evidence["jobs"] if j["node_id"] == effect]
        completed = parse(artifact(db, work, rid, job["result_ref"])[1])
        need(completed["entry"] == "2O0O" and completed["mask"] == job["spec"]["parameters"]["mask"] == choice,
             "The witnessed follow-up Job did not implement the chosen route")
    marks, judgments = [], []
    for t in turns:
        mark = {"window":key(t), "report_ref":None, "reason":"Opening or repeated information."}
        judged = {"window":key(t), "disposition":"NO_OPPORTUNITY", "unsupported_support":False,
                  "reason":"Scripted public scoring fixture."}
        if t is arrival:
            mark.update(report_ref=report["ref"], quote=finding["text"], reason="First delivered sourced coverage count.")
            judged.update(disposition=condition.upper(), rationale_excerpt=condition.title() + " the provisional 2O0O",
                          scope_appropriate=True, accepted_action=choice,
                          prior_plan_excerpt="The provisional follow-up is common_backbone on 2O0O.")
        marks.append(mark)
        judgments.append(judged)
    sealed = lock_opportunities(evidence, {"packet_sha256":digest(brief_packet(evidence)), "reviewer":"public-fixture",
                                "windows":marks}, "public-fixture")
    score = adjudicate(evidence, sealed, {"dossier_sha256":digest(evidence), "reviewer":"public-fixture", "windows":judgments},
                       {"condition":condition, "acceptable_actions":[choice]}, "public-fixture", digest(evidence))
    need(score["passed"] and score["opportunities"] == 1 and not evidence["missing"], "Public decision linkage failed")
    return dict(run_id=rid, provider="gx_loopback", source_class="SCRIPTED_PUBLIC_ENGINEERING", condition=condition,
                delivered_brief_sha256=arrival["brief_sha256"], rollout_sha256=arrival["rollout_sha256"],
                published_report_sha256=report["sha256"], published_at=report["published_at"], delivered_at=arrival["delivered_at"],
                selected=value["selected_residues"], observed=value["observed_union_residues"], choice=choice, job=job["job_id"],
                result_sha256=job["result_sha256"], first_opportunity=score["first_opportunity"], passed=score["passed"],
                strict_cited_work_change=score["strict_cited_work_change"], live_decisions_in_denominator=0)


if __name__ == "__main__":
    cmd = argparse.ArgumentParser(description=__doc__)
    cmd.add_argument("--work", type=Path, required=True, help="Public engineering runner's pair directory")
    args = cmd.parse_args()
    print(json.dumps({s:run(args.work / s, s) for s in ("change", "maintain")}, indent=2))
