"""Check post-run engineering controls against their original states and independently locked ratings."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v62c.parity import check_slot, fingerprint
from acceptance.v62c.rate import freeze
from acceptance.v622.public.enrollment import read
from acceptance.v622.public.scripted import verify_standard


def need(condition, message):
    if not condition:
        raise ValueError(message)


def controls(work, cases):
    checks = {}
    for number, role in zip(("01","02","03","04"), ("B1","B2","B3","F"), strict=True):
        slot = work / number
        assigned = read(slot / "assignment.json")
        score = cases[number]
        with closing(sqlite3.connect((slot / "state/gx.db").as_uri()+"?mode=ro", uri=True)) as db:
            def event(kind, subject=None):
                return db.execute("""SELECT event_id,payload_json FROM events WHERE run_id=? AND kind=?
                                  AND (? IS NULL OR subject_id=?) ORDER BY event_id""",
                                  (score["run_id"],kind,subject,subject)).fetchall()
            openings = event("v62.module.opened")
            action = event("v6.decision.committed", score["decision_ref"])
            need(len(action)==1, f"{role} has no accepted post-measurement decision")
            after = [eid for eid, _ in event("job.created") if eid > action[0][0]]
        details = {"run_id":score["run_id"],"assignment_event_id":assigned["event_id"],
                   "module_open_event_ids":[eid for eid,_ in openings], "action_event_id":action[0][0],
                   "post_action_jobs":after, "actual_effect":score["enactment_receipt"],
                   "scientific_judgment":score["scientific_judgment"],"science_pass":score["science_pass"],
                   "opportunity":score["opportunity"],"decision":score["decision"],"delegation":score["delegation"]}
        need(score["opportunity"]=="DELIVERED" and score["closed"], f"{role} did not receive and conclude")
        if role=="B1":
            need(score["science_pass"] and score["delegation_offered"] and not openings and score["delegation"]=="NOT_USED",
                 "Direct use did not retain its original delegation-available allocation")
        elif role=="B2":
            late = [(eid,json.loads(payload)) for eid,payload in openings if eid > assigned["event_id"]]
            need(not assigned["module_instance_id"] and len(late)==len(openings)==1 and
                 assigned["artifact_ref"] in late[0][1]["effective_grant"]["input_refs"] and
                 score["science_pass"] and score["delegation"]=="DEMONSTRATED", "Late module grant or chain missing")
            details["module_open_event_id"] = late[0][0]
            details["module_instance_id"] = late[0][1]["module_instance_id"]
        elif role=="B3":
            need(score["science_pass"] and score["decision"]=="CHANGE" and not after and
                 details["actual_effect"]["kind"]=="closed_run" and details["actual_effect"]["unresolved"],
                 "Evidence-backed early termination did not take effect")
        else:
            need(not score["science_pass"] and score["scientific_judgment"]=="CHANGE" and score["decision"]=="MAINTAIN"
                 and details["actual_effect"]["kind"] in ("completed_job","maintained_job"),
                 "Self-declared CHANGE on the original enacted comparison received credit")
        checks[role] = details
    return {"formal_controls":checks}


def verify(work):
    reg = read(work/"registration.json")
    result = read(work/"result.json")
    scores = read(work/"scores.json")
    need(len(reg["slots"])==len(result["cases"])==4 and result["classification"]==reg["scope"],"Unfinished batch")
    slots = {}
    for item in reg["slots"]:
        number = item["slot"]
        need(result["cases"][number]["status"]=="CLOSED","An original enrolled case did not close")
        slot = work / number
        arm = check_slot(slot)
        wire = read(slot/"raw/wire.json")
        for message in wire:
            need(not re.search(r"\b(?:P4|K7|B[123])\b|oracle\.json|GX-V62C-PUBLIC-COVERAGE|scores\.json",
                               message["brief_text"]), "An evaluation condition leaked into a native brief")
        need(arm["source_sha256"]==read(work/"custody"/"oracle.json")[item["code"]]["sha256"],
             "Assigned original differs from the registered material")
        slots[number] = arm
    need(len({fingerprint(s["common"]) for s in slots.values()})==1,
         "Actual tools, method card, study card, Goal or native model configuration differed between arms")
    grants = reg["grants"]
    need({k:v for k,v in grants["single"].items() if k!="research_modules"}==
         {k:v for k,v in grants["delegation_available"].items() if k!="research_modules"},
         "Single and delegation-available grants differ outside module availability")
    parity = {"slots":slots,"schema":"gx.v622.arm-parity/1","classification":reg["scope"],
              "common_sha256":fingerprint(next(iter(slots.values()))["common"]),"status":"PASS"}
    if reg["suite"] == "standard":
        pairs = {}
        for code in ("P4","K7"):
            arms = {i["architecture"]:slots[i["slot"]] for i in reg["slots"] if i["code"]==code}
            single, delegated = arms["single"], arms["delegation_available"]
            need(single["source_sha256"]==delegated["source_sha256"],"Material differs between matched arms")
            pairs[code] = {k:{"delta_delegated_minus_single":delegated[k]-single[k],
                              "ratio_delegated_over_single":delegated[k]/single[k] if single[k] else None}
                           for k in ("native_tool_calls","wall_seconds","gx_recorded_cpu_seconds")}
        parity["matched_pairs"] = pairs
        cases = verify_standard(work)
    else:
        cases = controls(work,scores["cases"])
    voucher = {"status":"PASS","suite":reg["suite"],"series":reg["series"],"classification":reg["scope"],
               "original_run_ids":{k:v["run_id"] for k,v in slots.items()},"paid_model_calls":0,
               "provider_usd":None,"live_model_decisions":0, **cases}
    freeze(work/"capability-parity.json", parity)
    freeze(work/"verification.json", voucher)
    return voucher


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    print(json.dumps(verify(parser.parse_args().work.resolve()), indent=2, ensure_ascii=False))
