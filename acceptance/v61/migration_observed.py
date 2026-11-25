"""Cold-restore the terminal V6 live study in a disposable checkout and upgrade it in place."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile


BASE = '47fc728d32bc10ba478df6a063529a8d98f98828'
RUN_ID = 'gxr_01m8871dsn80ypeaz1'


def digest(file):
    with file.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def invoke(args, env=None, cwd=None):
    proc = subprocess.run(args, check=True, cwd=cwd, env=env, text=True, capture_output=True, timeout=90)
    return proc.stdout.strip()


def business(state):
    db = state / 'gx.db'
    with closing(sqlite3.connect(db.resolve().as_uri() + '?mode=ro', uri=True)) as c:
        assert c.execute('pragma quick_check').fetchone() == ('ok',)
        assert not c.execute('pragma foreign_key_check').fetchall()
        # A drained run's logical graph, scientific evidence, external identities, original
        # charges and provider receipts are compared by row contents, including their IDs.
        unchanged = {}
        for name in ('runs', 'v6_goals', 'v6_graph_versions', 'v6_decision_tasks', 'v6_decisions',
                     'v6_branches', 'v6_nodes', 'v6_job_attempts', 'v6_node_waits', 'jobs', 'artifacts',
                     'hypotheses', 'hypothesis_revisions', 'budgets', 'budget_reservations',
                     'p0_meter_native_turns', 'outbox'):
            rows = sorted((list(row) for row in c.execute(f'SELECT * FROM {name}')), key=sha)
            unchanged[name] = {'rows':len(rows), 'sha256':sha(rows)}
        for key, ns, rel, size, wanted in c.execute('SELECT artifact_ref,namespace,rel_path,bytes,sha256 FROM artifacts'):
            loc = state / 'artifacts' / ns / rel
            assert loc.is_file() and loc.stat().st_size == size and digest(loc) == wanted, key
        contract = c.execute("select value from schema_meta where key='contract_sha256'").fetchone()[0]
        run = c.execute('SELECT run_id,status FROM runs').fetchall()
        assert run == [(RUN_ID, 'STOPPED')]
        assert c.execute('SELECT status FROM v6_goals WHERE run_id=?',(RUN_ID,)).fetchone()[0] == 'CLOSED'
        heads = c.execute('select version,sha256,decision_ref from v6_graph_versions order by version desc limit 1').fetchone()
        claims = c.execute('select hypothesis_id,head_revision from hypotheses order by hypothesis_id').fetchall()
        charge = c.execute('select spent_json,reserved_json from budgets where parent_ref is null').fetchone()
        token_observed = c.execute('select sum(settled_tokens) from p0_meter_native_turns').fetchone()[0]
        ids = c.execute('select job_id,status,reservation_ref from jobs order by job_id').fetchall()
        root = {'run':RUN_ID, 'status':'STOPPED', 'graph_head':heads, 'claim_heads':claims,
                'job_status_and_reservation':ids,'ledger':{'spent':json.loads(charge[0]),'reserved':json.loads(charge[1])},
                'native_settled_tokens':token_observed,'tables':unchanged}
        return root, contract


def restore(args):
    source = args.archive.resolve()
    manifest = json.loads((source/'manifest.json').read_text())
    evidence = json.loads((args.repo/'acceptance/v6/observed-run-02/operator-archive.json').read_text())
    assert digest(source/'manifest.json') == evidence['manifest_sha256']
    for key, expected in manifest.items():
        if key.startswith('state/'):
            assert digest(source/key) == expected, key
    candidate = invoke(['git','-C',args.repo,'rev-parse','--verify',args.candidate])
    object_kind = invoke(['git','-C',args.repo,'cat-file','-t',candidate])
    assert object_kind in {'tree','commit'}
    env = dict(os.environ, GX_CODEX_OFFLINE='1', GX_PYTHON=sys.executable, PYTHONDONTWRITEBYTECODE='1')
    with tempfile.TemporaryDirectory(prefix='gx-v61-observed-restore-') as root:
        work = Path(root)
        src = work/'product'
        state = work/'state'
        invoke(['git','clone','--shared','--no-checkout','--quiet',str(args.repo.resolve()),str(src)])
        def install(rev):
            if rev == candidate and object_kind == 'tree':
                invoke(['git','-C',src,'read-tree','--reset','-u',candidate])
            else:
                invoke(['git','-C',src,'checkout','-f','--detach','--quiet',rev])
        install(BASE)
        # Relocation needs the release/state identity for this sandbox. Establish it with
        # the original native owner while empty; then keep source/state paths and marker
        # bytes identical across baseline, candidate and rollback.
        invoke([sys.executable,'-B','-c',
                'import sys; from gx_core.native_release import native_state\nwith native_state(sys.argv[1]): pass',
                str(state)], env, src/'gx-h')
        marker = state/'.gx-native-state.json'
        marker_digest = digest(marker)
        shutil.copytree(source/'state',state,dirs_exist_ok=True)
        expected,old_contract = business(state)
        assert expected['tables']['jobs']['rows'] == 5
        assert expected['native_settled_tokens'] == 259718
        copy = work/'preupgrade-state'
        shutil.copytree(state,copy)
        reports = []
        def inspect(version,label):
            result = json.loads(invoke([sys.executable,'-B',str(src/'bin/gx-p0'),'resume','--state',str(state),
                       '--run-id',RUN_ID,'--timeout','1'],env,src))
            current, contract = business(state)
            assert current == expected, {k for k in current['tables'] if current['tables'][k]!=expected['tables'][k]}
            assert digest(marker)==marker_digest
            assert result['status']=='STOPPED'
            reports.append({'label':label,'code':version,'public_resume_exit':0,'contract':contract,
                            'old_business_sha256':sha(current),'graph_head':current['graph_head'],
                            'marker_sha256':marker_digest,'added_jobs':0,'added_charges':0})
            return contract
        assert inspect(BASE,'baseline restore') == old_contract
        install(candidate)
        new_sha = json.loads((src/'gx-h/contracts/CONTRACT_SHA').read_text())['__combined__']
        assert new_sha != old_contract
        assert inspect(candidate,'first cold upgrade') == new_sha
        assert inspect(candidate,'second cold reopen') == new_sha
        # Only the dedicated scratch state is discarded. Reinstall baseline and its full preupgrade copy.
        shutil.rmtree(state)
        shutil.copytree(copy,state)
        install(BASE)
        assert inspect(BASE,'rollback cold reopen') == old_contract
    return {'status':'PASS','fixture':'observed-02; original frozen OAuth V6 scientific run',
            'fixture_manifest_sha256':evidence['manifest_sha256'],'source_db_sha256':manifest['state/gx.db'],
            'base':BASE,'candidate':candidate,'candidate_object_kind':object_kind,'scope':'Fresh public writable gx-p0 resume in each process. Original custody archive read-only; no model calls.',
            'marker_handling':'Official V6 native_state established a scratch path marker before restoring the archived database and artifacts. In-place version switches kept both absolute paths and the marker bytes fixed.',
            'business':expected,'steps':reports}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo',required=True,type=Path)
    parser.add_argument('--archive',required=True,type=Path)
    parser.add_argument('--candidate',required=True)
    parser.add_argument('--out',required=True,type=Path)
    args = parser.parse_args()
    args.out.write_text(json.dumps(restore(args),indent=2)+'\n')
