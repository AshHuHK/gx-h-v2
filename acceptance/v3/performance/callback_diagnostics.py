"""Match GX callbacks, official arguments and product verdicts, then measure outer writer coverage."""
import argparse
from bisect import bisect_left,bisect_right
from collections import Counter,defaultdict
from contextlib import closing
import json
from pathlib import Path

from audit_workload import db_file,quantile,records_in,write_compressed
from dispatch_phases import ms,ns
from run_workload import save,sha,utc


def encoded(obj):
    return len(json.dumps(obj,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())


def official_calls(sessions,known):
    calls=defaultdict(list)
    for f in sorted(sessions.rglob('rollout-*.jsonl')):
        thread=None
        for row in records_in(f):
            payload=row.get('payload',{})
            if row['type']=='session_meta':
                thread=payload['id']
                if thread not in known:break
            item=payload.get('item') or {}
            if row['type']!='event_msg' or payload.get('type')!='item_completed' or item.get('type')!='DynamicToolCall':continue
            tool=item['tool'];args=item.get('arguments') or {};turn=payload.get('turn_id');reply={}
            for content in item.get('content_items') or []:
                try:reply=json.loads(content.get('text',''))
                except (ValueError,TypeError):continue
                if isinstance(reply,dict):break
            if not isinstance(reply,dict):reply={}
            calls[thread,turn,tool].append({'call_id':item['id'],'official_success':item.get('success'),
                'official_completed_ns':int(payload['completed_at_ms'])*1_000_000 if payload.get('completed_at_ms') else ns(row['timestamp']),
                'canonical_argument_bytes':encoded(args),'product_bytes':encoded(args['product']) if 'product' in args else None,
                'submission_id':reply.get('submission_id'),'read_ref':args.get('ref') if tool=='gx_read_artifact' else None,
                'grid_path_id':args.get('path_id') if tool=='gx_grid_measurement' else None,
                'tool_error':str(reply.get('error','')).partition(':')[0] or None})
    return calls


def trial(work,waves,control):
    known={};artifacts={};jobs={};submissions={};runs={};errors=[]
    for wave in waves:
        with closing(db_file(work/f'state-{wave:02d}'/'gx.db')) as c:
            for r in c.execute('SELECT run_id,inputs_json FROM runs'):
                runs[r['run_id']]=(wave,json.loads(r['inputs_json'])['inputs']['p0_spec']['teams'][0]['team_id'])
            for r in c.execute('SELECT thread_id,task_id,lease_id,attempt,role FROM p0_roots WHERE thread_id IS NOT NULL'):
                known[r['thread_id']]=dict(r)
            for r in c.execute('SELECT artifact_ref,kind,bytes FROM artifacts'):
                artifacts[r['artifact_ref']]=(r['kind'],r['bytes'])
            for r in c.execute('SELECT submission_id,lease_id,task_id,attempt,result,created_at FROM submissions'):
                submissions[r['submission_id']]=dict(r)
            for r in c.execute('SELECT thread_id,call_id,turn_id,result_json FROM p0_root_calls'):
                ans=json.loads(r['result_json'])
                if isinstance(ans,dict):jobs[r['thread_id'],r['call_id']]=(r['turn_id'],ans.get('submission_id'))
    completed=official_calls(work/('codex' if control else 'codex-home')/'sessions',known)
    callbacks=defaultdict(list);tx=[]
    # Intervals on different arrival workspaces never overlap in these supplies of paid work.
    # Filter unscoped ambient transactions to each selected driver's active epoch below.
    for r in records_in(work/'metrics.jsonl'):
        if r['kind']=='db_transaction':
            end=r['observed_ns'];tx.append({'end':end,'start':end-round(r['duration_ms']*1e6),'ms':r['duration_ms'],'lock_ms':r['lock_acquire_ms']})
        if r['kind']=='p0_callback' and r.get('run_id') in runs:
            callbacks[r['thread_id'],r['turn_id'],r.get('tool')].append(r)
    tx.sort(key=lambda r:r['end']);ends=[q['end'] for q in tx];covered_tx=set();output=[];ranges=defaultdict(list)
    for key,metrics in callbacks.items():
        thread,turn,tool=key;events=sorted(completed.pop(key,[]),key=lambda r:r['official_completed_ns']);metrics.sort(key=lambda r:r['observed_ns'])
        if len(events)!=len(metrics):
            errors.append(f'official/GX callback cardinality {thread}/{turn}/{tool}: {len(events)} vs {len(metrics)}');continue
        for r,call in zip(metrics,events):
            end=r['observed_ns'];begin=end-round(r['duration_ms']*1e6);wave,lane=runs[r['run_id']];ranges[wave].append((begin,end));root=known[thread]
            row={'wave':wave,'lane':lane,'run_id':r['run_id'],'thread_id':thread,'turn_id':turn,'task_id':root['task_id'],'role':root['role'],
                 'attempt':root['attempt'],'tool':tool,'callback_start_ns':begin,'callback_end_ns':end,'callback_ms':r['duration_ms'],**call}
            lag=ms(call['official_completed_ns']-end);row['native_ack_lag_ms']=lag
            receipt=jobs.get((thread,call['call_id']));sub_id=call['submission_id'];sub=submissions.get(sub_id)
            if receipt and receipt!=(turn,sub_id):errors.append('GX call receipt identity/result mismatch '+call['call_id'])
            if tool=='gx_submit_product':
                if sub and (sub['lease_id'],sub['task_id'],sub['attempt'])!=(root['lease_id'],root['task_id'],root['attempt']):
                    errors.append('submitted product belongs to different owner '+call['call_id'])
                if sub and not receipt:errors.append('formal product lacks native GX call receipt '+call['call_id'])
                row['product_outcome']=sub['result'] if sub else ('TOOL_REFUSED' if call['official_success'] is False else 'NO_FORMAL_SUBMISSION')
                if sub:
                    row['product_created_ns']=ns(sub['created_at'])
                    if not begin-1_000_000<=row['product_created_ns']<=end+1_000_000:
                        errors.append('formal submission timestamp outside its GX callback '+call['call_id'])
            if call['read_ref']:
                row['read_artifact_kind'],row['read_artifact_bytes']=artifacts.get(call['read_ref'],(None,None))
            # One coordinator invokes the original callback synchronously. GX does not write
            # thread identity to outer SQL measurements here; use their completion order and
            # containment, retaining the count and every interval rather than guessing an SQL name.
            first,last=bisect_right(ends,begin),bisect_right(ends,end)
            matches=[];edge=begin;covered=0
            for ix in range(first,last):
                tr=tx[ix]
                if tr['start']<begin-1_000_000:continue
                matches.append(tr);covered_tx.add(ix)
            for tr in sorted(matches,key=lambda t:t['start']):
                covered+=max(0,min(end,tr['end'])-max(begin,edge,tr['start']));edge=max(edge,tr['end'])
            row.update(writer_count=len(matches),writer_ms=ms(covered),outside_writer_ms=ms(end-begin-covered),
                       writer_lock_ms=round(sum(t['lock_ms'] for t in matches),4),writer_samples=matches)
            output.append(row)
    if any(completed.values()):errors.append('official call has no GX callback measurement')
    ambient=[]
    for wave,bounds in ranges.items():
        lo=min(r[0] for r in bounds);hi=max(r[1] for r in bounds)
        for i in range(bisect_left(ends,lo),bisect_right(ends,hi)):
            if i in covered_tx:continue
            t=tx[i]
            if any(t['start']<b and a<t['end'] for a,b in bounds):continue
            ambient.append({'wave':wave,**t})
    return output,ambient,errors


def stats(rows):
    return {k:quantile(r[k] for r in rows if r.get(k) is not None) for k in ('callback_ms','canonical_argument_bytes','product_bytes','read_artifact_bytes','writer_ms','writer_lock_ms','outside_writer_ms','native_ack_lag_ms')}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--baseline',required=True,type=Path);p.add_argument('--candidate',required=True,type=Path)
    p.add_argument('--control',action='store_true');p.add_argument('--waves',type=int,default=13);p.add_argument('--output',required=True,type=Path);p.add_argument('--samples',type=Path)
    a=p.parse_args();groups={};all_rows=[];issues=[]
    arms=[('baseline',a.baseline),('candidate',a.candidate)]
    if a.control and any(a.candidate.glob('*-candidate_quiet')):arms.append(('candidate_quiet',a.candidate))
    for arm,w in arms:
        runs=sorted(w.glob('*-'+arm)) if a.control else [w];allarm=[];ambient=[]
        for work in runs:
            out,other,err=trial(work,range(1 if a.control else a.waves),a.control);issues.extend(err)
            if a.control:
                for r in out+other:r['wave']=int(work.name.split('-')[0]);r['trial']=work.name
            allarm.extend(out);ambient.extend(other)
        for r in allarm:r['arm']=arm
        all_rows.extend(allarm)
        tools=defaultdict(list);strata=defaultdict(list);by_read=defaultdict(list);submit_counts=Counter();by_wave=defaultdict(list)
        for r in allarm:
            tools[r['tool']].append(r);by_wave[r['wave']].append(r)
            if r['tool']=='gx_submit_product':
                strata[r['role']+'/'+r['product_outcome']].append(r);submit_counts[r['role'],r['product_outcome'],r['writer_count']]+=1
            if r['tool']=='gx_read_artifact':by_read[r.get('read_artifact_kind') or '(unresolved)'].append(r)
        groups[arm]={'all_callbacks':stats(allarm),'by_tool':{k:stats(v) for k,v in sorted(tools.items())},'submit_by_role_and_verdict':{k:stats(v) for k,v in sorted(strata.items())},
            'submit_writer_counts':[{'role':k[0],'outcome':k[1],'outer_writers':k[2],'n':v} for k,v in sorted(submit_counts.items())],
            'artifact_reads_by_kind':{k:stats(v) for k,v in sorted(by_read.items())},'per_wave':{str(k):{'all':stats(v),'submit':stats([r for r in v if r['tool']=='gx_submit_product'])} for k,v in sorted(by_wave.items())},
            'noncallback_sql_during_callback_epoch_ms':quantile(r['ms'] for r in ambient),'noncallback_sql_lock_ms':quantile(r['lock_ms'] for r in ambient)}
    report={'captured':utc(),'study':'direct interleaved identical script control' if a.control else 'matched actual paid arrivals','groups':groups,'issues':issues,
        'matching':'native completed dynamic calls, ordered within same bound thread/turn/tool, joined to GX metric timestamps and to call ID + GX submission/lease. Argument/product sizes are canonical compact UTF-8 JSON. A formal REJECTED submission can have a successful tool transport.',
        'sql_attribution':'outer SQLite transactions share a clock/metrics stream but callback-context writers have no persisted thread ID. Containment and completion order on the synchronous GX process identify likely writer residency. Nested savepoints are not separately timed. Timings do not separate filesystem, serialization, CPU and writeback inside the writer.',
        'ambient':'other outer transactions entirely outside any callback, within each paired workspace’s first-to-last callback epoch; operation mixes can vary. Artifact reads report input byte lengths by actual classified artifact kind.'}
    if a.samples:write_compressed(a.samples,all_rows);report['sample_sha256']=sha(a.samples)
    save(a.output,report);print(json.dumps({'issues':issues,'submit_ms':{s:r['by_tool'].get('gx_submit_product',{}).get('callback_ms') for s,r in groups.items()},'submit_strata':{s:{k:v['callback_ms'] for k,v in r['submit_by_role_and_verdict'].items()} for s,r in groups.items()}},indent=2));return bool(issues)


if __name__=='__main__':raise SystemExit(main())
