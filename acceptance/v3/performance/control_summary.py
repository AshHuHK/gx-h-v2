"""Summarize immutable stage A/B trials, official RPC timings and probe outcomes."""
import argparse
from collections import Counter, defaultdict
from pathlib import Path
import json

from audit_workload import quantile,records_in,write_compressed
from run_workload import save,sha,utc


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--private',type=Path,required=True)
    p.add_argument('--evidence',type=Path,required=True);a=p.parse_args();out=a.evidence
    control=json.loads((out/'control.json').read_bytes()); all_ops=[]; groups={}; issues=[]
    if control['result']!='PASS' or len(control['trials'])!=8:
        issues.append('control trial set is incomplete')
    for arm in ('baseline','candidate'):
        counters=Counter(); per_trial={}; samples=defaultdict(list); peers=[]
        for t in (t for t in control['trials'] if t['arm']==arm):
            trial=a.private/Path(t['private_trial']).name
            if not t['valid'] or not t['all_connections_loopback']:
                issues.append('unmatched science trial '+trial.name)
            if sha(trial/'metrics.jsonl')!=t['metrics_sha256'] or sha(trial/'peer.jsonl')!=t['peer_trace_sha256']:
                issues.append('acquisition hash mismatch '+trial.name)
            peers.append((t['peer_responses'],t['peer_tool_actions'],t['reviews'],t['model_effort']))
            counters.update({'trials':1,'completed_runs':t['runs'].get('COMPLETED',0),'jobs':t['jobs'].get('SUCCEEDED',0),
                'completed_turns':t['turns'].get('completed',0),'provider_responses':t['peer_responses'],
                'accepted_products':t['reviews'].get('ACCEPTED',0),'rejected_products':t['reviews'].get('REJECTED',0)})
            if not t['instrumented']:
                continue
            timed=trial/'owner-timings.jsonl'
            if sha(timed)!=t['owner_timings_sha256']:
                issues.append('owner-timer hash mismatch '+trial.name)
            private_rows=list(records_in(timed)); dispatches=[v for v in private_rows if v['operation']=='dispatch']; phases=defaultdict(list)
            # _claim records the actual return/exception emitted by the owner. Nest by identity and time bounds.
            for v in private_rows:
                outcome=v['outcome']; name=v['operation']; method=v['owner']+'.'+name
                if name=='__enter__':
                    segment='cold_app_server';
                else:
                    span=next((u for u in dispatches if u['task_id']==v['task_id'] and u['started_ns']<=v['started_ns'] and v['ended_ns']<=u['ended_ns']),None)
                    if span is None:
                        issues.append('unjoined nested control timer '+trial.name); continue
                    claim=next((u for u in private_rows if u['operation']=='_claim' and u['task_id']==span['task_id'] and span['started_ns']<=u['started_ns'] and u['ended_ns']<=span['ended_ns']),None)
                    if not claim:
                        issues.append('control dispatch without _claim '+trial.name); continue
                    segment={'new_root':'new_claim','HoldActive':'denied_READY','existing_lease':'existing_lease'}.get(claim['outcome'],claim['outcome'])
                key=segment+'/'+method
                samples[key].append(v['duration_ms']);phases[key].append(v['duration_ms'])
                all_ops.append({'arm':arm,'sequence':t['sequence'],'stratum':segment,**v})
            per_trial[str(t['sequence'])]={k:quantile(v) for k,v in sorted(phases.items())}
        groups[arm]={'outcomes':counters,'script_mix':[{'responses':n,'actions':actions,'reviews':reviews,'model_effort':model} for n,actions,reviews,model in peers],
            'instrumented_owners_ms':{k:quantile(v) for k,v in sorted(samples.items())},'instrumented_owners_per_trial_ms':per_trial}
    b=groups['baseline'];c=groups['candidate']
    if b['outcomes']!=c['outcomes'] or b['script_mix']!=c['script_mix']:
        issues.append('different scientific or Responses action mix by arm')
    outfile=out/'trace-samples.jsonl.gz';write_compressed(outfile,all_ops)
    save(out/'owner-summary.json',{'captured':utc(),'groups':groups,'issues':issues,'passes':not issues,
        'timing':'per-call wall times of the same named owner in both staged releases. Parent and child intervals overlap and must not be summed.',
        'source_trace_samples':len(all_ops),'source_trace_samples_sha256':sha(outfile)})
    for key in ('cold_app_server/Roots.__enter__','new_claim/Roots.dispatch','new_claim/Roots._claim','denied_READY/Roots._claim',
                'new_claim/Roots._materialize','new_claim/Roots.thread/start','new_claim/Roots.turn/start','new_claim/RootDelivery.deliver'):
        print(key,b['instrumented_owners_ms'].get(key),c['instrumented_owners_ms'].get(key))
    print('issues',issues)
    return bool(issues)


if __name__=='__main__':
    raise SystemExit(main())
