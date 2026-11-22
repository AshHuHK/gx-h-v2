"""Join optional Linux birth observations to individual cold dispatches and their first timed GX writer."""
import argparse
import json
from pathlib import Path

from audit_workload import quantile
from dispatch_phases import collect,ms
from run_workload import save,utc


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--private',required=True,type=Path)
    p.add_argument('--births',required=True,type=Path);p.add_argument('--output',required=True,type=Path)
    p.add_argument('--completed-waves',type=int,default=13);a=p.parse_args()
    births=[r for r in map(json.loads,a.births.read_text().splitlines()) if r['wave']<a.completed_waves]
    dispatches,errors,_=collect(a.private,[r['wave'] for r in births]);rows=[]
    for wave in births:
        cold=[r for r in dispatches if r['wave']==wave['wave'] and 'first_same_task_writer' in r]
        for child in wave['official_codex_processes']:
            t=child['estimated_birth_ns'];precision=child['tick_quantization_ns']+child['clock_sample_width_ns'];upper=t+precision
            # The kernel reports birth in ticks. The whole birth interval must intersect
            # exactly one first-claim metric on this public paired coordinator.
            matches=[r for r in cold if r['started_ns']<=upper and t<=r['first_same_task_writer']['started_ns']]
            if len(matches)!=1:
                errors.append(f"wave {wave['wave']} pid {child['pid']} has {len(matches)} cold dispatch matches");continue
            r=matches[0];s=r['started_ns'];f=r['first_same_task_writer']['started_ns'];mid=(t+upper)//2
            rows.append({'wave':r['wave'],'run_id':r['run_id'],'lane':r['lane'],'task_id':r['task_id'],'thread_id':r['thread_id'],
                'pid':child['pid'],'driver_pid':child['driver_pid'],'started_ns':s,'estimated_birth_ns':t,
                'birth_upper_ns':upper,'first_same_task_writer_ns':f,'claim_ns':r['claim_ns'],
                'dispatch_start_to_birth_ms':[ms(t-s),ms(upper-s)],'birth_to_first_writer_ms':[ms(f-upper),ms(f-t)],
                'dispatch_start_to_birth_midpoint_ms':ms(mid-s),'birth_to_first_writer_midpoint_ms':ms(f-mid),
                'dispatch_start_to_first_writer_ms':ms(f-s),'full_dispatch_ms':r['duration_ms']})
    if len({r['task_id'] for r in rows})!=len(rows):errors.append('multiple observed processes assigned to one cold dispatch')
    measures={k:quantile([r[k] for r in rows]) for k in ('dispatch_start_to_birth_midpoint_ms','birth_to_first_writer_midpoint_ms','dispatch_start_to_first_writer_ms','full_dispatch_ms')}
    save(a.output,{'captured':utc(),'scope':'corrected real candidate only, passive process sampler began after wave index 3',
        'method':'Linux official binary descendant birth is precise within one clock tick, typically 10 ms. All per-sample duration ranges are retained; aggregate percentiles use their midpoints.',
        'interpretation':'Before birth includes public Driver and child-launch preparation. After birth through first GX outer writer includes native app-server startup, access checks, provider/catalog/account calls and remaining preprocessing; those activities are not individually timed.',
        'observed_wave_indices':[r['wave'] for r in births],'complete_observed_waves':all(r['all_expected_processes'] for r in births),
        'latency_ms':measures,'samples':rows,'errors':errors});print(json.dumps({'latency_ms':measures,'errors':errors},indent=2));return bool(errors)


if __name__=='__main__':
    raise SystemExit(main())
