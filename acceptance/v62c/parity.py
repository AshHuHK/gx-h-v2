"""Verify initial Lead parity and summarize actual paired engineering costs."""
from collections import Counter
from contextlib import closing
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3


def read(path):
    return json.loads(path.read_bytes())


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(",", ":")).encode()).hexdigest()


def check_slot(slot):
    summary=read(slot/"summary.json")
    first=summary["initial_brief"]
    wire=read(slot/"raw/wire.json")
    caps=first["capabilities"]
    calls=Counter(r["tool"] for r in wire)
    role_cost=summary["role_usage"]
    granted_tools=sorted(set(summary["initial_tools"])-{"gx_open_module","gx_cancel_module"})
    assert first["research_effort"]=="deep" and first["branch_delegation"] is False
    assert len(caps)==1 and caps[0]["capability_version"]=="deposited-chain-geometry@2"
    assert "gx_delegate_decision" not in granted_tools
    assert all(c["model"]=="gpt-6.2-astra" and c["effort"]=="max" for c in wire)
    with closing(sqlite3.connect((slot/"state/gx.db").as_uri()+"?mode=ro",uri=True)) as db:
        def result(call):
            row=db.execute("SELECT result_json FROM p0_root_calls WHERE call_id=?",(call["call_id"],)).fetchone()
            assert row
            return json.loads(row[0])
        catalog=result(wire[0])
        assert wire[0]["args"]=={"kind":"method","reference":caps[0]["capability_version"]} and catalog["execution_granted"]
        card_ref=catalog["source_ref"]
        workflow=next(m for m in first["materials"] if m["role"]=="workflow_spec")
        def actual_read(ref):
            first_read=next(c for c in wire if c["brief"]["scope"]=="run" and c["tool"]=="gx_read_artifact" and c["args"]["ref"]==ref)
            return fingerprint(result(first_read))
        baseline={"research_effort":first["research_effort"],"branch_delegation":first["branch_delegation"],
                  "capabilities":fingerprint(caps),"method_card_sha256":actual_read(card_ref),
                  "study_card_sha256":actual_read(workflow["artifact_ref"]),"workflow_input_sha256":workflow["sha256"],
                  "source_tools":first["source_tools"],"tools":granted_tools,
                  "model":wire[0]["model"],"model_effort":wire[0]["effort"],"goal_sha256":fingerprint(first["goal"])}
        # Some experimental flags live in the granted graph rules rather than the menu.
        assert bool(first["module_catalog"])==(summary["architecture"]=="delegation_available")
    return {"run_id":summary["run_id"],"architecture":summary["architecture"],"common":baseline,
            "source_sha256":summary["assignment"]["sha256"],"job_count":len(summary["jobs"]),
            "role_usage":role_cost,"native_tool_attempts_by_name":dict(calls),"native_tool_calls":sum(calls.values()),
            "wall_seconds":summary["wall_seconds"],"gx_recorded_cpu_seconds":summary["resources"]["lines"]["cpu_seconds"]["booked"],
            "live_provider_tokens":None,"live_provider_usd":None}


def audit(directory):
    reg=read(directory/"registration.json")
    slots={s["slot"]:check_slot(directory/s["slot"]) for s in reg["slots"]}
    assert len(slots)==4 and len({fingerprint(c["common"]) for c in slots.values()})==1
    pairs={}
    for code in ("P4","K7"):
        single,multi=(next(slots[s["slot"]] for s in reg["slots"] if s["code"]==code and s["architecture"]==a)
                      for a in ("single","delegation_available"))
        assert single["source_sha256"]==multi["source_sha256"] and single["job_count"]==multi["job_count"]==2
        pairs[code]={key:{"delta_delegated_minus_single":multi[key]-single[key],
                          "ratio_delegated_over_single":round(multi[key]/single[key],3) if single[key] else None}
                     for key in ("native_tool_calls","wall_seconds","gx_recorded_cpu_seconds")}
        pairs[code]["cost_ratio_paid"]=None
    return {"schema":"gx.v62c.arm-parity/1","classification":"SCRIPTED_REHEARSAL","status":"PASS",
            "slots":slots,"matched_pairs":pairs,"live_cost_observation":"No paid calls or paid cost comparison."}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,required=True)
    path=parser.parse_args().work.resolve()
    with (path/"capability-parity.json").open("x") as out:
        json.dump(audit(path),out,indent=2,sort_keys=True)
        out.write("\n")
