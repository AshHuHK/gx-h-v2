"""Time production Driver.native on both stages, including only metadata RPCs."""
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

def utc():
    return datetime.now(timezone.utc).isoformat()

def observe(stage,state):
    sys.path.insert(0,str(stage/'gx-h'))
    from gx_core.p0.driver import Driver
    from gx_core.p0.roots import Roots, AppServerClient
    from gx_core.orchestration import service
    from gx_core.persistence import store
    from gx_core.p0 import profiles
    spans=[]
    def wrap(owner,name):
        orig=getattr(owner,name)
        def timed(*args,**kwargs):
            label=args[1] if name in ('request','notify') else owner.__name__+'.'+name
            if name in ('request','notify') and label not in ('initialize','initialized','config/read','account/read','model/list'):
                raise RuntimeError('unexpected native RPC '+label)
            start=time.perf_counter_ns()
            try:
                return orig(*args,**kwargs)
            finally:
                spans.append({'phase':label,'start':start,'end':time.perf_counter_ns()})
        setattr(owner,name,timed)
    for owner,name in [(AppServerClient,'request'),(AppServerClient,'start'),(AppServerClient,'notify'),(Roots,'__init__'),(Roots,'__enter__'),(service,'hooks_for_run')]:
        wrap(owner,name)
    rows=[]
    with closing(store.open_store(state/'gx.db')) as conn, ExitStack() as stack:
        # Both fresh Roots share one public coordinator process and private native home.
        runs=conn.execute('SELECT run_id,inputs_json FROM runs ORDER BY rowid').fetchall()
        assert len(runs)==2
        for position,(rid,inputs) in enumerate(runs):
            driver=Driver(conn,state,rid)
            stack.callback(driver.close)
            spans.clear()
            start=time.perf_counter_ns()
            native=driver.native
            end=time.perf_counter_ns()
            main=list(spans)
            spans.clear()
            # The credential-free fixture does not require account/catalog. Time both on
            # the same official process as supplemental diagnostics after the production path.
            if all(x['phase']!='account/read' for x in main):
                native.client.request('account/read',{'refreshToken':False})
            if all(x['phase']!='model/list' for x in main):
                profiles.model_catalog(native.client,'gx_loopback')
            tick=lambda value: round(value/1e6,6)
            fmt=lambda rs:[{'phase':x['phase'],'start_ms':tick(x['start']-start),'end_ms':tick(x['end']-start),'duration_ms':tick(x['end']-x['start'])} for x in rs]
            enter=next(x for x in main if x['phase']=='Roots.__enter__')
            hook=next(x for x in main if x['phase'].endswith('.hooks_for_run'))
            setup=next(x for x in main if x['phase']=='Roots.__init__')
            rows.append({'run_id':rid,'position':position,'selected_plugin':bool(json.loads(inputs).get('plugin_binding')),'total_ms':tick(end-start),
                         'before_enter_ms':tick(enter['start']-start),'hook_and_tool_prep_ms':tick(setup['start']-hook['start']),'worker_tool_build_envelope_ms':tick(setup['start']-hook['end']),'main':fmt(main),'supplement':fmt(spans)})
        counts={t:conn.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('p0_meter_native_turns','budget_reservations','p0_roots')}
        counts['reservations_by_subject']=dict(conn.execute('SELECT subject_kind,count(*) FROM budget_reservations GROUP BY subject_kind'))
    print(json.dumps({'rows':rows,'counts':counts}))

def acquire(a):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from run_workload import GRID_PLUGIN, LANES, MODEL, OFFICIAL_BINARY_SHA256, check_proxy, sha, specification
    runtime=a.runtime.resolve()
    sys.path[:0]=[str(runtime/'acceptance'),str(runtime/'gx-h')]
    from e2e_grid import fixture_env
    from e2e_responses import local_responses
    binary=runtime/'vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex'
    assert sha(binary)==OFFICIAL_BINARY_SHA256
    private=a.private.resolve()
    private.mkdir(mode=0o700,exist_ok=False)
    evidence=Path(__file__).resolve().parent
    if (evidence/a.output).exists():
        raise FileExistsError(a.output)
    stages={'baseline':a.baseline.resolve(),'candidate':a.candidate.resolve()}
    script=Path(__file__).resolve()
    rows=[]
    order=('baseline','candidate','candidate','baseline','candidate','baseline','baseline','candidate')*a.blocks
    measured={'started':utc(),'scope':'credential-free loopback for native startup, followed by supplemental account/read and model/list; zero model turns','source_sha256':sha(script),'binary_sha256':sha(binary),'stages':{k:str(v) for k,v in stages.items()},'order':order,'results':rows}
    for seq,arm in enumerate(order):
        if a.avoid_live:
            live=a.avoid_live
            while True:
                t0=datetime.fromisoformat(json.loads((live/'start.json').read_bytes())['released']).timestamp()
                elapsed=time.time()-t0
                wave=min(int(elapsed//600),12)
                exits={v['label'] for v in map(json.loads,(live/'commands.jsonl').read_text().splitlines()) if 'exit_code' in v}
                if f'drive-{wave:02d}' in exits and (wave==12 or (wave+1)*600-elapsed>50):
                    break
                time.sleep(2)
        check_proxy()
        root=private/f'{seq:02d}-{arm}';root.mkdir()
        state=root/'state'
        stage=stages[arm];py=str(runtime/'.venv/bin/python');start=utc()
        with local_responses(root/'peer.jsonl') as peer:
            env=fixture_env(root,peer,str(binary))
            env['GX_P0_MODEL']=MODEL
            env['PATH']=str(runtime/'.venv/bin')+':'+env['PATH']
            cmds=[]
            for lane in LANES:
                spec=specification(stage,0,lane)
                if arm=='candidate':spec['plugins']=[GRID_PLUGIN]
                specpath=root/(lane+'.json')
                specpath.write_text(json.dumps(spec))
                cmd=[py,'-B',str(stage/'bin/gx-p0'),'start','--state',str(state),'--run-key',lane,'--spec',str(specpath)]
                proc=subprocess.run(cmd,cwd=stage,env=env,capture_output=True,timeout=35)
                (root/(lane+'.err')).write_bytes(proc.stderr)
                proc.check_returncode()
                cmds.append(cmd)
            cmd=[py,'-B',str(script),'--observe',str(stage),str(state)]
            proc=subprocess.run(cmd,cwd=stage,env=env,capture_output=True,timeout=80)
            (root/'observe.err').write_bytes(proc.stderr)
            proc.check_returncode()
            cmds.append(cmd)
            r=json.loads(proc.stdout)
            assert r['counts']['p0_meter_native_turns']==0 and r['counts']['p0_roots']==0 and not peer.request_count
            assert r['counts']['reservations_by_subject']=={'run':2,'job':4,'team':2}
            assert all(sample['selected_plugin']==(arm=='candidate') for sample in r['rows'])
            rows.append({'sequence':seq,'arm':arm,'started':start,'ended':utc(),'commands':cmds,'responses':peer.request_count,**r})
            measured['ended']=utc()
            tmp=evidence/(a.output+'.tmp')
            tmp.write_text(json.dumps(measured,indent=2)+'\n')
            tmp.replace(evidence/a.output)
            print(seq,arm,[v['total_ms'] for v in r['rows']],flush=True)
if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--observe':
        observe(Path(sys.argv[2]),Path(sys.argv[3]))
    else:
        p=argparse.ArgumentParser()
        for name in ('baseline','candidate','runtime','private'):
            p.add_argument('--'+name,type=Path,required=True)
        p.add_argument('--avoid-live',type=Path)
        p.add_argument('--output',default='samples.json')
        p.add_argument('--blocks',type=int,default=3)
        acquire(p.parse_args())
