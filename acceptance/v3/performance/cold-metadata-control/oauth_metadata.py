"""Time the real metadata-only GX startup profile through Clash, with zero turns."""
import argparse
import ipaddress
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

here=Path(__file__).resolve().parent
sys.path.insert(0,str(here.parent))
from run_workload import GRID_PLUGIN, LANES, MODEL, OFFICIAL_BINARY_SHA256, check_proxy, sha, specification, utc

def peers(proc,official):
    import psutil
    seen=set()
    try:
        children=psutil.Process(proc.pid).children(recursive=True)
    except psutil.NoSuchProcess:
        return seen
    for child in children:
        try:
            if Path(child.exe()).resolve()==official:
                for sock in (child.net_connections if hasattr(child,'net_connections') else child.connections)(kind='inet'):
                    if sock.raddr and sock.status=='ESTABLISHED':
                        seen.add((sock.raddr.ip,sock.raddr.port))
        except (psutil.NoSuchProcess,psutil.AccessDenied):
            pass
    return seen

def observe_configured(stage,state):
    import contextlib
    import io
    sys.path.insert(0,str(stage/'gx-h'))
    from gx_core.p0.driver import Driver
    from probe import observe
    origin=Driver.__init__
    startup=[]
    def first_coordinator_pass(self,*args,**kwargs):
        origin(self,*args,**kwargs)
        # The production coordinator advances each new Driver to its first yield
        # before dispatch. This includes binding validation on a composed Run.
        begin=time.perf_counter_ns()
        self.probe_first_pass=self.passes(time.monotonic()+30,shared=True)
        next(self.probe_first_pass)
        startup.append((time.perf_counter_ns()-begin)/1e6)
    Driver.__init__=first_coordinator_pass
    with contextlib.redirect_stdout(io.StringIO()) as data:
        observe(stage,state)
    result=json.loads(data.getvalue())
    assert len(startup)==len(result['rows'])
    for row,duration in zip(result['rows'],startup):
        row['initial_coordinator_pass_to_first_yield_ms']=round(duration,6)
    print(json.dumps(result))

def run(a):
    os.umask(0o077)
    check_proxy()
    for name in ('GX_P0_TEST','GX_P0_TEST_PROVIDER','GX_P0_PROVIDER','OPENAI_BASE_URL','OPENAI_API_BASE','OPENAI_API_KEY'):
        if name in os.environ:raise ValueError('unexpected provider override '+name)
    runtime=a.runtime.resolve()
    binary=(runtime/'vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex').resolve()
    assert sha(binary)==OFFICIAL_BINARY_SHA256
    py=str(runtime/'.venv/bin/python')
    private=a.private.resolve();private.mkdir(mode=0o700,exist_ok=False)
    stages={'baseline':a.baseline.resolve(),'candidate':a.candidate.resolve()}
    order=('baseline','candidate','candidate','baseline','candidate','baseline','baseline','candidate')*a.blocks
    output=here/a.output
    assert not output.exists()
    policy=private/'profile.json';policy.write_text(json.dumps({'default':{'provider':'openai','model':MODEL,'effort':'xhigh'}}))
    source=Path(os.environ.get('CODEX_HOME',str(Path.home()/'.codex')))
    assert (source/'auth.json').is_file()
    rows=[]
    metadata={'started':utc(),'binary_sha256':sha(binary),'order':order,'results':rows,'scope':'metadata-only OAuth startup through verified Clash, no turn or thread RPC allowed','files':{p.name:sha(p) for p in (Path(__file__),here/'probe.py')}}
    for seq,arm in enumerate(order):
        while True:
            received=list(map(json.loads,(a.avoid_live/'commands.jsonl').read_text().splitlines()))
            from datetime import datetime
            t0=datetime.fromisoformat(json.loads((a.avoid_live/'start.json').read_bytes())['released']).timestamp()
            elapsed=time.time()-t0;wave=min(int(elapsed//600),12)
            ends={r['label'] for r in received if 'exit_code' in r}
            if f'drive-{wave:02d}' in ends and (wave==12 or (wave+1)*600-elapsed>60):
                break
            time.sleep(2)
        check_proxy()
        start=utc();stage=stages[arm];root=private/f'{seq:02d}-{arm}';root.mkdir();state=root/'state';cmds=[]
        with tempfile.TemporaryDirectory(prefix='native-metadata-',dir=root) as home:
            h=Path(home)
            for name in ('auth.json','models_cache.json'):
                if (source/name).is_file():shutil.copyfile(source/name,h/name)
            env={**os.environ,'CODEX_HOME':home,'GX_P0_CODEX':str(binary),'GX_P0_PROFILE':str(policy),
                 'PYTHONDONTWRITEBYTECODE':'1','PATH':str(runtime/'.venv/bin')+':'+os.environ['PATH']}
            for lane in LANES:
                spec=specification(stage,0,lane)
                if arm=='candidate':spec['plugins']=[GRID_PLUGIN]
                path=root/(lane+'.json');path.write_text(json.dumps(spec))
                cmd=[py,'-B',str(stage/'bin/gx-p0'),'start','--state',str(state),'--run-key',lane,'--spec',str(path)]
                res=subprocess.run(cmd,cwd=stage,env=env,capture_output=True,timeout=40)
                if res.returncode:raise RuntimeError('public GX start exited '+str(res.returncode))
                cmds.append(cmd)
            check_proxy()
            cmd=[py,'-B',str(Path(__file__).resolve()),'--observe',str(stage),str(state)]
            cmds.append(cmd);seen=set()
            with subprocess.Popen(cmd,cwd=stage,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE) as proc:
                deadline=time.monotonic()+70
                try:
                    while proc.poll() is None and time.monotonic()<deadline:
                        new=peers(proc,binary)
                        if any(not ipaddress.ip_address(host).is_loopback or port not in (7890,7891) for host,port in new):
                            raise RuntimeError('unexpected outbound native socket')
                        seen.update(new)
                        time.sleep(.08)
                    out,err=proc.communicate(timeout=4)
                except BaseException:
                    proc.kill();proc.wait();raise
                if proc.returncode:raise RuntimeError('native metadata probe exited '+str(proc.returncode))
            result=json.loads(out)
            counts=result['counts']
            assert counts['p0_meter_native_turns']==counts['p0_roots']==0
            assert counts['reservations_by_subject']=={'run':2,'job':4,'team':2}
            for row in result['rows']:
                assert row['selected_plugin']==(arm=='candidate') and not row['supplement']
                assert {'initialize','config/read','account/read','model/list'}<=set(x['phase'] for x in row['main'])
            rows.append({'sequence':seq,'arm':arm,'started':start,'ended':utc(),'commands':cmds,
                         'observed_native_established_peers':[{'address':host,'port':port} for host,port in sorted(seen)],**result})
        metadata['ended']=utc();tmp=output.with_suffix('.tmp');tmp.write_text(json.dumps(metadata,indent=2)+'\n');tmp.replace(output)
        print(seq,arm,[round(r['total_ms'],1) for r in result['rows']],flush=True)
if __name__=='__main__' and len(sys.argv)>1 and sys.argv[1]=='--observe':
    observe_configured(Path(sys.argv[2]),Path(sys.argv[3]))
elif __name__=='__main__':
    p=argparse.ArgumentParser()
    for k in ('runtime','baseline','candidate','private','avoid-live'):p.add_argument('--'+k,type=Path,required=True)
    p.add_argument('--blocks',type=int,default=1);p.add_argument('--output',default='oauth-samples.json')
    run(p.parse_args())
