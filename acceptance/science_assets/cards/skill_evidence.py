"""Read skill discovery, exact native receipts and subsequent adopted actions from field stores."""
import argparse
from collections import Counter
from contextlib import closing
import json
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
TARGETS = {"interpret-coordinate-coverage@1", "interpret-enzyme-annotation@1"}
STUDIES = {
    "enzyme": "../gx-science/asset-study-live/yeast",
    "protein-coordinate-qc":"../gx-science/science-assets-live/archived-qc",
    "linear-peptide":"../gx-science/science-assets-live/modalities/linear-peptides",
    "cyclic-peptide":"../gx-science/science-assets-live/modalities/macrocycle",
    "fab":"../gx-science/science-assets-live/modalities/fab-first",
    "literature":"../gx-science/asset-live/literature",
    "slim-peptide":"../science-assets-live/slim-peptide",
    "slim-yeast": "../science-assets-live/slim-yeast",
    "prior-reading-control":"../gx-science/plugin-blueprint-live/reading-field",
}


def trusted_contexts(study, threads):
    contexts = {}
    for sessions in [study/"host/codex/sessions",study/"codex/sessions"]:
        if not sessions.is_dir():
            continue
        for file in sessions.rglob("*.jsonl"):
            if not any(thread in file.name for thread in threads):
                continue
            session = None
            with file.open(encoding="utf-8") as handle:
                for line in handle:
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        # A live host can still be completing its trailing JSON line.
                        if not line.endswith("\n"):
                            break
                        raise
                    payload = item.get("payload",{})
                    if item.get("type")=="session_meta":
                        ident = payload.get("id")
                        session = ident if ident in threads and payload.get("model_provider")=="openai" else None
                    elif item.get("type")=="turn_context" and session and payload.get("turn_id") and payload.get("model"):
                        contexts[(session,payload["turn_id"])] = payload["model"]
    return contexts


def extract(conn, study, run_id):
    scopes = dict(conn.execute("SELECT r.thread_id,t.scope FROM p0_roots r JOIN v6_decision_tasks t ON t.task_id=r.task_id WHERE r.run_id=?", (run_id,)))
    context = trusted_contexts(study, scopes)
    deliveries = {tuple(r) for r in conn.execute("SELECT DISTINCT thread_id,turn_id FROM p0_root_deliveries WHERE run_id=? AND state='acknowledged' AND method='turn/start'",(run_id,))}
    confirmed = {t for t in deliveries if t in context}
    opened, appearances, decisions = [], [], []
    query_count = Counter()
    calls = conn.execute("""
        SELECT n.rowid AS idx,n.thread_id,n.turn_id,n.call_id,n.request_json,c.result_json,t.scope
        FROM p0_v6_native_calls n JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
        JOIN p0_roots r USING(thread_id) JOIN v6_decision_tasks t ON t.task_id=r.task_id
        WHERE r.run_id=? ORDER BY n.rowid""", (run_id,))
    for row in calls:
        req, reply = json.loads(row["request_json"]),json.loads(row["result_json"])
        if req.get("tool") != "gx_query_catalog":
            continue
        args = req.get("arguments",{})
        turn = (row["thread_id"],row["turn_id"])
        common = {k:row[k] for k in ("idx","thread_id","turn_id","call_id","scope")}
        common["official_turn_confirmed"] = turn in confirmed
        query_count[row["scope"]]+=1
        if args.get("reference") and reply.get("kind")=="skill":
            card = reply.get("spec",{})
            ref = card.get("reference")
            if ref and ref==args["reference"]==reply.get("reference") and card.get("digest"):
                opened.append({**common,"reference":ref,"digest":card["digest"],"status":card.get("status"),
                               "source":card.get("source"),"guide":card.get("reading_guide")})
        for rank, item in enumerate(reply.get("items",()),1):
            ref=item.get("reference")
            if ref and (ref in TARGETS or item.get("kind")=="skill"):
                appearances.append({**common,"reference":ref,"page_rank":rank})
    for row in conn.execute("""
        SELECT n.rowid AS idx,v.thread_id,v.turn_id,v.call_id,v.decision_ref,v.request_json,v.receipt_json,t.scope
        FROM v6_decisions v JOIN p0_v6_native_calls n USING(thread_id,turn_id,call_id)
        JOIN v6_decision_tasks t ON t.task_id=v.task_id
        WHERE v.run_id=? ORDER BY n.rowid""",(run_id,)):
        req,receipt=json.loads(row["request_json"]),json.loads(row["receipt_json"])
        read = [r for r in opened if r["scope"]==row["scope"] and r["idx"]<row["idx"]]
        if not read:
            continue
        decision = req["decision"]
        grounded = json.dumps(decision,ensure_ascii=False,sort_keys=True)
        dispatches = [dict(r) for r in conn.execute("""
            SELECT n.node_id,n.job_id,n.result_ref,j.capability_id,j.capability_version,j.status
            FROM v6_nodes n LEFT JOIN jobs j ON j.job_id=n.job_id
            WHERE n.run_id=? AND n.decision_ref=? ORDER BY n.node_id""",(run_id,row["decision_ref"]))]
        decisions.append({"idx":row["idx"],"turn_id":row["turn_id"],"thread_id":row["thread_id"],"scope":row["scope"],
                          "event_id":receipt["event_id"],"decision_ref":row["decision_ref"],
                          "official_turn_confirmed":(row["thread_id"],row["turn_id"]) in confirmed,
                          "earlier_reads_in_same_scope":len(read),
                          "earlier_reads_in_same_turn":[v["reference"] for v in read if
                               (v["thread_id"],v["turn_id"])==(row["thread_id"],row["turn_id"])],
                          "explicit_skill_ref_or_digest":sorted({v["reference"] for v in read
                                                               if v["reference"] in grounded or v["digest"] in grounded}),
                          "rationale":decision.get("rationale"),"operations":[op["op"] for op in decision.get("ops",[])],
                          "evidence_refs":decision.get("evidence_refs",[]),"dispatched_nodes":dispatches})
    goal = conn.execute('SELECT status,stop_decision_ref FROM v6_goals WHERE run_id=?',(run_id,)).fetchone()
    high = conn.execute('SELECT MAX(event_id) FROM events WHERE run_id=?',(run_id,)).fetchone()[0]
    return {"run_id":run_id, "goal_status":goal["status"], "stop_decision_ref":goal["stop_decision_ref"],
            "through_event":high, "acknowledged_official_turns":len(confirmed),
            "unmatched_gx_turns":len(deliveries-confirmed), "catalog_queries_by_scope":dict(query_count),
            "skills_on_returned_pages":appearances,"exact_skill_reads":opened,"committed_after_reads":decisions}


def collect():
    studies={}
    for kind,relative in STUDIES.items():
        study=ROOT/relative
        db=study/"state/gx.db"
        if not db.is_file():
            studies[kind]={"status":"store_unavailable","relative_study":relative}
            continue
        with closing(sqlite3.connect(db.resolve().as_uri()+"?mode=ro",uri=True)) as conn:
            conn.row_factory=sqlite3.Row
            conn.execute("BEGIN")
            runs=[extract(conn,study,r[0]) for r in conn.execute("SELECT run_id FROM v6_goals ORDER BY rowid")]
            studies[kind]={"cohort":"prior" if kind.startswith("prior-") else "current","relative_study":relative,"runs":runs}
    return {"definitions":{"exact_read":"Exact requested reference, returned skill kind and matching versioned detail with a digest.",
                           "official_turn":"GX acknowledged turn matched to OpenAI host session_meta and same native turn_context.",
                           "page_rank":"One-based rank on the actual returned page; previous pages are not assumed.",
                           "after_reads":"Formal accepted decisions from that scope after the first skill read; timing alone is not causal evidence."},
            "studies":studies}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",type=Path)
    args=parser.parse_args()
    text=json.dumps(collect(),ensure_ascii=False,indent=2)+"\n"
    if args.output:
        args.output.write_text(text)
    else:
        print(text,end="")
