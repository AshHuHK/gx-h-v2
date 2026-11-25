"""Rate the registered public slots from original GX states and delivered Codex conversations."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT),str(ROOT/"gx-h")]
from acceptance.v62c.rate import freeze, read, file_hash
from acceptance.v622.public import scoring, witness


def registration(work):
    reg = read(work/"registration.json")
    protocol = read(HERE/"protocol.json")
    scoring.need(reg["provider"]=="gx_loopback" and reg["scope"] in ("FORMAL_OFFLINE","SCRIPTED_REHEARSAL"),
                 "Scripted ratings require the original offline enrollment")
    scoring.need(reg["protocol_sha256"]==file_hash(HERE/"protocol.json"),"Frozen protocol changed")
    for file in ("rate.py","scoring.py","witness.py"):
        scoring.need(reg["source_pins"][file]==file_hash(HERE/file),f"Frozen scorer differs at {file}")
    if reg["series"]==protocol["series"]:
        slots = protocol["slots"]
    else:
        scoring.need(reg["series"]==protocol["controls_series"], "Unknown public series")
        slots = protocol["controls"]
    fields = ("slot","code","architecture")
    scoring.need([tuple(s[k] for k in fields) for s in slots]==[tuple(s[k] for k in fields) for s in reg["slots"]],
                 "The slot denominator, order or allocation changed")
    return reg,protocol


def assess_slot(work, number):
    """Seal this original slot's reader lock and outcome before admitting the next one."""
    number = str(number).zfill(2)
    return run(work, Path(work)/number/"assessment", only=number)["cases"][number]


def run(work, output=None, stage="both", *, only=None):
    work = Path(work).resolve()
    dest = Path(output).resolve() if output else work/"ratings"
    reg, protocol = registration(work)
    cases = {}
    enrolled = []
    slots = [s for s in reg["slots"] if only is None or s["slot"]==only]
    scoring.need(slots, "Unknown registered slot")
    for item in slots:
        num = item["slot"]
        slot = work/num
        summary = read(slot/"summary.json") if (slot/"summary.json").exists() else {}
        assignment = read(slot/"assignment.json") if (slot/"assignment.json").exists() else {}
        if not summary or not assignment or not summary.get("assignment") or not summary.get("run_id"):
            status = summary.get("status") or ("INCOMPLETE" if (slot/"state/gx.db").exists() else "NOT_STARTED")
            cases[num] = {"status":status,"architecture":item["architecture"],"run_id":summary.get("run_id"),
                          "opportunity":"NO_CONFIRMED_DELIVERY","science_pass":False,"action_realized":False,
                          "delegation":"UNASSESSED","provider_tokens":None,"provider_usd":None,
                          "unsupported_support":False,"independent_quantity_mismatch":False,
                          "role_usage":summary.get("role_usage"),"wall_seconds":summary.get("wall_seconds"),
                          "accounting_by_scope":summary.get("accounting_by_scope"),"not_scored_reason":
                          "No registered assignment and accepted initial plan confirmed" if not assignment else "No completed slot summary"}
            continue
        scoring.need(assignment["run_id"]==summary["run_id"] and summary["classification"]==reg["scope"] and
                     summary["paid_model_calls"]==0,"Run or host classification differs from enrollment")
        enrolled.append((item,slot,assignment,summary))
        if stage in ("both","opportunity"):
            evidence = witness.extract(slot,summary["run_id"],protocol["assay"],assignment,outcomes=False)
            evidence["provisional_method_family"]=protocol["provisional_method_family"]
            first, lock = scoring.opportunity(evidence)
            freeze(dest/num/"opportunity-packet.json",first)
            freeze(dest/num/"opportunity-lock.json",lock)
    if stage=="opportunity":
        return None
    # Inspect the outcome key and later actions only after every enrolled reader packet is locked.
    for item, _, _, _ in enrolled:
        scoring.need((dest/item["slot"]/"opportunity-lock.json").is_file(), "Lock every enrolled opportunity first")
    key = work/"custody/oracle.json"
    scoring.need(file_hash(key)==reg["endpoints_sha256"],"Expected source endpoint commitment differs")
    registered_counts = read(key)
    for item,slot,assignment,summary in enrolled:
        num = item["slot"]
        trace = witness.extract(slot,summary["run_id"],protocol["assay"],assignment,outcomes=True)
        trace["provisional_method_family"]=protocol["provisional_method_family"]
        scoring.need(trace["actor_class"]=="SCRIPTED_REHEARSAL" and trace["evidence_policy"]==protocol["evidence_policy"],
                     "Official session provider or Run evidence policy differs")
        scoring.need(trace["offered"]==(item["architecture"]=="delegation_available"),"Wrong delegation allocation")
        independent = trace["independent_count"]
        canonical = registered_counts[item["code"]]
        scoring.need(independent["sha256"]==protocol["materials"][item["code"]]["sha256"] and
                     all(canonical[k]==independent[k] for k in ("selected","observed","sha256","mask")),
                     "Independent reparse of the actual assigned bytes differs from the preregistered material")
        lock = read(dest/num/"opportunity-lock.json")
        first, frozen = scoring.opportunity(trace)
        scoring.need(first==read(dest/num/"opportunity-packet.json") and frozen==lock,
                     "First-stage reader packet or lock differs")
        packet, review, score = scoring.rate(trace,lock,protocol)
        scoring.need(review["expected"]==canonical["expected"],"Independent threshold differs from the committed result")
        score.update(status="SCORED", run_status=summary["status"], architecture=item["architecture"],
                     material_sha256=independent["sha256"],wall_seconds=summary["wall_seconds"],
                     role_usage=summary["role_usage"], accounting_by_scope=summary.get("accounting_by_scope",{}))
        cases[num]=score
        for kind,value in (("causal-trace",trace),("outcome-packet",packet),("review",review),("score",score)):
            freeze(dest/num/(kind+".json"),value)
    batch = {"schema":scoring.SCHEMA, "series":reg["series"], "classification":reg["scope"],
             "registered_slots":len(reg["slots"]),"assessed_slots":len(slots),"registration_sha256":file_hash(work/"registration.json"),"cases":cases,
             "scripted_science_passes":sum(c["science_pass"] for c in cases.values()),
             "scripted_delegation_demonstrated":sum(c["delegation"]=="DEMONSTRATED" for c in cases.values()),
             "paid_model_calls":0,"live_model_decisions":0,"private_earned_slots":0,
             "provider_tokens":None,"provider_usd":None}
    freeze(dest/"scores.json",batch)
    if output is None:
        freeze(work/"scores.json",batch)
    return batch


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--work",type=Path,required=True)
    p.add_argument("--output",type=Path)
    p.add_argument("--stage", choices=("opportunity","outcome","both"),default="both")
    args = p.parse_args()
    batch = run(args.work,args.output,args.stage)
    if batch:
        print(json.dumps({k:batch[k] for k in ("registered_slots","scripted_science_passes","scripted_delegation_demonstrated","paid_model_calls")}))


if __name__=="__main__":
    main()
