"""Join independent clocks, physical Job manifests and delivered native calls on their actual identities."""
import json
from contextlib import closing
from pathlib import Path
from acceptance.v62b.study import receipts as native
from acceptance.v62c import rehearse
from acceptance.v622.public import funding
from acceptance.v622.public.enrollment import digest, read
from gx_core.p0 import manifest


def collect(work,rid,seal,ledger,wire,cold_hash):
    from .operate import ACTOR,decisions
    slot=work/"lead"
    openings=[read(work/"custody"/f"opening-{n}.json") for n in (1,2)]
    cells={i["sha256"]:dict(entry=i["entry"],input_sha256=i["sha256"],opening=o, jobs=[])
           for i,o in zip(seal["inputs"],openings,strict=True)}
    transcripts=native.native_session(slot/"codex/sessions")
    emitted={w["call_id"]:w for w in wire}
    records=[]
    with closing(rehearse.db_open(slot)) as db:
        dd=decisions(db,rid)
        for d in dd:
            call=emitted[d["call_id"]]
            session,context,stamp,calls,_=native.delivered(transcripts[d["thread_id"]],d["turn_id"],call["brief_text"])
            if session["model_provider"]!="gx_loopback" or calls[d["call_id"]][:2]!=("gx_decide_revision",call["args"]):
                raise ValueError("The native transcript and GX decision disagree")
            if context["model"]!=seal["native_profile"]["model"] or context["effort"]!=seal["native_profile"]["effort"]:
                raise ValueError("Unexpected researcher model profile")
            records.append({**d,"brief_delivered_utc":stamp,"transcript_sha256":digest(transcripts[d["thread_id"]]),
                            "native_record_line":calls[d["call_id"]][2],"visible_input_sha256":[m["sha256"] for m in call["brief"]["materials"]],
                            "observations":[{"artifact_ref":o["artifact_ref"],"metric_refs":[m["metric_ref"] for m in o.get("verified_metrics",[])],
                                             "endpoint_rows":o.get("endpoint_rows",[])} for o in call["brief"]["observations"]]})
        for row in db.execute("SELECT job_id,status FROM jobs WHERE run_id=? ORDER BY rowid",(rid,)):
            j=manifest.read_manifest(db,row["job_id"])
            if row["status"]!="SUCCEEDED":
                raise ValueError("Study Job did not succeed")
            admission=j["admission"];done=j["completion"]; raw,=admission["inputs"]
            cell=cells[raw["sha256"]]
            node=db.execute("SELECT result_ref FROM v6_nodes WHERE run_id=? AND job_id=?",(rid,row["job_id"])).fetchone()[0]
            start=db.execute("SELECT event_id,ts FROM events WHERE run_id=? AND kind='job.created' AND subject_id=?",(rid,row["job_id"])).fetchone()
            end=db.execute("SELECT event_id,ts FROM events WHERE run_id=? AND kind='v6.node.settled' AND json_extract(payload_json,'$.result_ref')=?",
                           (rid,node)).fetchone()
            if not end or not (cell["opening"]["assignment"]["event_id"]<start["event_id"]<=end["event_id"]):
                raise ValueError("Job chronology does not follow material assignment")
            completed=db.execute("SELECT event_id,ts FROM events WHERE run_id=? AND kind='job.transition' AND subject_id=? AND new_state='SUCCEEDED'",
                                 (rid,row["job_id"])).fetchone()
            named=next(o for o in done["outputs"] if o["output_name"]=="block-influence.json")
            published=db.execute("SELECT event_id,ts FROM events WHERE run_id=? AND kind='artifact.published' AND subject_id=?",
                                 (rid,named["artifact_ref"])).fetchone()
            if not (published and completed and start["event_id"] < published["event_id"] <= completed["event_id"] <= end["event_id"]):
                raise ValueError("Missing the original table publication, Job qualification or node settlement")
            for out in done["outputs"]:
                native.artifact(db,slot,rid,out["artifact_ref"])
            cell["jobs"].append({"job_id":row["job_id"],"status":row["status"],"mask":admission["parameters"]["mask"],
                                 "input_ref":raw["artifact_ref"],"input_sha256":raw["sha256"],"parameters":admission["parameters"],
                                 "outputs":done["outputs"],"admission_sha256":j["admission_hash"],"manifest_sha256":j["manifest_hash"],
                                 "admitted_event":start["event_id"],"admitted_utc":start["ts"],"available_event":published["event_id"],"available_utc":published["ts"],
                                 "qualified_event":completed["event_id"],"qualified_utc":completed["ts"],
                                 "node_settled_event":end["event_id"],"node_settled_utc":end["ts"]})
        claims=[json.loads(r[0]) for r in db.execute("""SELECT document_json FROM hypothesis_revisions JOIN hypotheses
                    USING(hypothesis_id) WHERE run_id=? ORDER BY hypothesis_revisions.rowid""",(rid,))]
    for i,(sha,cell) in enumerate(cells.items()):
        commit=next(d for d in records if d["decision_ref"]==cell["opening"]["commitment"]["decision_ref"])
        if sha in commit["visible_input_sha256"] or set(j["mask"] for j in cell["jobs"])!={"common_ca","common_backbone"}:
            raise ValueError("The enrollment order or the promised masks were not preserved")
        if i==1 and max(j["available_event"] for j in next(iter(cells.values()))["jobs"])>=commit["event_id"]:
            raise ValueError("Second prediction was not made after the first deposited results")
    if not claims or any(c.get("level")!="open" for c in claims):
        raise ValueError("Missing bounded formal study claims")
    account=funding.check_slot_receipts(ledger,slot.name,[json.loads(l) for l in (slot/"raw/requests.jsonl").read_text().splitlines()])
    return {"schema":"gx.coordinate-study-result/1","series":seal["series"],"status":"CLOSED","run_id":rid,
            "classification":seal["mode"],"source_commit":seal["source_commit"],"actor_class":ACTOR,"registered_utc":seal["registered_utc"],
            "seal_sha256":digest(work/"seal.json"),"first_commitment":openings[0]["commitment"],"second_commitment":openings[1]["commitment"],
            "cells":list(cells.values()),"native_decisions":records,"published_claims":claims,"cold_state_sha256":cold_hash,
            "fixture_accounting":account,"paid_provider_calls":0,"live_scientific_decisions":0,"provider_cost_usd":None,
            "interpretation":"Descriptive comparisons on these archived coordinates; biological sampling independence is unknown."}
