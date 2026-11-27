"""Verify real browser, scoped PI effects, native research and independent child measurement."""
import json

from .live import CARDS, db, module_turns, parent_id, research_routes, write


def load(path):
    return json.loads(path.read_text())


def summarize(study):
    return {
        "run_id": study["run_id"], "close": study["closure"]["decision_ref"],
        "closed_event":study["closure"]["closed_event"],
        "conclusion":study["final_decision"]["rendered"],
        "jobs":[{"job_id":job["job_id"], "method":job["method"]["id"], "status":job["status"],
                 "inputs":[i["sha256"] for i in job["inputs"]], "branch_id":job["owner"].get("branch_id"),
                 "outputs":[{"ref":o["artifact_ref"], "lead_deliveries":o.get("lead_delivery_event_ids",[])}
                            for o in job["outputs"]]} for job in study["jobs"]],
        "actions":[{k:v for k,v in a.items() if k in
                    ("decision_ref","scope","event_id","report_events","operations","rendered_rationale")}
                   for a in study["post_read_actions"]]
    }


def contact_tables(assets, study):
    """Read the immutable full tables only after closing, with the actual Job's input digest."""
    rows = []
    for job in study["jobs"]:
        inputs = {i["sha256"] for i in job["inputs"]}
        for output in job["outputs"]:
            if job["status"] != "SUCCEEDED" or output["output_name"] != "interface-table.json":
                continue
            table = assets.read_json(output["artifact_ref"])
            if table.get("schema") != "gx.pcna-interface-table/1" or table["input_sha256"] not in inputs:
                raise ValueError("Interface table does not identify its Job's coordinate material")
            rows.append(dict(entry=table["entry"], model=table["model"], job_id=job["job_id"],
                             table_ref=output["artifact_ref"], branch_id=job["owner"].get("branch_id"),
                             cutoff_A=table["contact_cutoff_A"], sha256=table["input_sha256"],
                             pairs=[dict(chains=p["chains"],contacts=p["whole"]["n_contact_residue_pairs"])
                                    for p in table["pairs"].values()]))
    return rows


def verify(work, key):
    from gx_core.artifacts import ArtifactService
    from gx_core.p0 import conversation

    pi, follow = load(work/"pi.json"), load(work/"followup.json")
    parent, child = parent_id(work,key), follow["child_run_id"]
    studies = [load(work/"state/research"/rid/"result.json") for rid in (parent,child)]
    first, second = studies
    mid = pi["challenge"]["payload"]["client_message_id"]
    ids = [r["turn_id"] for r in pi["at_send"]["routes"]]
    a,b = pi["at_send"]["routes"]
    with db(work) as conn:
        assets = ArtifactService(conn, work/"state/artifacts")
        tables = {s["run_id"]:contact_tables(assets,s) for s in studies}
        parent_view = conversation.timeline(conn,parent)
        child_view = conversation.timeline(conn,child)
        messages = {m["message_id"]:m for m in parent_view["messages"]}
        challenge = messages[mid]
        table_count = conn.execute("SELECT count(*) FROM p0_operator_messages WHERE run_id=? AND message_id=?",
                                   (parent,mid)).fetchone()[0]
        b_pub = conn.execute("""SELECT p.receipt_json FROM v62_module_reports p
             WHERE p.run_id=? AND p.module_instance_id=?""",(parent,b["module_instance_id"])).fetchone()
        author = json.loads(b_pub[0]).get("author",{}) if b_pub else {}
        call = conn.execute("""SELECT result_json FROM p0_root_calls WHERE thread_id=? AND turn_id=? AND call_id=?""",
                            (author.get("thread_id"),author.get("turn_id"),author.get("call_id"))).fetchone()
        accepted_by_host = json.loads(call[0]) if call else {}
        released = conn.execute("""SELECT e.ts FROM v62_module_reports p JOIN events e
                ON e.run_id=p.run_id AND e.event_id=json_extract(p.receipt_json,'$.event_id')
                WHERE p.run_id=? AND p.module_instance_id=?""", (parent,b["module_instance_id"])).fetchone()
        real = [t for t in module_turns(conn,work,parent) if t["turn_id"] in ids and t["classification"]=="NATIVE_MODEL"]
    arrival = research_routes._instant(challenge["created_at"])
    bounds = [(research_routes._instant(t["start"]),research_routes._instant(t["end"])) for t in real]
    overlaps = [x for x in first["routes"]["real_turn_overlaps"] if {x["left_turn"],x["right_turn"]} == set(ids)]
    modules = {m["module_instance_id"]:m for m in first["routes"]["modules"]}
    module_a,module_b = [modules[r["module_instance_id"]] for r in (a,b)]
    feedback = challenge["feedback"]
    targeted = [f for f in feedback if f["branch_id"]==a["branch_id"]]
    dispositions = [f["disposition"] for f in targeted if f["deliveries"] and f["disposition"]]
    follow_msg = messages[key+"-new-material"]["result"]
    b_reports = module_b["report"] or {}
    b_turn = next((t for th in module_b["threads"] for t in th["turns"]
                   if th["thread_id"]==author.get("thread_id") and t["turn_id"]==author.get("turn_id")), {})
    b_source = b_reports.get("source_refs") or []
    b_real = (author.get("kind")=="MODULE" and b_turn.get("classification")=="NATIVE_MODEL"
              and accepted_by_host.get("report_ref")==b_reports.get("report_ref") and bool(b_source))
    reopen = load(work/"reopen.json")["result"]["invocation"]
    rcv = {h["report_ref"] for h in first["routes"]["handoffs"] if h.get("received_content")
           and h.get("receiver_scope")=="run" and h.get("decision_ref")}
    parent_reports = {m["report"]["report_ref"] for m in (module_a,module_b) if m["report"]}
    new_digest = "05a1488d313d9926f556f495ad155e3ebf7859b2fa3014bc68ae55465acdb776"
    new_jobs = [j for j in second["jobs"] if j["status"]=="SUCCEEDED" and j["outputs"] and
                any(i["sha256"]==new_digest for i in j["inputs"])]
    new_outputs = {o["artifact_ref"] for j in new_jobs for o in j["outputs"]}
    new_delivery = any(d["scope"]=="run" and any(e.get("source_ref") in new_outputs for e in d["endpoints"])
                       for d in second["deliveries"])
    check = {
        "driver_restarted_same_run": load(work/"study.json")["result"]["invocation"]["action"]=="start"
             and load(work/"continue-study.json")["result"]["invocation"]["action"]=="resume"
             and load(work/"study.json")["result"]["run_id"]==parent
             and load(work/"study.json")["driver_pid"]!=load(work/"continue-study.json")["driver_pid"],
        "browser_to_cli_exactly_once": table_count==1 and challenge["text"]==pi["challenge"]["payload"]["text"]
            and pi["replay"]["message_id"]==mid,
        "cli_to_refreshed_browser": pi["refresh"]["visible"] and messages[key+"-language"]["text"] in pi["refresh"]["text"],
        "both_routes_visible_at_send": {r["branch_id"] for r in pi["challenge"]["visibleRoutes"]}
            =={a["branch_id"],b["branch_id"]},
        "real_turns_active_at_ingress": bool(overlaps) and len(real)==2 and arrival is not None
            and all(lo is not None and hi is not None and lo <= arrival < hi for lo,hi in bounds),
        "actual_parent_coordinate_tables": {row["entry"].upper() for row in tables[parent]} == {"1VYM","9VGW"}
             and all(len(row["pairs"])==3 for row in tables[parent]),
        "main_answered_compound_and_trace": all(bool(messages[x]["reply"] and messages[x]["reply"]["text"])
             for x in [mid,key+"-trace"]),
        "a_scientific_disposition": any(d["outcome"] in {"measure","adopt","maintain"}
             and d["reason"] and d["product_ref"] for d in dispositions),
        "b_continued_original_work": b_real and bool(b_reports.get("released_event")) and bool(released)
             and arrival is not None and research_routes._instant(released[0]) >= arrival and module_b["status"] in ("COMPLETE","PARTIAL")
             and any(n["job_status"]=="SUCCEEDED" for n in module_b["nodes"])
             and any(th["task_id"]==b["task_id"] and th["thread_id"]==b["thread_id"] for th in module_b["threads"]),
        "lead_used_published_evidence": len(parent_reports)==2 and parent_reports<=rcv,
        "sealed_explanation": bool(follow["sealed_response"]["reply"]["text"]) and
             follow["sealed_before"]["jobs"]==follow["sealed_after"]["jobs"],
        "child_same_conversation_and_parent": parent_view["conversation_id"]==child_view["conversation_id"]
             and follow_msg["parent_decision_ref"]==first["closure"]["decision_ref"] and
             follow_msg["run_id"]==child and parent!=child and follow["switched"]["runId"]==child,
        "child_new_coordinate_measurement": bool(new_jobs) and (new_delivery or
            any(h.get("receiver_scope")=="run" and h.get("received_content") and h.get("decision_ref")
                for h in second["routes"]["handoffs"])),
        "sealed_reexport": reopen["action"]=="export_closed" and reopen["added"]=={"jobs":0,"host_turns":0}
    }
    evidence = dict(tested_build=load(work/"study.json")["build"], checks=check, credential_route="official Codex ChatGPT OAuth via local Clash",
        project=parent_view["conversation_id"], parent=summarize(first),child=summarize(second), tables=tables,
        intake={k:challenge[k] for k in ("message_id","text","created_at","branch_id","clauses","feedback")},
        native=real, overlap=overlaps, modules=[{k:m[k] for k in ("scope","branch_id","focus","status","report")}
                                               for m in (module_a,module_b)],
        replies=[{k:m[k] for k in ("message_id","text","reply","status")} for m in parent_view["messages"]
                 if m["message_id"] in (mid,key+"-trace",follow["sealed"]["payload"]["client_message_id"])],
        successor=follow_msg, reopen=reopen)
    write(work/"live-evidence.json",evidence)
    lines = ["# 人 PCNA 沉积坐标的界面质控", "", (CARDS/"question.md").read_text().split("\n\n")[1],
             "", "## 首航研究", "", first["final_decision"]["rendered"]["rationale"],"",
             f"PI 在 {challenge['created_at']} 提出「{challenge['text']}」",""]
    lines += ["| 坐标 | 链对及其接触残基对数 | 原始 Job |", "|---|---|---|"]
    for row in tables[parent]:
        values = "，".join("-".join(pair["chains"])+" "+str(pair["contacts"]) for pair in row["pairs"])
        lines.append(f"| {row['entry']} 模型 {row['model']} | {values} | {row['job_id']} |")
    lines += ["", "接触按沉积坐标中任意非氢原子间距不超过 4 Å 的独特残基对计数。", ""]
    for d in dispositions:
        lines += [f"研究者在事件 {d['event_id']} 处置为 {d['outcome']}。{d['reason']}", ""]
    lines += ["## 开放后继研究","",second["final_decision"]["rendered"]["rationale"],""]
    for label,study in zip(("父题","后继"),studies):
        lines += [f"{label}封存决定 {study['closure']['decision_ref']}，事件 {study['closure']['closed_event']}。"]
        lines.extend(str(x) for x in study["final_decision"]["rendered"]["unresolved"])
        lines.append("")
    (work/"RESEARCH.md").write_text("\n".join(lines),encoding="utf-8")
    missing = [name for name,ok in check.items() if not ok]
    print(json.dumps(check, indent=2,ensure_ascii=False))
    if missing:
        raise ValueError("Unverified live requirements: "+", ".join(missing))
