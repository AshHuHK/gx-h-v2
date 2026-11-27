"""Read original GX and official Codex receipts for a question-only literature study."""
import argparse
from collections import Counter
from datetime import datetime
from itertools import combinations
import json
from pathlib import Path
import sqlite3

from adapters.scientific.publication_coverage import report
from gx_core.p0 import manifest, research_routes
from acceptance.science_capabilities.field_collaboration import route_receipt


def native_turns(work, conn, rid):
    assignments = {r[0]: r[1] for r in conn.execute(
        "SELECT r.thread_id,t.scope FROM p0_roots r JOIN v6_decision_tasks t USING(task_id) WHERE r.run_id=?", (rid,))}
    accepted = {(r[0],r[1]) for r in conn.execute(
        "SELECT thread_id,turn_id FROM p0_root_deliveries WHERE run_id=? AND method='turn/start' AND state='acknowledged'", (rid,))}
    turns = {}
    for path in (work/'codex/sessions').rglob('*.jsonl'):
        thread = None
        for line in path.open(encoding='utf-8'):
            item = json.loads(line)
            body = item.get('payload', {})
            if item['type'] == 'session_meta':
                thread = body['id']
            if thread not in assignments or item['type'] != 'event_msg':
                continue
            turn = body.get('turn_id')
            if (thread,turn) not in accepted:
                continue
            if body['type'] == 'task_started':
                turns[turn] = dict(id=turn, thread=thread, scope=assignments[thread], start=item['timestamp'])
            if turn in turns and body['type'] in ('task_complete','turn_aborted'):
                turns[turn].update(end=item['timestamp'], outcome=body['type'])
    return sorted(turns.values(), key=lambda t:t['start'])


def overlap(intervals):
    overlaps = []
    for a,b in combinations(intervals, 2):
        if a['scope'] == b['scope'] or not all(x.get('start') and x.get('end') for x in (a,b)):
            continue
        start = max(datetime.fromisoformat(x['start'].replace('Z','+00:00')) for x in (a,b))
        end = min(datetime.fromisoformat(x['end'].replace('Z','+00:00')) for x in (a,b))
        if end > start:
            overlaps.append({"pair":[a['id'],b['id']],"scopes":[a['scope'],b['scope']],
                             "overlap_seconds":round((end-start).total_seconds(),3)})
    return sorted(overlaps,key=lambda o:o["overlap_seconds"],reverse=True)


def collect(work):
    work = work.resolve()
    db = work/'state/gx.db'
    with sqlite3.connect(db.resolve().as_uri()+'?mode=ro',uri=True) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")
        goal, = c.execute('SELECT run_id,goal,status FROM v6_goals')
        rid = goal['run_id']
        scopes = {r[0]:r[1] for r in c.execute("SELECT r.thread_id,t.scope FROM p0_roots r JOIN v6_decision_tasks t USING(task_id) WHERE r.run_id=?",(rid,))}
        turns = native_turns(work,c,rid)
        jobs = []
        for row in c.execute("""SELECT j.job_id AS id, j.capability_id,j.capability_version,j.status,j.outcome_json,COALESCE(m.scope,'run') AS scope
               FROM jobs j JOIN v6_nodes node ON node.run_id=j.run_id AND node.job_id=j.job_id
               LEFT JOIN v62_modules m ON m.run_id=j.run_id AND m.branch_id=node.branch_id
               WHERE j.run_id=? ORDER BY j.rowid""",(rid,)):
            j = dict(row)
            outcome = json.loads(j.pop("outcome_json"))
            if outcome.get("failure"):
                j["failure"] = outcome["failure"]
                j["failure_log"] = next((a for a in outcome.get("outputs", []) if a.get("output_name") == "stderr"), None)
            ev = list(c.execute("SELECT new_state,ts FROM events WHERE run_id=? AND subject_id=? AND kind='job.transition' ORDER BY event_id",(rid,j['id'])))
            j.update(start=next((r[1] for r in ev if r[0]=='RUNNING'),None),
                     end=next((r[1] for r in ev if r[0] in ('SUCCEEDED','FAILED','CANCELLED','TIMED_OUT')),None))
            m = manifest.read_manifest(c,j['id'],missing_ok=True)
            if m:
                j['inputs'] = m['admission']['inputs']
                j['pinned_method'] = m['admission']['capability']
                j['source_digest'] = m['admission']['source'].get('sha256')
                j['completion_receipt'] = m.get('manifest_hash')
                j['method_parameters'] = m['admission'].get('parameters')
                if m.get('completion'):
                    j['outputs'] = [{k:p[k] for k in ('output_name','artifact_ref','sha256')} for p in m['completion']['outputs']]
            jobs.append(j)
        survey=[]; opened=[]; read=Counter(); reading_calls=[]; exact_tools=Counter()
        for r in c.execute("""SELECT n.request_json,c.result_json,n.call_id,n.thread_id,n.turn_id
            FROM p0_v6_native_calls n JOIN p0_root_calls c USING(thread_id,turn_id,call_id)
            JOIN p0_roots root ON root.thread_id=c.thread_id WHERE root.run_id=? ORDER BY c.rowid""",(rid,)):
            req,ans = json.loads(r[0]),json.loads(r[1]); tool=req['tool']; args=req['arguments']; scope=scopes[r['thread_id']]
            exact_tools[(scope,tool)]+=1
            if tool=='gx_query_catalog':
                items=[(idx+1,i['reference']) for idx,i in enumerate(ans.get('items',())) if i.get('kind')=='skill']
                if items:
                    survey.append(dict(scope=scope,call_id=r['call_id'],turn=r['turn_id'],skills=items))
                spec=ans.get('spec',{})
                if spec.get('kind')=='skill':
                    opened.append(dict(scope=scope,call_id=r['call_id'],turn=r['turn_id'],skill=args.get('reference'),version_digest=spec.get('digest')))
            if tool=='gx_read_source' and ans.get('receipt_id'):
                read[(scope,ans['source_ref'])]+=1
                reading_calls.append(dict(scope=scope,turn=r['turn_id'],receipt_id=ans['receipt_id'],source_ref=ans['source_ref'],segment_id=ans['segment_id'],text_sha256=ans['text_sha256']))
        decisions=[dict(scope=r['scope'],event_id=json.loads(r['receipt_json']).get('event_id'),ref=r['decision_ref'],
                        rationale=json.loads(r['request_json'])['decision']['rationale'],
                        operations=[o.get('op') for o in json.loads(r['request_json'])['decision']['ops']],
                        evidence_refs=json.loads(r['request_json'])['decision']['evidence_refs'])
                 for r in c.execute('select d.*,t.scope from v6_decisions d join v6_decision_tasks t on t.task_id=d.task_id where d.run_id=? order by d.rowid',(rid,))]
        modules=[dict(r) for r in c.execute("SELECT module_instance_id,scope,module_id,status,focus,expected_delivery FROM v62_modules WHERE run_id=? ORDER BY rowid",(rid,))]
        handoffs=[dict(event_id=r['event_id'],kind=r['kind'],payload={k:v for k,v in json.loads(r['payload_json']).items()
                                                                    if k in ('module_instance_id','module_instance_ids','status','author','report_ref','report_sha256')})
                 for r in c.execute("SELECT event_id,kind,payload_json FROM events WHERE run_id=? AND kind IN ('v62.module.reported','v62.module.released','v62.module.wake') ORDER BY event_id",(rid,))]
        run=c.execute('SELECT created_at,inputs_json FROM runs WHERE run_id=?',(rid,)).fetchone()
        inputs=json.loads(run['inputs_json'])['inputs']['p0_spec']
        model_settings=[dict(r) for r in c.execute('SELECT DISTINCT provider,model,effort FROM p0_roots WHERE run_id=?',(rid,))]
        def latency(ids):
            end=min((j['end'] for j in jobs if j['id'] in ids and j['status']=='SUCCEEDED' and j.get('end')),default=None)
            if end:
                return round((datetime.fromisoformat(end.replace('Z','+00:00')) -
                              datetime.fromisoformat(run['created_at'].replace('Z','+00:00'))).total_seconds(),3)
        original_job_ids={j['id'] for j in jobs if any(o['output_name']=='article.xml' for o in j.get('outputs',()))}
        routes = research_routes.routes(c, rid, work/'state')
        rejected = [dict(event_id=row['event_id'], **json.loads(row['payload_json'])) for row in c.execute(
            "SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v624.decision.rejected' ORDER BY event_id",
            (rid,))]
        return dict(goal=dict(goal), given_materials=inputs['input_files'], admitted_at=run['created_at'],
                    official_login=route_receipt(work), report_delivery_receipts=routes['handoffs'],
                    model_settings=model_settings, first_successful_job_seconds=latency({j['id'] for j in jobs}),
                    first_verified_article_job_seconds=latency(original_job_ids),
                    turns=turns, jobs=jobs, modules=modules, reports=handoffs,
                    located_reads=[dict(scope=s,ref=ref,n=n) for (s,ref),n in read.items()],
                    received_paragraphs=reading_calls, listed_skills=survey, exact_opened_skills=opened,
                    native_tool_counts=[dict(scope=s,tool=t,n=n) for (s,t),n in sorted(exact_tools.items())],
                    publication_coverage=report(c,rid), effective_decisions=decisions, rejected_attempts=rejected,
                    overlap_between_researchers=dict(native_turns=overlap([t for t in turns if t['scope'].startswith('module:')]),
                                                      cpu_jobs=overlap([j for j in jobs if j['scope'].startswith('module:')])),
                    closed_event=next((r[0] for r in c.execute("select event_id from events where run_id=? and kind='v6.run.closed'",(rid,))),None))


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('work',type=Path)
    args=p.parse_args()
    print(json.dumps(collect(args.work),ensure_ascii=False,indent=2))
