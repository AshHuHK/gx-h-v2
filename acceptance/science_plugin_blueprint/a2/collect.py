"""Export PI-verifiable receipts from the existing blind Run and official session histories."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.science_capabilities.field_collaboration import route_receipt
from acceptance.science_plugin_blueprint.a2.deliver_later import first_body_read
from gx_core.artifacts import ArtifactService
from gx_core.p0 import research_routes, v6_native


def collect(work):
    state = work / 'state'
    with closing(sqlite3.connect((state/'gx.db').as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        goal = db.execute('SELECT run_id,status,goal_version FROM v6_goals').fetchone()
        rid = goal['run_id']
        def rows(query, parameters=()):
            return [dict(row) for row in db.execute(query,parameters)]
        store = ArtifactService(db, root=state/'artifacts')
        original = rows("""SELECT s.title,s.source_id,s.blob_artifact_ref AS source_ref,s.blob_sha256,s.text_coverage,
            s.segments_count,count(r.receipt_id) AS paragraph_deliveries,count(DISTINCT r.segment_id) AS distinct_segments
            FROM sources s LEFT JOIN source_reads r USING(source_id) WHERE s.namespace=? GROUP BY s.source_id""", (rid,))
        segments = rows("""SELECT s.title,d.scope,r.principal, count(*) AS paragraph_deliveries,
            group_concat(DISTINCT r.segment_id) AS segments FROM sources s JOIN source_reads r USING(source_id)
            JOIN v6_decision_tasks d USING(task_id) WHERE s.namespace=? GROUP BY s.title,d.scope,r.principal""", (rid,))
        pages = rows("""SELECT json_extract(n.request_json,'$.arguments.source_ref') AS source_ref,
            n.thread_id,count(*) AS lists,group_concat(DISTINCT coalesce(json_extract(n.request_json,'$.arguments.start'),0)) AS page_starts
            FROM p0_v6_native_calls n JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
            JOIN p0_roots r USING(thread_id) WHERE r.run_id=? AND json_extract(n.request_json,'$.tool')='gx_read_source'
            AND json_type(n.request_json,'$.arguments.segment_id') IS NULL GROUP BY source_ref,n.thread_id""",(rid,))
        extracts = rows("SELECT evidence_id,claim,conditions,source_id,anchored_segment_id,receipt_id,review_status,"
                        "quote_sha256,extracted_principal FROM evidence WHERE run_id=? ORDER BY rowid", (rid,))
        reviews = rows("""SELECT r.review_id,r.evidence_id,r.principal AS reviewer,r.receipt_id AS review_receipt,
            r.verdict,r.note,r.independence_level,e.extracted_principal AS author,e.receipt_id AS extraction_receipt,
            a.scope AS author_scope,v.scope AS reviewer_scope,e.source_id,e.anchored_segment_id
            FROM evidence_reviews r JOIN evidence e USING(evidence_id) JOIN v6_decision_tasks a ON a.task_id=e.extracted_task_id
            JOIN v6_decision_tasks v ON v.task_id=r.task_id WHERE e.run_id=? ORDER BY r.rowid""", (rid,))
        reports = []
        for row in rows("""SELECT m.module_id,m.scope,r.report_ref,r.receipt_json FROM v62_module_reports r
                JOIN v62_modules m USING(module_instance_id) WHERE r.run_id=? ORDER BY r.rowid""", (rid,)):
            doc = store.read_json(row['report_ref'])
            receipt = json.loads(row.pop('receipt_json'))
            reports.append({**row, 'status':doc['status'], 'author':doc['author'],
                'published_event':receipt['event_id'], 'released_event':receipt.get('wake_event_id'),
                'words':doc.get('researcher_statement') if doc['author']['kind']=='MODULE' else None,
                'gx_gaps':doc.get('gaps') if doc['author']['kind']=='GX' else None,
                'source_refs':[x['artifact_ref'] for x in doc['sources']]})
        decisions = []
        for row in rows("SELECT d.decision_ref,t.scope,d.task_id,d.thread_id,d.turn_id,d.request_json,d.receipt_json "
                        "FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id) WHERE d.run_id=? ORDER BY d.rowid",(rid,)):
            req = json.loads(row.pop('request_json'))["decision"]
            rec = json.loads(row.pop('receipt_json'))
            decisions.append({**row,'rationale':req.get('rationale'),'operations':req.get('ops'),
                              'evidence_refs':req.get('evidence_refs'), 'effective':rec})
        turns = []
        for row in db.execute("SELECT * FROM p0_roots WHERE run_id=? ORDER BY rowid", (rid,)):
            for turn in research_routes._turns(db,rid,state,row):
                turns.append({**{k:turn.get(k) for k in ('turn_id','thread_id','classification','model','start','end','terminal_status')},
                              'task_id':row['task_id'], 'role':row['role']})
        routes = research_routes.routes(db,rid,state)
        by_ref = {report["report_ref"]:report for report in reports}
        for handoff in routes["handoffs"]:
            if not handoff.get("delivery_receipt") or handoff.get("receiver_scope") != "run":
                continue
            binding = db.execute("SELECT * FROM p0_roots WHERE run_id=? AND thread_id=?",
                                 (rid,handoff["thread_id"])).fetchone()
            ack = v6_native.acknowledged_brief(db,binding,handoff["turn_id"])
            summary = next((r for r in ack[0].get("module_reports",()) if r["report_ref"]==handoff["report_ref"]),{}) if ack else {}
            message = summary.get("researcher_message",{})
            if message:
                handoff["delivered_author_message"] = message
                statement = by_ref[handoff["report_ref"]].get("words") or {}
                authored = [*statement.get("findings",()), *statement.get("qualitative_notes",())]
                shown = message.get("finding",{}).get("text")
                if shown:
                    handoff["verbatim_prefix_in_author_report"] = any(f["text"].startswith(shown) for f in authored)

        survey = rows("SELECT event_id, json_extract(payload_json,'$.through_event') AS through_event,"
                      "json_extract(payload_json,'$.arguments.query') AS query FROM events "
                      "WHERE run_id=? AND kind='research.catalog.surveyed' ORDER BY event_id",(rid,))
        jobs = rows("SELECT j.job_id,j.capability_id,j.capability_version,j.status,n.node_id,n.result_ref FROM jobs j "
                    "LEFT JOIN v6_nodes n USING(job_id) WHERE j.run_id=? ORDER BY j.rowid",(rid,))
        delayed_file = work / 'delayed.json'
        later = json.loads(delayed_file.read_text()) if delayed_file.is_file() and delayed_file.stat().st_size else None
        if later:
            atlas_digest = next(s["blob_sha256"] for s in original if s["title"] == "atlas.xml")
            first_article = first_body_read(db, atlas_digest)
            arrival = db.execute("""SELECT event_id,ts FROM events WHERE run_id=? AND kind='pi.attachment.registered'
                AND json_extract(payload_json,'$.name')='trend.xml' ORDER BY event_id LIMIT 1""",(rid,)).fetchone()
            later.update(first_main_article_body_read=dict(first_article) if first_article else None,
                         attachment_registered=dict(arrival) if arrival else None)
        preflight = work / 'admission-0.stderr'
        return {'goal':dict(goal), 'official_host':route_receipt(work),
                'first_admission_error':preflight.read_text().strip() if preflight.is_file() else None,
                'through_event':db.execute('SELECT max(event_id) FROM events WHERE run_id=?',(rid,)).fetchone()[0],
                'survey':survey,'turns':turns,'jobs':jobs,'materials':original, 'segments_by_reader':segments,'source_page_lists':pages,
                'delayed_ingress':later,'extractions':extracts,'independent_reviews':reviews,'reports':reports,
                'decisions':decisions,'report_handoffs':routes.get('handoffs',[])}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('work',type=Path)
    args = parser.parse_args()
    print(json.dumps(collect(args.work.resolve()), ensure_ascii=False, indent=2))
