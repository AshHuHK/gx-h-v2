"""Check the preregistered local investigators after all four original runs are closed."""
from contextlib import closing
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT),str(ROOT/"gx-h")]
from acceptance.v62c import rehearse
from acceptance.v623.live import adjudication
from acceptance.v623.live.registration import P,read,sha


def need(ok,message):
    if not ok: raise ValueError(message)


def verify(work):
    reg=read(work/"registration.json")
    result=read(work/"result.json")
    f=read(work/"funding.json")
    need(reg["series"]==P["rehearsal_series"] and reg["slots"]==P["slots"] and reg["host"]=="offline","Wrong public series")
    cases={}
    available=set()
    for item in reg["slots"]:
        number=item["slot"];slot=work/number
        need(result["cases"][number]["status"]=="CLOSED","An original rehearsal Run did not close")
        summary=read(slot/"summary.json"); funding=read(slot/"accounting.json")
        score=adjudication.score(work,number)
        need(adjudication.ready_for_next(work,number) and score["opportunity"]=="DELIVERED" and
             score["science_pass"] is None,"A paid/reviewed science outcome or missing original receipt was reported")
        need(score["intent"]==score["enacted"] and score["enacted"] in ("CHANGE","MAINTAIN","STOP"),
             "The scripted investigator's declared action was not observed")
        first=read(slot/"raw/wire.json")[0]
        brief=first["brief"]
        authorized=sorted(set(first["tools"])-{"gx_open_module","gx_cancel_module"})
        available.add(sha([authorized,brief["research_effort"],brief["capabilities"],brief["source_tools"],
                           [(m["role"],m["sha256"]) for m in brief["materials"]],first["model"],first["effort"]]))
        need(summary["assignment"]["sha256"]==P["materials"][item["code"]]["sha256"],"Wrong assigned source")
        rows=read(slot/"native-host.json")["roots"]
        with closing(rehearse.db_open(slot)) as db:
            openings=[r[0] for r in db.execute("SELECT event_id FROM events WHERE run_id=? AND kind='v62.module.opened'",
                                                (summary["run_id"],))]
        events=sum(len(r["turns"]) for r in rows)
        n_requests=len(funding["requests"])
        need(events and n_requests and not funding["unmatched_turns"],"Missing session-to-physical reconciliation")
        if number=="02":
            need(openings and min(openings)>summary["assignment"]["event_id"],"Expected the scripted late commission")
        elif number=="04":
            need(not openings and brief["module_catalog"],"Expected the scripted direct choice with delegation offered")
        cases[number]={"architecture":item["architecture"],"run_id":summary["run_id"],"job_count":len(summary["jobs"]),
                       "request_count":n_requests,"native_turns":events,"opportunity":score["opportunity"],
                       "intent":score["intent"],"enacted":score["enacted"],"delegation":score["delegation"],
                       "stop_reason":score["stop_reason"],"cold_sha256":summary["cold_state_sha256"],"science_pass":None}
    need(len(available)==1,"Actual initial data/method/tool access differed beyond delegation")
    need(not f["batch"]["unresolved_requests"] and not f["batch"]["breaches"],"Missing final physical usage")
    return {"status":"PASS","series":reg["series"],"source_commit":reg["source_commit"],"classification":reg["scope"],
            "protocol_sha256":reg["protocol_sha256"],"original_runs":cases,"paid_inference_requests":0,
            "actual_live_slots_started":0,"live_scientific_decisions":0,"independent_science_grades":0,
            "available_capability_sha256":available.pop(),"physical_totals":f["batch"]}


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("--work",type=Path,required=True)
    print(json.dumps(verify(p.parse_args().work),ensure_ascii=False,indent=2))
