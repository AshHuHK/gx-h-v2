"""Export anonymous scientific text, compare against a shared primary record and retain trace receipts."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import secrets
import subprocess
import sys
import time

from common import ARMS, HERE, PACKET, blind_text, lock, read, save, sha
from trial import sql, stop

def report_for(folder,arm):
    if arm=="stock":return read(folder/"second-terminal.json")["answer"]
    rid=read(folder/"outcome.json")["facts"]["goal"]["run_id"]
    item=read(folder/"state/research"/rid/"result.json")["final_decision"]
    final=item.get("rendered") or item
    # Preserve the decision, its reasoning and limitations in their original words.
    return "\n\n".join([str(final.get("reason") or item["reason"]),str(final.get("rationale") or item["rationale"]),
                         *[str(v) for v in final.get("unresolved",item["unresolved"])]])

def contains_publisher_return(value, expected):
    """Match the complete supplied JSON even if an official user brief wraps its PI text."""
    if isinstance(value,dict):
        return value==expected or any(contains_publisher_return(x,expected) for x in value.values())
    if isinstance(value,list):
        return any(contains_publisher_return(x,expected) for x in value)
    if not isinstance(value,str):return False
    candidate=value.strip()
    for prefix in ("GX research brief", "Publisher response to your open question. Use E2 to finish the same curatorial decision."):
        if candidate.startswith(prefix):
            return contains_publisher_return(candidate[len(prefix):].lstrip(),expected)
    if not candidate.startswith(("{","[")):return False
    try: document,_=json.JSONDecoder().raw_decode(candidate)
    except ValueError:return False
    return contains_publisher_return(document,expected)

def lead_delivery(folder,arm):
    intake=folder/"late-delivery.json"
    if not intake.exists():return None
    supplied=read(intake)["at"]
    if arm=="stock":
        threads={read(folder/"thread.json")["id"]}
    else:
        threads={r["thread_id"] for r in sql(folder,"""SELECT r.thread_id FROM p0_roots r
            JOIN v6_decision_tasks d ON r.run_id=d.run_id AND r.task_id=d.task_id WHERE d.scope='run'""")}
    expected=read(PACKET/"late.json")
    completed=[]
    for path in (folder/"sessions").rglob("*.jsonl"):
        if not any(path.name.endswith(t+".jsonl") for t in threads):continue
        with path.open() as rows:
            for n,line in enumerate(rows,1):
                if "item_completed" not in line or "PMC3953731" not in line:continue
                row=json.loads(line);msg=row.get("payload",{});item=msg.get("item",{})
                if row.get("type")!="event_msg" or msg.get("type")!="item_completed" or msg.get("thread_id") not in threads:continue
                if item.get("type")=="UserMessage":body=item.get("content",[])
                elif item.get("type")=="DynamicToolCall" and item.get("success") is True:body=item.get("content_items",[])
                else:continue
                when=datetime.fromisoformat(row["timestamp"].replace("Z","+00:00")).timestamp()
                if when+1<supplied or not contains_publisher_return(body,expected):continue
                completed.append({"utc":row["timestamp"],"at":when,"session":str(path.relative_to(folder)),
                    "line":n,"thread":msg["thread_id"],"turn":msg["turn_id"],"item":item.get("id"),"source":"official "+item["type"]})
    return min(completed,key=lambda r:r["at"],default=None)

def scientific_timeline(folder,arm):
    """Keep accepted statements after the intervention, with timestamps hidden from the judge."""
    late_path=folder/"late-delivery.json"
    if not late_path.exists():return []
    late=read(late_path)["at"]
    statements=[]
    if arm=="stock":
        thread=read(folder/"thread.json")["id"]
        for path in (folder/"sessions").rglob("*"+thread+".jsonl"):
            with path.open() as rows:
                for line in rows:
                    raw=json.loads(line);obj=raw.get("payload",{})
                    if raw.get("type")!="response_item" or obj.get("role")!="assistant" or obj.get("type")!="message":continue
                    if obj.get("phase") not in (None,"final_answer"):continue
                    when=datetime.fromisoformat(raw["timestamp"].replace("Z","+00:00")).timestamp()
                    if when<late:continue
                    text=" ".join(c["text"] for c in obj.get("content",[]) if c.get("text"))
                    if text:statements.append((when,text))
    else:
        for raw in sql(folder,"""SELECT e.ts,d.request_json FROM v6_decisions d JOIN events e
                  ON e.subject_id=d.decision_ref AND e.kind='v6.decision.committed'
                  WHERE json_extract(d.request_json,'$.scope')='run' ORDER BY e.event_id"""):
            when=datetime.fromisoformat(raw["ts"].replace("Z","+00:00")).timestamp()
            if when<late:continue
            decision=json.loads(raw["request_json"])["decision"]
            parts=[decision.get("rationale","")]
            for op in decision.get("ops",[]):
                parts.extend(op.get(k,"") for k in ("reason","conclusion","question") if isinstance(op.get(k),str))
            statements.append((when," ".join(x for x in parts if x)))
    return [{"label":f"D{i}","seconds_from_late":round(when-late,3),"statement":blind_text(text)}
            for i,(when,text) in enumerate(sorted(statements),start=1)]

def export(work):
    lock(work)
    answers={}
    for arm in ARMS:
        p=work/arm/"outcome.json"
        if not p.exists():raise ValueError("Every original arm must finish or record a terminal obstruction before blinding")
        answers[arm]=report_for(work/arm,arm) if read(p)["outcome"]=="finished" else None
    mapping=work/"sealed-private-key.json"
    if not mapping.exists():
        labels=list(ARMS);secrets.SystemRandom().shuffle(labels)
        save(mapping,{f"study-{i+1}":arm for i,arm in enumerate(labels)})
    blind=work/"anonymous";blind.mkdir(mode=0o700,exist_ok=True)
    rubric=read(HERE/"rubric.json")["criteria"]
    criterion={"type":"object","properties":{"mark":{"type":"string","enum":["pass","fail","not demonstrated"]},
                "report_support":{"type":"string"},"source_locator":{"type":"string"},"reason":{"type":"string"}},
                "required":["mark","report_support","source_locator","reason"],"additionalProperties":False}
    schema={"type":"object","properties":{k:criterion for k in rubric},"required":list(rubric),"additionalProperties":False}
    for label,arm in read(mapping).items():
        d=blind/label;d.mkdir(mode=0o700,exist_ok=True)
        timeline=scientific_timeline(work/arm,arm) if answers[arm] else []
        grades_schema=json.loads(json.dumps(schema))
        grades_schema["properties"]["first_adoption"]={"type":"string","enum":["none",*[v["label"] for v in timeline]]}
        grades_schema["required"].append("first_adoption")
        packet={"decision_statements_in_order":[{"id":v["label"],"text":v["statement"]} for v in timeline],"question":(PACKET/"question.txt").read_text(),"initial":read(PACKET/"initial.json"),"late":read(PACKET/"late.json"),
                "rubric":rubric,"instruction":"For each criterion mark pass, fail or not demonstrated. Cite exact support in the report and scientific source. Unknown is valid if justified. Select first_adoption as the earliest supplied D-statement that interprets the bracketed 54 as relative activity from the same result and acts on its independence or publication status. Select none if no supplied statement does so.",
                "report":blind_text(answers[arm]) if answers[arm] else None}
        save(d/"packet.json",packet);save(d/"schema.json",grades_schema)
    return blind

def judge(work):
    blind=export(work)
    for d in sorted(blind.iterdir()):
        if not read(d/"packet.json")["report"] or (d/"score.json").exists():continue
        if not (d/"judge-terminal.json").exists():
            if (d/"judge-launch.json").exists():
                raise ValueError("Reconcile the original judge receipt before sending another review")
            save(d/"judge-launch.json",{"time":time.time(),"packet_sha256":sha(d/"packet.json")})
            p=subprocess.Popen([sys.executable,"-B",str(HERE/"native.py"),str(d),"judge"],start_new_session=True)
            try: rc=p.wait(timeout=480)
            except subprocess.TimeoutExpired: stop(p.pid);p.wait();raise
            if rc:raise RuntimeError("Blinded review did not finish; original retained")
        terminal=read(d/"judge-terminal.json")
        if terminal["status"]!="completed":raise ValueError("Judge did not complete")
        grades=json.loads(terminal["answer"]);rubric=read(HERE/"rubric.json")["criteria"]
        first_use=grades.pop("first_adoption",None)
        allowed=read(d/"schema.json")["properties"]["first_adoption"]["enum"]
        if first_use not in allowed:raise ValueError("Reviewer supplied unknown adoption locator")
        if grades.keys()!=rubric.keys() or any(
                g.get("mark") not in {"pass","fail","not demonstrated"} or not g.get("reason") for g in grades.values()):
            raise ValueError("Reviewer output cannot be scored")
        save(d/"score.json",{"turn":terminal["turn_id"],"packet_sha256":sha(d/"packet.json"),
                "criteria":grades,"first_adoption":first_use,"passed":sum(x["mark"]=="pass" for x in grades.values()),"denominator":len(grades)})

def turns(folder):
    result=[]
    for p in sorted((folder/"sessions").rglob("*.jsonl")):
        thread=None;provider=None;seen={}
        with p.open() as rows:
            for text in rows:
                try: msg=json.loads(text)
                except json.JSONDecodeError:continue
                h=msg.get("payload",{})
                if msg.get("type")=="session_meta":thread=h.get("id");provider=h.get("model_provider")
                if msg.get("type")=="turn_context" and h.get("turn_id"):
                    seen.setdefault(h["turn_id"],{"id":h["turn_id"],"model":h.get("model"),"effort":h.get("effort"),"started":msg.get("timestamp")})
        if provider!="openai":continue
        result.extend({"thread":thread,"session":str(p.relative_to(folder)),"session_sha256":sha(p),**v} for v in seen.values())
    return result

def summary(work):
    blind=export(work); mapping=read(work/"sealed-private-key.json");summaries=[]
    for label,arm in mapping.items():
        d=blind/label
        result=read(work/arm/"outcome.json")
        if result["outcome"]=="finished" and not (d/"score.json").is_file():raise ValueError("Grading precedes unblinding")
    for label,arm in mapping.items():
        f=work/arm; d=blind/label;out=read(f/"outcome.json");graded=read(d/"score.json") if (d/"score.json").exists() else None
        checkpoint=read(f/"interruption.json") if (f/"interruption.json").exists() else None
        late=read(f/"late-delivery.json") if (f/"late-delivery.json").exists() else None
        if arm=="stock":
            run=read(f/"thread.json")["id"] if (f/"thread.json").exists() else None
            after_jobs=[]; before_jobs=[]
            finish=read(f/"second-terminal.json") if (f/"second-terminal.json").is_file() else None
            endpoint=out["finish"] if finish else None
            events=[]
        else:
            facts=out["facts"];run=(facts.get("goal") or {}).get("run_id");after_jobs=facts.get("jobs",[])
            before_jobs=checkpoint["before"]["jobs"] if checkpoint else []
            events=sql(f,"SELECT event_id,kind,ts FROM events WHERE run_id=? AND (kind='v6.run.closed' OR kind='v6.decision.committed' OR kind LIKE '%supplement%' OR kind='research.close.material_prompt') ORDER BY event_id",(run,))
            endpoint=next((datetime.fromisoformat(e["ts"].replace("Z","+00:00")).timestamp()
                          for e in events if e["kind"]=="v6.run.closed"),None)
        timeline=scientific_timeline(f,arm)
        if graded:
            selected=next((r for r in timeline if r["label"]==graded["first_adoption"]),None)
        else:selected=None
        delivered=lead_delivery(f,arm) if late else None
        selected_fields=None
        if selected is not None:
            adopted_at=late["at"]+selected["seconds_from_late"]
            selected_fields={"label":selected["label"],"statement":selected["statement"],
                  "seconds_from_submission":selected["seconds_from_late"],
                  "seconds_from_verified_lead_delivery":round(adopted_at-delivered["at"],3)
                      if delivered is not None and adopted_at>=delivered["at"] else None}
        reminders=None;attempts=None
        if arm!="stock" and run:
            reminders=sql(f,"SELECT event_id,ts,payload_json FROM events WHERE run_id=? AND kind='research.close.material_prompt' ORDER BY event_id",(run,))
            for r in reminders:r["evidence"]=json.loads(r.pop("payload_json"))
            attempts=sql(f,"""SELECT DISTINCT n.thread_id,n.turn_id,n.call_id FROM p0_v6_native_calls n
                JOIN p0_roots r ON r.thread_id=n.thread_id AND r.run_id=?
                JOIN json_each(n.request_json,'$.arguments.decision.ops') op
                WHERE json_extract(n.request_json,'$.tool')='gx_decide_revision' AND json_extract(op.value,'$.op')='close_run'
                ORDER BY n.rowid""",(run,))
        reminder_support={"raw_reminders":reminders,"close_attempts":attempts,
            "opportunities":len(attempts) if attempts is not None else None,
            "supported_reminders":0 if reminders==[] and attempts else None,
            "qualification":"Raw GX scientific reminder and material linkage; if one occurs, independent review is required to mark support. Stock has no GX close-op/reminder."}
        summaries.append({"arm":arm,"blind_id":label,"run_or_native_thread":run,"outcome":out["outcome"],
            "field_turns":turns(f),"commit":read(f/"began.json")["release"],
            "wall_seconds":out["elapsed_seconds"],"external_jobs":after_jobs,
            "external_jobs_added_after_interrupt":len({j["job_id"] for j in after_jobs}-{j["job_id"] for j in before_jobs}) if checkpoint else None,
            "interrupted_at_first_read":bool(checkpoint),"source_read":checkpoint["checkpoint"] if checkpoint else None,
            "publisher_submission":late,"verified_lead_delivery":delivered,"premature_close_check":reminder_support,"events":events,
            "critical_sources_examined":{"denominator":2,"archive_read":bool(checkpoint),
                "paper_use_supported_by_judge":bool(graded and graded["criteria"]["counterevidence"]["mark"]=="pass"
                                                      and graded["criteria"]["locator"]["mark"]=="pass")},
            "earliest_adoption_supported_by_blind_reader":selected_fields,
            "seconds_from_verified_lead_delivery_to_accepted_final_use_upper_bound":round(endpoint-delivered["at"],2) if endpoint and delivered and endpoint>=delivered["at"] and graded and graded["criteria"]["counterevidence"]["mark"]=="pass" else None,
            "human_scientific_interventions_excluding_preregistered_delivery":0,
            "grade":graded,"judge_sessions":turns(d),"anonymous_packet_sha256":sha(d/"packet.json")})
    return {"registration":read(work/"registration.json"),"arms":summaries,
            "limits":"One paired archival question; independent scientific support uses blind judgement. Elapsed-to-close is an upper bound on first adoption. A fact not witnessed remains null."}

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("workspace",type=Path)
    p.add_argument("action",choices=["export","judge","summary"]);args=p.parse_args();w=args.workspace.resolve()
    if args.action=="export":print(export(w))
    elif args.action=="judge":judge(w)
    else: print(json.dumps(summary(w),indent=2,ensure_ascii=False))
