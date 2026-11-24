"""Run the GX Python and Node suites and the host generator in an isolated checkout of a named commit."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import time

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from acceptance.closure.profile import test_environment
from scripts.acceptance_dependencies import inspect_python, runtime, verify_assets
PRODUCT = ('gx-h/', 'bin/', 'codex_adapter/', 'examples/', 'scripts/')
PROTOCOL = ('native_activity.py','native_budget.py','native_cumulative.py','native_events.py','native_refusal.py','native_child.py','native_wake.py')

PREFLIGHT = """
import json
from gx_core import config
from gx_core.capabilities import probe
from gx_core.hostobs.native_app_server import _proxy_preflight
from gx_core.jobs import capacity

_proxy_preflight()
cpu = capacity.cpu_state()
gpu = probe.read_gpu_state()
print(json.dumps({"cpu_source": cpu["source"], "logical_cpus": cpu["logical_cpus"],
                  "quota_cpus": cpu["cgroup_quota_cpus"], "cpu_limits": config.cpu_limits(),
                  "gpu_source": gpu["source"], "gpu_devices": len(gpu["devices"])}))
"""


def utc():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream,'sha256').hexdigest()


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)+'\n')


def unit_counts(log):
    text=log.read_text()
    count=re.findall(r'^Ran (\d+) tests?',text,re.M)
    total=int(count[-1]) if count else 0
    skips=[line.strip() for line in text.splitlines() if '... skipped ' in line]
    failures=re.findall(r'^(FAIL|ERROR): (.*)$',text,re.M)
    return {'total':total,'passed':total-len(skips)-len(failures),'skipped':len(skips),
            'failures':len([f for f in failures if f[0]=='FAIL']),'errors':len([f for f in failures if f[0]=='ERROR']),
            'skip_details':skips,'failure_details':failures}


def run_suite(commit, out, *, native, selected, product_commit, python_only=False, binary=None):
    scratch=tempfile.TemporaryDirectory(prefix='gx-v21-regression-')
    checkout=Path(scratch.name)
    if native and binary is None:
        binary=REPO/'vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex'
    env=test_environment(checkout/'profile', binary=binary)
    if product_commit:
        subprocess.run(['git','diff','--exit-code','--quiet',product_commit,commit,'--',
                        *[p.rstrip('/') for p in PRODUCT],'release-assets'],cwd=REPO,check=True,env=env)
    package=checkout/'package'
    package.mkdir()
    paths=['gx-h','bin','codex_adapter','examples','scripts','release-assets/lock.json','release-assets/npm',
           'release-assets/python','release-assets/codex-schema-v0.169.0-experimental.tar.gz']
    if native:
        # Retain only acceptance sources and fixtures in the reproducible checkout.
        paths += subprocess.check_output(['git','ls-tree','--name-only','-r',commit,'acceptance'],cwd=REPO,
                  text=True,env=env).splitlines()
        paths=[p for p in paths if not p.startswith('acceptance/') or
               (p.endswith('.py') and (len(Path(p).parts)==2 or Path(p).name in PROTOCOL))]
    with subprocess.Popen(['git','archive',commit,*paths],cwd=REPO,stdout=subprocess.PIPE,env=env) as process:
        with tarfile.open(mode='r|',fileobj=process.stdout) as archive:
            archive.extractall(package,filter='data')
        if process.wait():
            raise RuntimeError('failed to extract requested checkout')
    # Historical named revisions can predate the runtime declaration. Verify their
    # own committed dependencies against the current deterministic runtime lock.
    current_pins = json.loads((REPO / "release-assets/lock.json").read_text())["deterministic_test_runtime"]
    lock, requirements, cordis, archives = verify_assets(package, historical_pins=current_pins)
    pins = lock.get("deterministic_test_runtime", current_pins)
    _, node, npm, py_version, node_version, npm_version, env = runtime(
        REPO, checkout, python=Path(sys.executable), node=None, env=env, pins=pins)
    manifest={p.relative_to(package).as_posix():digest(p) for p in sorted(package.rglob('*')) if p.is_file()}
    save(out/'files.json',manifest)
    product={name:sha for name,sha in manifest.items() if name.startswith(PRODUCT)}
    content_hash=hashlib.sha256(json.dumps(product,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    summary={'commit':commit,'verified_product_commit':product_commit or commit,'started_utc':utc(),'source_file_manifest':'files.json','product_files':len(product),
             'requested_test_profile':env['GX_TEST_RESOURCE_PROFILE'],'test_environment_sha256':digest(REPO/'acceptance/closure/profile.py'),
             'product_sha256':content_hash,'product_prefixes':PRODUCT,'node_version':"v"+node_version,
             'python_version':py_version, 'operations':{},
             'dependency_preflight':{'node':str(node),'npm':str(npm),'npm_version':npm_version,
                  'runtime_lock_source':'named checkout' if 'deterministic_test_runtime' in lock else 'acceptance runner',
                  'runtime_pins':pins,'release_lock_sha256':digest(package/'release-assets/lock.json'),
                  'cordis_lock_sha256':digest(cordis),'requirements_sha256':digest(requirements),
                  'offline_archive_sha256':archives},
             'class':'Local Python, SQLite and CPU Job fixtures. Official-protocol scenarios use pinned Codex with a scripted loopback provider.'}
    def execute(label, command, cwd):
        start=utc()
        elapsed=time.monotonic()
        with (out/(label+'.log')).open('w') as log:
            code=subprocess.run(command,cwd=cwd,env=env,stdout=log,stderr=subprocess.STDOUT).returncode
        return {'command':[str(a).replace(str(package),'$CHECKOUT').replace(str(checkout),'$SCRATCH').replace(
                    str(out),'$EVIDENCE') for a in command], 'returncode':code,'started_utc':start,'ended_utc':utc(),
                'cwd':str(cwd).replace(str(package),'$CHECKOUT'),'seconds':round(time.monotonic()-elapsed,3)}

    def record(name,command,cwd):
        operation=execute(name,command,cwd)
        summary['operations'][name]=operation
        save(out/'summary.json',summary)
        return operation['returncode']

    try:
        if record('clash-preflight', [sys.executable, '-B', '-c', PREFLIGHT], package/'gx-h'):
            return 2
        observed=json.loads((out/'clash-preflight.log').read_text())
        summary['resource_observation']=observed
        summary['test_resource_profile']='deterministic' if observed == {
            'cpu_source':'deterministic test profile','logical_cpus':2,'quota_cpus':2,
            'cpu_limits':{'max_pool_workers':2,'max_quota_fraction':1},
            'gpu_devices':0,'gpu_source':'supplied'} else 'real'
        venv=checkout/'venv'
        python=venv/'bin/python'
        wheels=package/'release-assets/python'
        installs=[('venv',[sys.executable,'-m','venv',venv]),
                  ('offline-wheels',[python,'-m','pip','--isolated','--disable-pip-version-check','install',
                     '--no-index','--no-cache-dir','--require-hashes','--find-links',wheels,'-r',wheels/'requirements.lock']),
                  ('offline-cordis',[python,'-B','scripts/release_assets.py','cordis'])]
        for name,command in installs:
            if record(name,command,package):
                return 2
        summary['dependency_preflight']['installed_python'] = inspect_python(
            venv, env, py_version, lock["scientific_review"]["wheels"])
        summary['dependency_preflight']['installed_node_modules'] = json.loads((out/'offline-cordis.log').read_text())
        env['PATH']=str(venv/'bin')+os.pathsep+env['PATH']
        if binary is not None:
            lock=json.loads((package/'release-assets/lock.json').read_text())
            summary['native_binary']={'version':subprocess.check_output([binary,'--version'],text=True,env=env).strip(),
                                      'sha256':digest(binary)}
            if summary['native_binary']['version']!='codex-cli 0.169.0' or summary['native_binary']['sha256']!=lock['linux_x64']['binary_sha256']:
                raise ValueError('binary does not match release lock')
            env['PATH']=str(binary.parent)+os.pathsep+env['PATH']
            env['GX_P0_CODEX']=str(binary)
        gx=package/'gx-h'
        if python_only:
            record('python-full',[python,'-B','-m','unittest','discover','-s','tests','-v'],gx)
            summary['python_counts']=unit_counts(out/'python-full.log')
        elif not selected:
            commands={'python-full':[python,'-B','-m','unittest','discover','-s','tests','-v'],
                      'generator':[python,'-B','adapters/hosts/generate.py','--check'],
                      'node':['node','--experimental-strip-types','--test','runtime/plugin-host/host.test.ts']}
            with ThreadPoolExecutor(max_workers=len(commands)) as pool:
                pending={k:pool.submit(execute,k,v,gx) for k,v in commands.items()}
                summary['operations'].update({k:v.result() for k,v in pending.items()})
            summary['python_counts']=unit_counts(out/'python-full.log')
            summary['node_counts']={key:int(value) for key,value in re.findall(
                r'^# (tests|pass|fail|skipped|cancelled) (\d+)',(out/'node.log').read_text(),re.M)}
            record('p0-focused',[python,'-B','-m','unittest','discover','-s','tests','-p','test_p0*.py','-v'],gx)
            summary['p0_counts']=unit_counts(out/'p0-focused.log')
        if native:
            native_dir=out/'native'
            native_dir.mkdir(exist_ok=True)
            native_commands={
                'native-reference':['v21/native_events.py'],
                'native-routing':['v21/native_activity.py','--case','routing'],
                'native-http429':['v21/native_refusal.py'],
                'native-lease':['v21/native_activity.py','--case','lease'],
                'native-multi-budget':['v21/native_budget.py'],
                'native-meter-warm':['v21/native_cumulative.py','--connection','warm'],
                'native-meter-cold':['v21/native_cumulative.py','--connection','cold'],
                'native-meter-repeated':['v21/native_cumulative.py','--connection','cold-repeated'],
                'native-late-child':['v21/native_child.py','--case','complete'],
                'native-incomplete-child':['v21/native_child.py','--case','missing'],
                'native-unloaded-child':['v21/native_child.py','--case','rapid'],
                'native-interrupted':['acc1_interrupted_meter.py'],
                'native-wake-ack':['v21/native_wake.py']}
            if selected:
                unknown=set(selected)-native_commands.keys()
                if unknown:
                    raise ValueError('Unknown native cases '+str(sorted(unknown)))
                native_commands={key:native_commands[key] for key in selected}
            with ThreadPoolExecutor(max_workers=3) as pool:
                jobs={k:pool.submit(execute,k,[python,'-B',package/'acceptance'/v[0],*v[1:],
                                   '--out',native_dir/(k+'.json')],package) for k,v in native_commands.items()}
                summary['operations'].update({k:v.result() for k,v in jobs.items()})
            if not selected:
                record('official-suite',[python,'-B','acceptance/native_probe.py','--binary',binary,
                                         '--out',native_dir/'official-suite'],package)
                summary['official_cases']={p.stem:json.loads(p.read_text())['status']
                                           for p in sorted((native_dir/'official-suite').glob('*.json'))}
        summary['ended_utc']=utc()
        if (out/'python-full.log').is_file():
            summary['python_log_sha256']=digest(out/'python-full.log')
        summary['passed']=all(op['returncode']==0 for op in summary['operations'].values())
        save(out/'summary.json',summary)
        print('PASS' if summary['passed'] else 'FAIL', {k:v['returncode'] for k,v in summary['operations'].items()})
        return 0 if summary['passed'] else 1
    finally:
        scratch.cleanup()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--commit',required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--native',action='store_true',help='also run the committed 2.1 protocol probes against the local locked ELF')
    parser.add_argument('--native-case',action='append',default=[],help='run only this named official scenario (repeatable)')
    parser.add_argument('--product-commit',help='verify the selected commit has identical production, test and dependency trees')
    parser.add_argument('--python-only',action='store_true',help='run the original Python discovery suite in its isolated checkout')
    parser.add_argument('--binary',type=Path,help='explicit local Codex binary matching the release lock')
    args=parser.parse_args()
    if args.python_only and (args.native or args.native_case):
        parser.error('--python-only and native protocol suites are separate selections')
    args.out.mkdir(parents=True,exist_ok=True)
    commit=subprocess.check_output(['git','rev-parse','--verify',args.commit+'^{commit}'],cwd=REPO,text=True).strip()
    product=None
    if args.product_commit:
        product=subprocess.check_output(['git','rev-parse','--verify',args.product_commit+'^{commit}'],cwd=REPO,text=True).strip()
    raise SystemExit(run_suite(commit,args.out.resolve(),native=args.native or bool(args.native_case),
                               selected=args.native_case,product_commit=product,python_only=args.python_only,
                               binary=args.binary.absolute() if args.binary else None))
