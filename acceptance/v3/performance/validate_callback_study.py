"""Verify fixed tool actions, scientific outcomes, payload sizes and time separation of the callback study."""
import argparse
from collections import Counter
from pathlib import Path
import gzip
import json

from audit_workload import quantile,records_in,stamp
from run_workload import save,sha,utc


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--control',required=True,type=Path);p.add_argument('--paid',required=True,type=Path);a=p.parse_args()
    control=json.loads((a.control/'control.json').read_bytes());rows=control['trials'];raw=list(records_in(a.paid/'commands.jsonl'));start={r['label']:r['at'] for r in raw if 'command' in r};end={r['label']:r['ended'] for r in raw if 'exit_code' in r};
    latest=stamp(utc());acquired=[]
    for name,t in start.items():
        if name.startswith('start-') and name.endswith('-west'):
            wave=int(name.split('-')[1]);fin=end.get(f'drive-{wave:02d}');acquired.append((wave,stamp(t),stamp(fin) if fin else latest))
    overlap=[]
    for trial in rows:
        for wave,begin,finish in acquired:
            time=max(0,min(finish,stamp(trial['ended']))-max(begin,stamp(trial['started'])))
            if time:overlap.append({'sequence':trial['sequence'],'paid_wave':wave,'seconds':round(time,4)})
    disk=list(records_in(a.control/'ambient-disk.jsonl')); groups={}
    arms=sorted({r['arm'] for r in rows})
    for arm in arms:
        trials=[r for r in rows if r['arm']==arm]; probes=[v for d in disk if d['arm']==arm for v in d['samples']];
        payloads=Counter((x['role'],x['attempt'],x['canonical_argument_bytes'],bool(x['fixture_dropped_last_finding'])) for t in trials for x in t['submit_payloads']);actions=Counter();outcomes=Counter()
        for t in trials:actions.update(t['peer_tool_actions']);outcomes.update(t['reviews'])
        groups[arm]={'trials':len(trials),'all_ordinary_cli':all(not t['instrumented'] for t in trials),'all_valid':all(t['valid'] for t in trials),
            'all_loopback':all(t['all_connections_loopback'] for t in trials),'responses':sum(t['peer_responses'] for t in trials),
            'completed_runs':sum(t['runs'].get('COMPLETED',0) for t in trials),'completed_turns':sum(t['turns'].get('completed',0) for t in trials),
            'succeeded_cpu_jobs':sum(t['jobs'].get('SUCCEEDED',0) for t in trials),'peer_actions':actions,'scientific_outcomes':outcomes,
            'payload_sizes':[{'role':k[0],'attempt':k[1],'bytes':k[2],'fixture_removed_last_finding':k[3],'n':z} for k,z in sorted(payloads.items())],
            'ambient_ms':{k:quantile(r[k] for r in probes) for k in ('warm_read_ms','fresh_create_write_fsync_close_ms')}}
    b,c=groups['baseline'],groups['candidate'];fields=['trials','completed_runs','completed_turns','succeeded_cpu_jobs','scientific_outcomes','payload_sizes']
    expected_trials=len(control['order']); replay=control.get('replayed_measurement_sequence');history_summary=None
    if not replay:fields+=['responses','peer_actions']
    checks={'all_scheduled_trials':len(rows)==expected_trials and control['result']=='PASS','identical_observed_groups':all(b[k]==g[k] for g in groups.values() for k in fields),
        'all_direct_public_cli':all(r['all_ordinary_cli'] and r['all_valid'] and r['all_loopback'] for r in groups.values()),'zero_overlap_with_paid_cli':not overlap,
        'ambient_samples_all_trials':len({d['sequence'] for d in disk})==expected_trials and len({d['reference_sha256'] for d in disk})==1 and all(len(d['samples'])==4 for d in disk)}
    if replay:
        source=Path(replay['source']);actual=Counter();errors=[]
        plans={(r['lane'],r['role'],r['attempt']):[(e['phase'],e['path_id'],e['success']) for e in r['measurements']]
               for r in json.loads(source.read_bytes())['ordered_worker_actions']}
        with gzip.open(a.control/'callback-samples.jsonl.gz','rt') as f:
            by_turn={}
            for r in map(json.loads,f):by_turn.setdefault((r['arm'],r['trial'],r['thread_id'],r['turn_id']),[]).append(r)
        for (arm,trial,thread,turn),events in by_turn.items():
            events.sort(key=lambda r:r['callback_end_ns']);submits=[r for r in events if r['tool']=='gx_submit_product'];submitted=False;history=[]
            if len(submits)!=1:errors.append(trial+' unexpected submit count');continue
            first=submits[0]
            for r in events:
                if r['tool']=='gx_submit_product':submitted=True
                if r['tool']=='gx_grid_measurement':
                    phase='after_submit' if submitted else 'before_submit';history.append((phase,r['grid_path_id'],r['official_success']));actual[(arm,phase,r['official_success'])]+=1
            expected=plans[first['lane'],first['role'],first['attempt']] if arm=='candidate' else []
            if history!=expected:errors.append(trial+' differs from original paid measurement order on '+thread)
        hashed={arm:Counter((r['lane'],r['role'],r['attempt'],r['portable_product_sha256']) for trial in rows if trial['arm']==arm for r in trial['submit_payloads']) for arm in arms}
        common=dict(c['peer_actions']);additional=common.pop('gx_grid_measurement',0)
        quiet=groups['candidate_quiet']
        checks.update(source_sequence_digest=sha(source)==replay['source_sha256'],matched_semantic_product_hashes=all(v==hashed['baseline'] for v in hashed.values()),
            only_grid_measurement_differs_in_peer_actions=common==b['peer_actions'] and c['responses']-b['responses']==additional,
            corrected_quiet_matches_baseline_actions=quiet['peer_actions']==b['peer_actions'] and quiet['responses']==b['responses'],
            exact_per_turn_paid_measurement_order=not errors and len(by_turn)==sum(g['completed_turns'] for g in groups.values()))
        history_summary={'per_phase_counts':[{'arm':k[0],'phase':k[1],'success':k[2],'n':n} for k,n in sorted(actual.items())],
            'issues':errors,'original_paid_source_sha256':replay['source_sha256'],'product_sha256_by_role_lane_attempt':[
                {'lane':k[0],'role':k[1],'attempt':k[2],'sha256':k[3],'count_per_arm':n} for k,n in sorted(hashed['baseline'].items())],
            'product_normalization':'Only store-generated artifact/Job/evaluation IDs are replaced with their explicit scientific roles. All numerical answers, findings, complete reason strings, text and remaining payload fields are byte-hashed unchanged.'}
    save(a.control/'validity.json',{'captured':utc(),'checks':checks,'groups':groups,'overlap_with_paid_windows':overlap,
        'overlap_definition':'local trial from before both GX starts through driver completion vs paid first public Run start through observed driver exit in each arrival',
        'ambient_method':'four timed same-file warm reads and four fresh 4096-byte create/write/fsync/close operations per local trial, from a separate observer on the shared filesystem',
        'payload_match':'canonical argument byte distributions, role, attempt, scripted rejection and response actions match. Store-generated absolute object IDs vary by fresh workspace.',
        'supplemental_paid_history':history_summary,'sources_sha256':{f:sha(a.control/f) for f in ('control.json','ambient-disk.jsonl','callbacks.json','callback-samples.jsonl.gz')},'result':'PASS' if all(checks.values()) else 'REVIEW'})
    print(checks);return not all(checks.values())


if __name__=='__main__':raise SystemExit(main())
