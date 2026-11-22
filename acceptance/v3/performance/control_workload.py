"""Interleaved, credential-free control for the real arms' dispatch operation mix."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

from run_workload import GRID_PLUGIN, LANES, MODEL, OFFICIAL_BINARY_SHA256, check_proxy, save, sha, specification, utc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for option in ('baseline','candidate','runtime','private','evidence'):
        parser.add_argument('--'+option,type=Path,required=True)
    parser.add_argument('--baseline-composed',action='store_true',
                        help='Select the reviewed grid in both arms when comparing a scheduling-only correction')
    args = parser.parse_args()
    os.umask(0o077)
    check_proxy()
    work, out = args.private.resolve(), args.evidence.resolve()
    work.mkdir(parents=True, exist_ok=False)
    out.mkdir(parents=True, exist_ok=False)
    runtime = args.runtime.resolve()
    sys.path.insert(0,str(runtime/'acceptance'))
    import e2e_grid
    import e2e_responses as peer
    judge_reply = peer.fixture_action
    def fixed_response(body, request_id):
        item, info = judge_reply(body,request_id)
        if 'task_id' in info:
            brief = peer.brief_from(body)
            info.update(attempt=brief['attempt'])
            if (info.get('tool')=='gx_submit_product' and info['output_kind']=='science_assessment'
                    and brief['attempt']==1):
                args_ = json.loads(item['arguments'])
                args_['product']['findings'].pop()
                item['arguments'] = json.dumps(args_)
                info['fixture_dropped_last_finding'] = True
        time.sleep(2.0 if item['type']=='message' else 0.20)
        return item, info
    peer.fixture_action = fixed_response
    binary = runtime/'vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex'
    if sha(binary) != OFFICIAL_BINARY_SHA256:
        raise RuntimeError('official Codex executable digest changed')
    stages = {'baseline':args.baseline.resolve(),'candidate':args.candidate.resolve()}
    order = ('baseline','candidate','candidate','baseline','candidate','baseline','baseline','candidate')
    result={'mode':'credential-free loopback Responses through actual Codex 0.169.0, two real local grid Runs per workspace',
        'codex_sha256':sha(binary),'started':utc(),'order':order,'model':MODEL,'peer_request_delay_s':0.2,
        'peer_final_message_delay_s':2.0,'fixed_script':'reject first assessment by removing one finding; record accepted retry; same scientific judge in both arms',
        'controlled_difference':('both arms select org.gx.science-grid@2.0.0 for a scheduling-only comparison' if args.baseline_composed else 'only candidate selects org.gx.science-grid@2.0.0')+'; peer never calls the contributed measurement tool',
        'stage_metadata':{k:{'path':str(stage),'driver_sha256':sha(stage/'gx-h/gx_core/p0/driver.py'),
            'roots_sha256':sha(stage/'gx-h/gx_core/p0/roots.py')} for k,stage in stages.items()},
        'trials':[], 'result':'IN_PROGRESS'}
    save(out/'control.json',result)
    for sequence,arm in enumerate(order):
        trial=work/f'{sequence:02d}-{arm}'
        trial.mkdir()
        # One trial on each side is uninstrumented; subsequent trials have symmetric nested timers.
        instrumented=sequence>=2
        stage = stages[arm]
        state = trial/'state-00'
        row={'sequence':sequence,'arm':arm,'private_trial':str(trial),'instrumented':instrumented,'started':utc(),'commands':[]}
        with peer.local_responses(trial/'peer.jsonl') as server:
            env=e2e_grid.fixture_env(trial,server,str(binary))
            env.update(GX_P0_MODEL=MODEL,GX_METRICS_FILE=str(trial/'metrics.jsonl'),
                       GX_CONTROL_TIMINGS=str(trial/'owner-timings.jsonl'),PATH=str(runtime/'.venv/bin')+':'+env['PATH'])
            def command(label, argv, instrument=False):
                cli=[str(runtime/'.venv/bin/python'),'-B']
                if instrument:
                    cli.append(str(Path(__file__).with_name('control_instrument.py').resolve()))
                cli.extend([str(stage/'bin/gx-p0'),*argv]); begin=time.monotonic()
                with (trial/(label+'.out')).open('w') as stdout,(trial/(label+'.err')).open('w') as stderr:
                    proc=subprocess.run(cli,env=env,cwd=stage,stdout=stdout,stderr=stderr,timeout=150)
                row['commands'].append({'label':label,'command':cli,'exit':proc.returncode,'wall_s':round(time.monotonic()-begin,4),
                    'stdout_sha256':sha(trial/(label+'.out')),'stderr_sha256':sha(trial/(label+'.err'))})
                if proc.returncode:
                    raise RuntimeError(f'control {trial.name} {label} exited {proc.returncode}')
            for lane in LANES:
                spec=specification(stage,0,lane)
                if arm=='candidate' or args.baseline_composed:
                    spec['plugins']=[GRID_PLUGIN]
                specfile=trial/(lane+'.json'); save(specfile,spec)
                command('start-'+lane,['start','--state',str(state),'--run-key','00-'+lane,'--spec',str(specfile)])
            command('drive',['run','--state',str(state),'--timeout','120'],instrumented)
            row['peer_responses']=len(server.rows)
            row['peer_tool_actions']=dict(Counter(i.get('tool','finish') for i in server.rows))
            row['attempts_made_incomplete_by_control']=sum(bool(i.get('fixture_dropped_last_finding')) for i in server.rows)
            row['peer_errors']=sum('error' in i for i in server.rows)
            row['all_connections_loopback']=all(i['peer']=='127.0.0.1' for i in server.rows)
            row['model_effort']=sorted(set((i['model'],i['reasoning_effort']) for i in server.rows))
        with sqlite3.connect((state/'gx.db').as_uri()+'?mode=ro',uri=True) as conn:
            row['runs']=dict(conn.execute('select status,count(*) from runs group by status'))
            row['jobs']=dict(conn.execute('select status,count(*) from jobs group by status'))
            row['turns']=dict(conn.execute('select terminal_status,count(*) from p0_meter_native_turns group by terminal_status'))
            row['reviews']=dict(conn.execute('select result,count(*) from submissions group by result'))
        row.update(ended=utc(),metrics_sha256=sha(trial/'metrics.jsonl'),peer_trace_sha256=sha(trial/'peer.jsonl'))
        if instrumented:
            row['owner_timings_sha256']=sha(trial/'owner-timings.jsonl')
        row['valid']=row['runs']=={'COMPLETED':2} and row['jobs']=={'SUCCEEDED':4} and row['turns']=={'completed':6} and row['all_connections_loopback'] and row['peer_errors']==0 and row['attempts_made_incomplete_by_control']==2
        result['trials'].append(row); save(out/'control.json',result)
        print(sequence,arm,row['valid'],row['runs'],row['turns'],row['reviews'],row['peer_tool_actions'],flush=True)
        if not row['valid']:
            raise RuntimeError('controlled science mix did not match the study specification')
    result.update(result='PASS',ended=utc()); save(out/'control.json',result)


if __name__=='__main__':
    main()
