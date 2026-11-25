"""Summarize an isolated replay, retaining digests and pointers to the original local observations."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/"gx-h")]
from acceptance.v622.public.enrollment import read,digest


def receipt(out):
    reg=read(out/"run/registration.json")
    trace=read(out/"export.json")
    network=read(out/"network.json")
    if (network["exit_code"]!=0 or network["ipv4_routes"] or set(network["ipv6_route_interfaces"])-{"lo"}
            or network["fixture_namespace"]==network["outer_namespace"] or trace["real_model"]
            or set(trace["cases"])!={"01","02"}):
        raise ValueError("Incomplete isolation or successor-loopback observation")
    files=["network.json","preflight.json","run/registration.json","export.json","status.json",
           "recovery.json","expired.json","controls.log"]
    cases={}
    for n,c in trace["cases"].items():
        replay=[read(out/(n+"-"+verb+".json")) for verb in ("run","resume","reconcile")]
        if not c["mechanical_pass"] or any(v!=replay[0] for v in replay[1:]) or replay[0]["status"]!="CLOSED":
            raise ValueError("Incomplete local effect or changed original Run on cold replay: "+n)
        files.extend(n+"-"+verb+".json" for verb in ("run","resume","reconcile"))
        files.extend("run/"+n+"/"+name for name in ("raw/commands.json","raw/physical-http.jsonl","first-attempt.json",
                      "assignment.json","accounting.json","role-accounting.json","roles.json","summary.json"))
        cost=c["accounting"]
        cases[n]={"run_id":c["run_id"],"arm":next(i for i in reg["slots"] if i["slot"]==n),
                  "reading":c["measurements"],"event_index":c["events"],"submission":c["submission"],
                  "action":c["facts"]["intent"],"effect":c["facts"]["effect"],"delegation":c["facts"]["delegation"],
                  "agreement":c["facts"]["independent_match"],"clock_phase":c["observation"],
                  "elapsed_seconds":c["seconds_delivery_to_effect"],"accounting":cost,
                  "mechanical_pass":True,"human_scientific_review":None}
    recover,expired=read(out/"recovery.json"),read(out/"expired.json")
    log=(out/"controls.log").read_text()
    if recover["status"]!="PASS" or expired["status"]!="PASS" or "Ran 5 tests" not in log or not log.rstrip().endswith("OK"):
        raise ValueError("Missing recovery or negative controls")
    return {"schema":"gx.science.execution.offline-replay/1","series":reg["series"],"candidate":reg["source_commit"],
            "host_binary_sha256":reg["official_binary_sha256"],"effort":reg["effort"],"research_effort":reg["research_effort"],
            "classification":"SCRIPTED_NATIVE_ENGINEERING","paid_model_calls":0,"actual_model_calls":0,"scientific_acceptances":0,
            "command":'bash acceptance/science_execution/rehearse.sh "$OUTPUT" "$CLASH_BINARY"',
            "entrypoint":"acceptance/science_execution/diagnostic.py","cases":cases,
            "process_crash_recovered":recover,"cold_deadline":expired,
            "legacy_control_count":5,"raw_paths_relative_to":"$OUTPUT",
            "sha256":{f:digest(out/f) for f in files}}


if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("output_directory",type=Path)
    print(json.dumps(receipt(p.parse_args().output_directory.resolve()),ensure_ascii=False,sort_keys=True,indent=2))
