"""Seal blinded public-pilot evidence and collect preregistered independent signed judgments."""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/"gx-h")]
from acceptance.v62c.rate import freeze, read, file_hash
from acceptance.v62c.scoring import digest, need
from acceptance.v623.live import action_witness

RUBRIC=HERE/"rubric.json"
PROTOCOL=HERE/"protocol.json"
NAMESPACE="gx-live-scientific-review-v1"
CRITERIA=tuple(read(RUBRIC)["criteria"])
OFFLINE="OFFICIAL_CODEX_LOOPBACK"
LIVE="CURRENT_AUTHORIZED_PROVIDER"


def validate_roster(roster,mode="live"):
    need(mode=="live" and isinstance(roster,dict) and roster.get("schema")=="gx.live-review-roster/1",
         "Independent scientific review requires a PI-registered roster")
    need(roster.get("series")==read(PROTOCOL)["series"], "Roster belongs to another series")
    readers=roster.get("reviewers",[])
    need(isinstance(readers,list) and len(readers) in (1,2),"Register one or two independent outcome reviewers")
    roles=[roster.get("opportunity_reader"),*readers]
    if roster.get("arbitrator"):
        roles.append(roster["arbitrator"])
    elif len(readers)==2:
        raise ValueError("Register the independent arbitrator before enrolling a two-reader series")
    ids=set(); keys=set()
    for role in roles:
        need(isinstance(role,dict) and role.get("independent_of_design_development_execution") is True and
             role.get("kind") in ("human","authorized_model"),"Missing independent reviewer declaration")
        name=role.get("id");pub=role.get("public_key","")
        need(isinstance(name,str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}",name) and name not in ids,
             "Reviewer principals must be distinct")
        need(isinstance(pub,str) and re.fullmatch(r"ssh-ed25519 [A-Za-z0-9+/]+={0,2}",pub) and pub not in keys,
             "Reviewer must have a unique SSH Ed25519 verification key")
        key=base64.b64decode(pub.split()[1],validate=True)
        need(len(key)==51 and key.startswith(b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20"),
             "Invalid Ed25519 public key")
        ids.add(name);keys.add(pub)
    return roster


def _root(work):
    return Path(work)/"adjudication"


def _reg(work):
    root=_root(work); a=read(root/"registration.json"); enrollment=read(Path(work)/"registration.json")
    need(a["enrollment_sha256"]==file_hash(Path(work)/"registration.json") and
         a["rubric_sha256"]==file_hash(RUBRIC) and a["protocol_sha256"]==file_hash(PROTOCOL) and
         all(file_hash(HERE/k)==v for k,v in a["software"].items()),"Frozen scoring protocol changed")
    if a["host"]=="live":
        need(digest(a["roster"])==enrollment["reviewer_roster_sha256"],"Registered reviewer roster differs")
        validate_roster(a["roster"])
    return a


def _register(work,roster,host):
    reg=read(Path(work)/"registration.json");p=read(PROTOCOL)
    need(reg["host"]==host and reg["scope"]==(LIVE if host=="live" else OFFLINE) and
         reg["series"]==p["series" if host=="live" else "rehearsal_series"],"Wrong cohort or host class")
    target=_root(work)/"registration.json"
    if host=="live":
        approve=reg.get("funding",{})
        need(reg.get("status")=="READY" and approve.get("ready") is True and
             approve.get("enforcement",{}).get("status")=="VERIFIED_DEPLOYMENT" and
             approve.get("authorization",{}).get("covers_series") is True, "Live enrollment lacks approved deployment")
        validate_roster(roster)
        need(reg.get("reviewer_roster_sha256")==digest(roster),"PI-approved enrollment has no matching roster digest")
        if not target.exists():
            need(not any((Path(work)/s["slot"]/"state/gx.db").exists() for s in p["slots"]),
                 "Reviewer enrollment must precede the first Run")
    value={"series":reg["series"],"host":host,"roster":roster,"rubric_sha256":file_hash(RUBRIC),
           "protocol_sha256":file_hash(PROTOCOL),"enrollment_sha256":file_hash(Path(work)/"registration.json"),
           "software":{k:file_hash(HERE/k) for k in ("adjudication.py","action_witness.py")}}
    freeze(target,value);return value


def prepare_offline(work):
    return _register(work,None,"offline")


def prepare_live(work,roster):
    return _register(work,roster,"live")


def _paths(work,slot):
    need(slot in {s["slot"] for s in read(PROTOCOL)["slots"]},"Unregistered slot")
    return _root(work)/slot


def _bound(work,slot):
    reg=_reg(work); path=_paths(work,slot)
    return reg,path


def prepare(work,slot):
    reg,path=_bound(work,slot)
    need((Path(work)/slot/"summary.json").is_file(),"The original Run has no sealed slot summary yet")
    trace=action_witness.trace_slot(work,slot,read(PROTOCOL),False)
    packet,machine=action_witness.reader_packet(trace)
    packet.update(rubric_sha256=reg["rubric_sha256"],blinding=read(RUBRIC)["blinding"])
    freeze(path/"opportunity.json",packet);freeze(path/"private/opportunity.json",machine)
    return packet,machine


def _verify(role,text,sig):
    """Verify the exact submitted UTF-8 bytes, including intentional whitespace."""
    with TemporaryDirectory() as tmp:
        d=Path(tmp);(d/"allowed").write_text(role["id"]+" "+role["public_key"]+"\n")
        (d/"signature").write_text(sig)
        proc=subprocess.run(["ssh-keygen","-Y","verify","-f",str(d/"allowed"),"-I",role["id"],
                             "-n",NAMESPACE,"-s",str(d/"signature")],input=text.encode(),
                            stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    need(proc.returncode==0,"Reviewer signature or signing principal differs")


def _submission(reg,kind,packet,document,role):
    text=document["text"];j=json.loads(text)
    need(isinstance(j,dict) and j.get("schema")=="gx.live-review/1" and j.get("reviewer")==role["id"] and
         j.get("stage")==kind and j.get("series")==reg["series"] and j.get("packet_sha256")==digest(packet) and
         j.get("rubric_sha256")==reg["rubric_sha256"],"Signed review does not identify its packet, rubric and reviewer")
    _verify(role,text,document["signature"])
    return j


def _audit_opportunity(reg, path):
    record=read(path/"lock.json"); packet=read(path/"opportunity.json"); machine=read(path/"private/opportunity.json")
    need(record["packet_sha256"]==digest(packet) and record["facts_sha256"]==digest(machine),"Changed opportunity")
    if reg["host"]=="live":
        submission=_submission(reg,"opportunity",packet,record["submission"],reg["roster"]["opportunity_reader"])
        need(submission.get("state")==machine["state"] and submission.get("basis","").strip() and
             submission.get("measurement_lines")==[k for k in packet["lines"] if k.startswith("M")],
             "Reader did not confirm the locked window")
    else:
        need(record["reader"]=="DETERMINISTIC_DELIVERY_REHEARSAL","Wrong rehearsal type")
    return machine


def lock(work,slot,document=None):
    reg,path=_bound(work,slot);packet,machine=prepare(work,slot)
    if reg["host"]=="live":
        r=reg["roster"]["opportunity_reader"]
        j=_submission(reg,"opportunity",packet,document,r)
        need(j.get("state")==machine["state"] and isinstance(j.get("basis"),str) and j["basis"].strip()
             and j.get("measurement_lines")==[k for k in packet["lines"] if k.startswith("M")],
             "Reader lock differs from the first delivered evidence")
        item={"reader":r["id"],"submission":document}
    else:
        need(document is None,"Offline rehearsal does not impersonate independent scientific readers")
        item={"reader":"DETERMINISTIC_DELIVERY_REHEARSAL"}
    item.update(packet_sha256=digest(packet),facts_sha256=digest(machine))
    freeze(path/"lock.json",item);return item


def outcome(work,slot):
    reg,path=_bound(work,slot);first=_audit_opportunity(reg,path)
    trace=action_witness.trace_slot(work,slot,read(PROTOCOL),True)
    packet,machine=action_witness.outcome_packet(trace,read(PROTOCOL),first)
    if trace.get("run_id") and not trace.get("missing_assignment"):
        need(trace.get("actor_class")==("SCRIPTED_REHEARSAL" if reg["host"]=="offline" else "OFFICIAL_PROVIDER_UNADJUDICATED"),
             "Original native actor class differs from enrollment")
    packet.update(rubric_sha256=reg["rubric_sha256"],blinding=read(RUBRIC)["blinding"],
                  opportunity_sha256=file_hash(path/"lock.json"))
    machine["classification"]=OFFLINE if reg["host"]=="offline" else LIVE
    freeze(path/"outcome.json",packet);freeze(path/"private/outcome.json",machine)
    return packet,machine


def _marks(sub,packet):
    judged=sub.get("criteria",{})
    need(set(judged)==set(CRITERIA),"Complete all four scientific criteria")
    for k,item in judged.items():
        need(isinstance(item,dict) and item.get("mark") in ("PASS","FAIL","UNCLEAR")
             and isinstance(item.get("basis"),str) and item["basis"].strip(),"Each criterion needs a verdict and reason")
        lines=item.get("lines");need(isinstance(lines,list) and lines and set(lines)<=set(packet["lines"]),
                                    "Each verdict must cite exact packet lines")
        if k in ("reason_scope","action_reason"):
            need("R" in lines and ("C" in lines or k=="action_reason" and "A" in lines or
                 "C" not in packet["lines"]),"Cite the decision reason and relevant raw-count or enacted effect")
    return {k:judged[k]["mark"] for k in CRITERIA}


def _originals(reg,path,packet):
    records={}
    for r in reg["roster"]["reviewers"]:
        fn=path/"reviews"/(r["id"]+".json")
        if fn.exists():
            doc=read(fn)
            records[r["id"]]=(_marks(_submission(reg,"reason",packet,doc,r),packet),digest(doc))
    return records


def submit(work,slot,document,*,resolve=False):
    reg,path=_bound(work,slot);need(reg["host"]=="live","Offline records do not accept official reviews")
    _audit_opportunity(reg,path);packet=read(path/"outcome.json");body=json.loads(document["text"])
    if resolve:
        role=reg["roster"].get("arbitrator")
        need(role is not None,"No arbitrator was registered")
        originals=_originals(reg,path,packet)
        need(len(originals)==len(reg["roster"]["reviewers"]),"Collect the registered independent reviews first")
        marks=[m for m,_ in originals.values()]
        need(any(v=="UNCLEAR" for m in marks for v in m.values()) or len(marks)==2 and marks[0]!=marks[1],
             "Resolution requires an actual disagreement or unresolved judgment")
        j=_submission(reg,"resolution",packet,document,role)
        need(j.get("originals")=={k:v[1] for k,v in originals.items()},"Arbitration must bind the original signed reviews")
        dest=path/"resolution.json"
    else:
        role=next((r for r in reg["roster"]["reviewers"] if r["id"]==body.get("reviewer")),None)
        need(role,"Unregistered outcome reader")
        j=_submission(reg,"reason",packet,document,role);dest=path/"reviews"/(role["id"]+".json")
    _marks(j,packet);freeze(dest,document)
    return score(work,slot)


def _cost_and_host(work,slot,reg,machine):
    slot_dir=Path(work)/slot
    ledger_file=slot_dir/"accounting.json"
    ledger=read(ledger_file) if ledger_file.is_file() else {}
    requests=ledger.get("requests",[])
    complete=bool(requests and not ledger.get("unmatched_turns") and
                  all(r.get("state")=="SETTLED" and r.get("provider_receipt_id") and
                      type(r.get("actual")) is int and r["actual"]>=0 for r in requests))
    if reg["host"]=="live" and machine.get("run_id"):
        from acceptance.v623.live.hosts import role_witness
        signed=read(slot_dir/"native-host.json")
        enrollment=read(Path(work)/"registration.json")
        need(signed["source"]=="PRODUCTION" and signed["classification"]==LIVE and
             signed["deployment"]==enrollment["deployment_id"] and signed["contract"]==enrollment["contract_id"]
             and signed["official_host_sha256"]==enrollment["official_host"]["binary_sha256"] and
             signed["roots"]==role_witness(slot_dir,machine["run_id"]), "Live actor receipts differ from the authorized deployment")
        complete=complete and len({r["provider_receipt_id"] for r in requests})==len(requests) and all(
            r["run_id"]==machine["run_id"] and r["model"]==enrollment["model"] and
            r["effort"]==enrollment["effort"] for r in requests)
    live=reg["host"]=="live"
    return {"accounting_sha256":file_hash(ledger_file) if ledger_file.is_file() else None,
            "physical_requests":len(requests),"provider_tokens":sum(r["actual"] for r in requests)
            if live and complete else None,"provider_usd":None,"charges_reconciled":bool(complete)}


def score(work,slot):
    reg,path=_bound(work,slot);_audit_opportunity(reg,path)
    packet,machine=outcome(work,slot)
    result={"opportunity":machine["state"],"intent":machine["intent"],"enacted":machine["enacted"],
            "stop_reason":machine["stop_reason"],"delegation":machine["delegation"],
            "classification":machine["classification"],"interpretable":False,
            "science_pass":None,"agreement":None,"status":"PENDING_INDEPENDENT_REVIEW","criteria":None,
            "run_id":machine["run_id"],"effects":machine["effect"],"slot":slot,
            "science_opportunity":machine["scientifically_interpretable"],"cost":_cost_and_host(work,slot,reg,machine)}
    if not machine["scientifically_interpretable"]:
        result["status"]="NO_INTERPRETABLE_DECISION"
    if reg["host"]=="offline":
        return result
    originals=_originals(reg,path,packet); marks=[m for m,_ in originals.values()]
    if len(marks)!=len(reg["roster"]["reviewers"]): return result
    if len(marks)==2:
        result["agreement"]={"matching":sum(marks[0][k]==marks[1][k] for k in CRITERIA),"total":len(CRITERIA)}
    decision=marks[0]
    if (len(marks)==2 and marks[0]!=marks[1]) or "UNCLEAR" in decision.values():
        result["status"]="PENDING_ARBITRATION"
        if not (path/"resolution.json").exists(): return result
        sub=_submission(reg,"resolution",packet,read(path/"resolution.json"),reg["roster"]["arbitrator"])
        need(sub.get("originals")=={k:v[1] for k,v in originals.items()},"Arbitration history differs")
        decision=_marks(sub,packet)
    result["criteria"]=decision
    if "UNCLEAR" in decision.values():
        result["status"]="PENDING_ARBITRATION"
    elif machine["scientifically_interpretable"]:
        result.update(status="REVIEWED",interpretable=True,science_pass=bool(all(v=="PASS" for v in decision.values())
                 and machine["independent_match"] and machine["endpoint_cited"] and machine["action_matches_intent"]))
    else:
        result["status"]="NO_INTERPRETABLE_DECISION"
    return result


def assess_offline_slot(work,number):
    slot=str(number).zfill(2);lock(work,slot)
    result=score(work,slot)
    freeze(_paths(work,slot)/"rehearsal-score.json",result)
    return result


def ready_for_next(work,number):
    slot=str(number).zfill(2);reg,path=_bound(work,slot)
    if not (path/"outcome.json").exists():return False
    s=score(work,slot)
    if reg["host"]=="offline":
        return (path/"rehearsal-score.json").is_file() and read(path/"rehearsal-score.json")==s
    if not (Path(work)/slot/"native-host.json").is_file() or not (Path(work)/slot/"accounting.json").is_file():
        return False
    return (s["status"] in ("REVIEWED","NO_INTERPRETABLE_DECISION") and s["criteria"] is not None
            and s["cost"]["charges_reconciled"])


def summary(work):
    reg=_reg(work);cases={}
    for item in read(PROTOCOL)["slots"]:
        slot=item["slot"];path=_paths(work,slot)
        if (path/"outcome.json").exists():cases[slot]=score(work,slot)
        else:cases[slot]={"status":"NOT_ADJUDICATED","science_pass":None,"interpretable":False}
    interpreted=sum(c["interpretable"] for c in cases.values());passed=sum(c["science_pass"] is True for c in cases.values())
    return {"series":reg["series"],"cases":cases,"interpretable_of_assigned":[interpreted,len(cases)],
            "science_pass_of_interpretable":[passed,interpreted]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command",choices=("register","prepare","lock","outcome","review","resolve","score","summary"))
    parser.add_argument("--work",type=Path,required=True);parser.add_argument("--slot",choices=("01","02","03","04"))
    parser.add_argument("--roster",type=Path);parser.add_argument("--submission",type=Path);parser.add_argument("--signature",type=Path)
    a=parser.parse_args()
    if a.command=="register":
        result=prepare_live(a.work,read(a.roster)) if a.roster else prepare_offline(a.work)
    elif a.command=="summary":result=summary(a.work)
    else:
        if not a.slot:parser.error("Specify the original --slot")
        if bool(a.submission)!=bool(a.signature):parser.error("Supply both --submission and --signature")
        doc={"text":a.submission.read_text(),"signature":a.signature.read_text()} if a.submission else None
        if a.command=="prepare":result=prepare(a.work,a.slot)[0]
        elif a.command=="lock":result=lock(a.work,a.slot,doc)
        elif a.command=="outcome":result=outcome(a.work,a.slot)[0]
        elif a.command in ("review","resolve"):result=submit(a.work,a.slot,doc,resolve=a.command=="resolve")
        else:result=score(a.work,a.slot)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=="__main__":main()
