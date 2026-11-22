"""Classify native admission timings by durable task transitions, leases, turn meters and official rollouts."""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path

from audit_workload import db_file, quantile, records_in, write_compressed
from run_workload import save, utc


def ns(iso):
    return round(datetime.fromisoformat(iso.replace('Z','+00:00')).timestamp()*1_000_000)*1000


def ms(delta):
    return round(delta/1_000_000,4)


def official_starts(home):
    starts={}
    for f in home.rglob('rollout-*.jsonl'):
        thread=None
        for r in records_in(f):
            d=r.get('payload',{})
            if r.get('type')=='session_meta':
                thread=d['id']
            if r.get('type')=='event_msg' and d.get('type')=='task_started':
                starts[thread,d['turn_id']]=ns(r['timestamp'])
    return starts


def collect(work,waves,control=False):
    sessions=official_starts(work/('codex' if control else 'codex-home')/'sessions')
    transitions=defaultdict(list)
    leases=defaultdict(list)
    roles={}
    starts={}
    run_wave={}
    deliveries=Counter()
    queues={}
    for wave in waves:
        state=work/f'state-{wave:02d}'
        with closing(db_file(state/'gx.db')) as conn:
            for r in conn.execute('SELECT run_id, inputs_json FROM runs'):
                spec=json.loads(r['inputs_json'])['inputs']['p0_spec']
                run_wave[r['run_id']]=(wave,spec['teams'][0]['team_id'])
            for r in conn.execute('SELECT task_id,role FROM tasks'):
                roles[r['task_id']]=r['role']
            for r in conn.execute("SELECT subject_id,ts,new_state FROM events WHERE kind='task.transition' ORDER BY event_id"):
                transitions[r['subject_id']].append((ns(r['ts']),r['new_state']))
            for r in conn.execute('SELECT task_id,attempt,issued_at,released_at,lease_id FROM leases ORDER BY issued_at'):
                leases[r['task_id']].append(dict(r))
            for r in conn.execute('SELECT d.event_id, d.method, d.state FROM p0_root_deliveries d'):
                deliveries[(r['event_id'].split(':',1)[0],r['method'],r['state'])]+=1
            for r in conn.execute('SELECT p.run_id,p.task_id,p.attempt,p.lease_id,p.thread_id,m.turn_id,m.event_id,b.created_at,b.settled_at,m.terminal_at '
                    'FROM p0_roots p JOIN p0_meter_native_turns m USING(thread_id) JOIN budget_reservations b ON b.reservation_ref=m.reservation_ref '
                    "WHERE m.event_id LIKE 'start:%'"):
                starts[r['task_id'],r['attempt']]={**dict(r),'reservation_ns':ns(r['created_at']),
                    'official_start_ns':sessions.get((r['thread_id'],r['turn_id']))}
    windows=defaultdict(list)
    transactions=defaultdict(list)
    for r in records_in(work/'metrics.jsonl'):
        if r['kind']=='db_transaction' and r.get('run_id') in run_wave and r.get('task_id'):
            end=r['observed_ns']
            transactions[r['task_id']].append({'started_ns':end-round(r['duration_ms']*1e6),'ended_ns':end,
                'duration_ms':r['duration_ms'],'lock_acquire_ms':r['lock_acquire_ms']})
        if r['kind']=='p0_dispatch' and r.get('run_id') in run_wave:
            end=r['observed_ns']; begin=end-round(r['duration_ms']*1e6)
            windows[r['task_id']].append({'task_id':r['task_id'],'run_id':r['run_id'],'wave':run_wave[r['run_id']][0],
                'lane':run_wave[r['run_id']][1],'role':roles[r['task_id']],'started_ns':begin,'ended_ns':end,
                'duration_ms':r['duration_ms']})
    errors=[]
    samples=[]
    for task,events in transitions.items():
        task_windows=windows.pop(task,[])
        ready=None
        claim_number=0
        held=leases[task]
        for at,status in events:
            if status=='READY':
                ready=at
            if status!='CLAIMED':
                continue
            claim_number+=1
            # SQLite's persisted UTC milliseconds truncate the actual event by under one millisecond.
            overlap=[w for w in task_windows if not w.get('operation') and w['started_ns']<=at+999_999 and at<=w['ended_ns']]
            exact=[w for w in overlap if w['started_ns']<=at<=w['ended_ns']]
            match=exact if exact else overlap
            if len(match)!=1 or ready is None or len(held)<claim_number:
                errors.append(f'claim join found {len(match)} dispatches or no preceding READY/lease for {task}#{claim_number}')
                continue
            w=match[0]; lease=held[claim_number-1]; native=starts.get((task,lease['attempt']),{})
            if len(overlap)!=1:
                errors.append(f'multiple dispatch windows at the millisecond claim edge for {task}#{claim_number}')
            if ns(lease['issued_at'])>at+1_000_000 or abs(ns(lease['issued_at'])-at)>20_000_000:
                errors.append(f'lease issue and claim materially diverge for {task}#{claim_number}')
            phase={'dispatch_start_to_claim_ms':ms(at-w['started_ns']),'claim_to_dispatch_return_ms':ms(w['ended_ns']-at),
                   'ready_to_claim_ms':ms(at-ready)}
            if native:
                reserv=native['reservation_ns']; official=native['official_start_ns']
                phase['claim_to_native_reservation_ms']=ms(reserv-at)
                if official is not None:
                    phase['native_reservation_to_official_start_ms']=ms(official-reserv)
                    phase['official_start_to_dispatch_return_ms']=ms(w['ended_ns']-official)
                else:
                    errors.append('official task_started missing '+native['turn_id'])
            else:
                errors.append('metered first native turn missing '+task)
            w.update(operation='new_claim',attempt=lease['attempt'],lease_id=lease['lease_id'],
                claim_ns=at,ready_ns=ready,meter_event=native.get('event_id'),thread_id=native.get('thread_id'),
                turn_id=native.get('turn_id'),reservation_ns=native.get('reservation_ns'),official_start_ns=native.get('official_start_ns'),
                subphases_ms=phase)
            queues[task,claim_number]=(ready,at)
            ready=None
        times=[pair[0] for pair in events]
        for w in task_windows:
            if 'operation' in w:
                continue
            end=w['ended_ns']; mid=(w['started_ns']+end)//2
            # Calls ending before the owning admission had no new lease or launched Turn.
            local=next(((k,to) for (tid,k),(fr,to) in queues.items() if tid==task and fr<=mid<to),None)
            if local:
                w.update(operation='ready_unadmitted',pending_attempt=local[0]); continue
            active=[l for l in held if ns(l['issued_at'])<=mid and (not l['released_at'] or mid<=ns(l['released_at']))]
            if len(active)==1:
                lease=active[0]
                w.update(operation='existing_lease_dispatch',attempt=lease['attempt'],lease_id=lease['lease_id']); continue
            index=bisect_right(times,mid)-1
            state=events[index][1] if index>=0 else 'no preceding transition'
            w.update(operation='other',state_at_midpoint=state)
        samples.extend(task_windows)
    # The only Team in each Run has one worker slot. Its previous native Turn can
    # keep that slot after the scientific submission released the Task lease.
    by_run=defaultdict(list)
    by_workspace=defaultdict(list)
    for w in samples:
        by_workspace[w['wave']].append(w)
        if w['operation']=='new_claim':
            by_run[w['run_id']].append(w)
    for lane in by_run.values():
        ordered=sorted(lane,key=lambda w:w['claim_ns'])
        first=ordered[0]
        relevant=[t for t in transactions[first['task_id']] if first['started_ns']<t['ended_ns']<=first['ended_ns'] and t['started_ns']<=first['claim_ns']]
        if relevant:
            writer=min(relevant,key=lambda t:t['started_ns'])
            first['first_same_task_writer']=writer
            first['subphases_ms']['cold_dispatch_start_to_first_writer_ms']=ms(max(0,writer['started_ns']-first['started_ns']))
        for earlier,later in zip(ordered,ordered[1:]):
            prev=starts.get((earlier['task_id'],earlier['attempt']),{})
            if prev.get('settled_at') and prev.get('terminal_at'):
                release=max(ns(prev['settled_at']),ns(prev['terminal_at']),ns(next(l['released_at'] for l in leases[earlier['task_id']] if l['lease_id']==earlier['lease_id'])))
                eligible=max(later['ready_ns'],release)
                later['previous_turn_id']=prev['turn_id']
                later['previous_slot_release_ns']=release
                later['subphases_ms']['ready_elapsed_with_previous_team_slot_occupied_ms']=ms(max(0,release-later['ready_ns']))
                later['subphases_ms']['ready_and_previous_team_slot_released_to_claim_ms']=ms(later['claim_ns']-eligible)
                # Each paired workspace has one public synchronous coordinator.
                # Count only observed sibling dispatch intervals overlapping this wait.
                overlaps=[]
                for sibling in by_workspace[later['wave']]:
                    if sibling['run_id']==later['run_id']:
                        continue
                    begin=max(eligible,sibling['started_ns']);end=min(later['claim_ns'],sibling['ended_ns'])
                    if end>begin:
                        overlaps.append({'task_id':sibling['task_id'],'operation':sibling['operation'],
                            'dispatch_end_ns':sibling['ended_ns'],'from_ns':begin,'to_ns':end})
                covered=covered_before_own=0
                edge=eligible
                own_start=max(eligible,later['started_ns'])
                for interval in sorted(overlaps,key=lambda o:o['from_ns']):
                    begin=max(edge,interval['from_ns'])
                    covered+=max(0,interval['to_ns']-begin)
                    covered_before_own+=max(0,min(own_start,interval['to_ns'])-begin)
                    edge=max(edge,interval['to_ns'])
                later['sibling_dispatch_overlap']=overlaps
                later['subphases_ms']['eligible_wait_overlapping_sibling_dispatch_ms']=ms(covered)
                later['subphases_ms']['eligible_wait_outside_sibling_dispatch_ms']=ms(later['claim_ns']-eligible-covered)
                later['subphases_ms']['eligible_before_own_dispatch_outside_sibling_ms']=ms(own_start-eligible-covered_before_own)
                later['subphases_ms']['follow_on_own_dispatch_start_to_claim_ms']=ms(later['claim_ns']-own_start)
                # Only outer BEGIN IMMEDIATE transactions are emitted, using the same Task
                # metric identity. The crossing writer continues after the claim edge.
                a,c,b=later['started_ns'],later['claim_ns'],later['ended_ns']
                before=[]
                for transaction in transactions[later['task_id']]:
                    s,e=transaction['started_ns'],transaction['ended_ns']
                    if a<e<=b and s<=c:
                        before.append({**transaction,'relation':'finished_before_claim' if e<=c else 'crosses_claim',
                            'clip_start_ns':max(a,s),'clip_end_ns':min(e,c)})
                later['preclaim_writer_intervals']=before
                writer=0;edge=a
                for tx in sorted(before,key=lambda v:v['clip_start_ns']):
                    writer+=max(0,tx['clip_end_ns']-max(edge,tx['clip_start_ns']))
                    edge=max(edge,tx['clip_end_ns'])
                later['subphases_ms']['follow_on_preclaim_writer_ms']=ms(writer)
                later['subphases_ms']['follow_on_preclaim_outside_writer_ms']=ms(c-a-writer)
                later['subphases_ms']['follow_on_preclaim_finished_writer_ms']=round(sum(t['duration_ms'] for t in before if t['relation']=='finished_before_claim'),4)
                later['subphases_ms']['follow_on_preclaim_lock_acquire_ms']=round(sum(t['lock_acquire_ms'] for t in before),4)
    if windows:
        errors.append('GX dispatch metrics without task transitions '+str(list(windows)))
    return samples,errors,[{'event':k[0],'method':k[1],'delivery_state':k[2],'n':n} for k,n in deliveries.items()]


def summary(samples):
    groups=defaultdict(list)
    for row in samples:
        groups['all_raw_ms'].append(row['duration_ms'])
        groups[row['operation']+'_ms'].append(row['duration_ms'])
        if row['operation']=='new_claim':
            groups[f"claim_{row['role']}_attempt_{row['attempt']}_ms"].append(row['duration_ms'])
        for k,v in row.get('subphases_ms',{}).items():
            groups[k].append(v)
    return {k:quantile(v) for k,v in sorted(groups.items())}


def commonmix(reference,weights):
    pool=defaultdict(list)
    for w in reference:
        pool[w['operation']].append(w['duration_ms'])
    if set(pool)!=set(weights):
        return None
    combined=sorted((v,weights[k]/(len(vals)*sum(weights.values()))) for k,vals in pool.items() for v in vals)
    cumulative=0
    for val,weight in combined:
        cumulative+=weight
        if cumulative>=0.95-1e-12:
            return round(val,4)


def compare(arms):
    counts={k:Counter(i['operation'] for i in v) for k,v in arms.items()}
    return {version+'_timings_with_'+mixture+'_operation_mix_p95_ms':commonmix(arms[version],counts[mixture])
        for version in ('baseline','candidate') for mixture in ('baseline','candidate')}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline',type=Path,required=True);p.add_argument('--candidate',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);p.add_argument('--samples',type=Path)
    p.add_argument('--waves',type=int,default=13);p.add_argument('--control',action='store_true')
    a=p.parse_args(); issues=[]; full={}; lease_counts={}; groups={}
    for arm,work in (('baseline',a.baseline),('candidate',a.candidate)):
        trialsets=sorted(work.glob('*-'+arm)) if a.control else [work]
        found=[]; events=[]
        for trial in trialsets:
            raw,errors,deliveries=collect(trial,range(1 if a.control else a.waves),a.control)
            for row in raw:
                if a.control:
                    row['trial']=trial.name; row['wave']=int(trial.name.split('-')[0]); row['instrumented']=row['wave']>=2
            found.extend(raw);events.extend(deliveries);issues.extend(errors)
        full[arm]=found; lease_counts[arm]=events
        patterns=Counter((r['role'],sum(t['relation']=='finished_before_claim' for t in r['preclaim_writer_intervals']),
            sum(t['relation']=='crosses_claim' for t in r['preclaim_writer_intervals'])) for r in found if 'preclaim_writer_intervals' in r)
        groups[arm]={'all':summary(found),'per_wave':{str(w):summary([r for r in found if r['wave']==w]) for w in sorted({r['wave'] for r in found})},
            'operations':dict(Counter(r['operation'] for r in found)),'follow_on_writer_counts_by_role':[
                {'role':role,'completed_before_claim':done,'crossing_claim':cross,'claims':count} for (role,done,cross),count in sorted(patterns.items())]}
    output={'captured':utc(),'scope':'fixed loopback Responses control' if a.control else 'matched completed official OAuth arrivals',
        'units':'milliseconds, empirical nearest rank', 'groups':groups,'receipt_join_issues':issues,'native_delivery_receipts':lease_counts,
        'standardization':compare(full), 'definitions':{'new_claim':'dispatch interval containing the durable READY to CLAIMED edge plus matching original lease; telemetry end minus elapsed reconstructs start',
        'ready_unadmitted':'task remained READY at midpoint before its actual claim; return reason unavailable from real GX metrics; control timers record HoldActive explicitly',
        'existing_lease_dispatch':'identity-matched dispatch on an already live original lease; count separately from official resume-prefixed turn deliveries',
        'ready_and_previous_team_slot_released_to_claim_ms':'noninitial same-Team claim minus the later of this READY timestamp and the prior Task lease release/native terminal/budget settlement; each Team has one slot and only one other single-slot Team shares its two-slot workspace',
        'eligible_wait_overlapping_sibling_dispatch_ms':'union of actually timed dispatch intervals on the other Run of the same single-coordinator workspace while the measured claim was eligible',
        'eligible_wait_outside_sibling_dispatch_ms':'eligible interval with observed synchronous other-Run dispatch overlap subtracted; other work or waits inside this residual are not classified',
        'eligible_before_own_dispatch_outside_sibling_ms':'eligible to start of own follow-on dispatch, excluding any overlapping synchronous sibling Run dispatches',
        'follow_on_own_dispatch_start_to_claim_ms':'admitted follow-on dispatch start (or eligibility if later) to the durable claim, including actual GX claim preparation',
        'follow_on_preclaim_writer_ms':'union of same-Task outer GX writer time clipped to follow-on dispatch start through claim, including lock acquisition. Metrics identify no individual SQL/Python call. The metric end timestamp is emitted after transaction-end callbacks, which can offset reconstruction; claim event is persisted to milliseconds.',
        'follow_on_preclaim_outside_writer_ms':'remaining observed own preclaim interval after excluding that clipped writer union; includes Python, other IO, metric emissions and timing uncertainty',
        'cold_dispatch_start_to_first_writer_ms':'first admitted Root dispatch per Run through the first outer GX writer under the same Task identity. Includes lazy public-driver native/worker tool initialization; no production timer separates these inside this interval.',
        'claim_to_native_reservation_ms':'durable claim until GX funding reservation for same native Turn, includes native thread materialization',
        'native_reservation_to_official_start_ms':'GX budget reservation to Codex official task_started message; accurate to persisted millisecond precision',
        'official_start_to_dispatch_return_ms':'official task_started to GX dispatch timer end; native start and receipt processing run asynchronously',
        'standardization':'nearest weighted empirical p95 retaining same operation-specific duration samples, replacing only operation frequencies; missing stratum yields null'}}
    if a.samples:
        write_compressed(a.samples,({'arm':name,**r} for name,vals in full.items() for r in vals))
    save(a.output,output)
    print(json.dumps({'groups':{k:{'operations':v['operations'],'claims':v['all'].get('new_claim_ms'),'raw':v['all'].get('all_raw_ms'),'queue':v['all'].get('ready_to_claim_ms')} for k,v in groups.items()},'errors':issues,'swapped':output['standardization']},indent=2))
    return bool(issues)


if __name__=='__main__':
    raise SystemExit(main())
