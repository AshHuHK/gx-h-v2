"""Resume an actual paused V6 science_grid@3 Run using its original methods and charges."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tarfile
import time
from contextlib import closing

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT / 'gx-h'),str(ROOT / 'acceptance/v61')]
from migration import install, published
from gx_core import contracts
from gx_core.errors import ContentHashMismatch, VersionImmutable
from gx_core.orchestration import repin, service
from gx_core.p0.driver import context
from gx_core.persistence import store
from gx_core.teams.manager import TeamManager

BASE = '47fc728d32bc10ba478df6a063529a8d98f98828'


def command(repo, state, action, *args, expected=0):
    argv = [sys.executable, '-B', str(repo / 'bin/gx-p0'), action, '--state', str(state), *map(str, args)]
    p = subprocess.run(argv, capture_output=True, text=True, timeout=60, env={**os.environ, 'GX_CODEX_OFFLINE': '1'})
    if p.returncode != expected:
        raise RuntimeError(f'{action} exited {p.returncode}: {p.stderr}')
    return json.loads(p.stdout)


def rows(conn, sql):
    return [dict(row) for row in conn.execute(sql)]


def facts(state):
    with closing(sqlite3.connect((state / 'gx.db').as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        run, = rows(conn, 'SELECT run_id,status,workflow_id,workflow_version,contract_sha256,workflow_sha256,'
                    'agent_policy_sha256 FROM runs')
        return {'run': run, 'schema': store.read_meta(conn)['contract_sha256'],
                'jobs': rows(conn, 'SELECT job_id,idempotency_key,reservation_ref,status FROM jobs ORDER BY job_id'),
                'budget': rows(conn, "SELECT spent_json,reserved_json FROM budgets WHERE run_id IS NOT NULL"),
                'tasks': rows(conn, 'SELECT task_id,kind,status FROM tasks ORDER BY task_id')}


def prepare(baseline, work, fixture):
    state = work / 'state'
    spec = json.loads((baseline/'examples/paired-grid.json').read_text())
    spec.update(workflow='science_grid', workflow_version='3', plugins=['org.gx.science-grid@3.0.0'])
    path = work/'spec.json'
    path.write_text(json.dumps(spec))
    started = command(baseline, state, 'start','--run-key','v61-static-upgrade','--spec',path)
    rid = started['run_id']
    command(baseline, state, 'stop','--run-id',rid)
    deadline = time.monotonic()+15
    while not all((state/'jobs'/j['job_id']/'outcome.json').exists() for j in started['jobs']):
        if time.monotonic()>deadline:
            raise RuntimeError('Baseline CPU workers did not finish')
        time.sleep(.1)
    with closing(sqlite3.connect(state/'gx.db')) as conn:
        conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    read = facts(state)
    assert read['run']['status'] == 'PAUSED' and len(read['jobs']) == 2
    assert all(j['status'] == 'SUBMITTED' for j in read['jobs'])
    with tarfile.open(fixture, 'w:gz') as tar:
        tar.add(state, arcname='state', filter=lambda entry: None if entry.name.endswith(('-wal','-shm')) else entry)
    manifest = {'source': BASE, 'sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(), 'state': read,
                'model_calls': 0, 'scope': 'Two real fixture CPU Jobs completed while the released Run was paused. '
                'GX had yet to settle either Job. No model was started.'}
    return manifest


def exercise(work, out, repo=ROOT):
    work.mkdir(parents=True, exist_ok=True)
    deploy = work/'installed'
    published(repo,deploy)
    state = work/'state'
    archive = out/'paused-grid-v6.tar.gz'
    fixture = prepare(deploy, work, archive)
    # Keep the stopped installation and the quiescent source intact. The
    # candidate sees the copy through the exact same source/state paths.
    state.rename(work/'original-state')
    deploy.rename(work/'released-code')
    install(ROOT, deploy)
    with tarfile.open(archive) as t:
        t.extractall(work, filter='data')
    before = facts(state)
    assert before == fixture['state']
    rid = before['run']['run_id']
    resumed = command(deploy, state, 'resume', '--run-id', rid, '--timeout', '2', expected=75)
    after = facts(state)
    pins = ['contract_sha256','workflow_sha256','agent_policy_sha256','workflow_id','workflow_version']
    assert all(after['run'][k] == before['run'][k] for k in pins)
    assert after['schema'] == contracts.contract_digest()['__combined__'] != before['schema']
    def identities(s):
        return [(j['job_id'],j['idempotency_key'],j['reservation_ref']) for j in s['jobs']]
    assert identities(before) == identities(after)
    assert all(j['status']=='SUCCEEDED' for j in after['jobs']) and after['tasks']
    command(deploy, state, 'resume','--run-id',rid,'--timeout','0.5',expected=75)
    assert facts(state) == after
    with closing(store.open_store(state/'gx.db')) as conn:
        ctx = context(conn)
        run = conn.execute('SELECT * FROM runs WHERE run_id=?',(rid,)).fetchone()
        lease = conn.execute('SELECT lease_id FROM coordinator_leases WHERE run_id=?',(rid,)).fetchone()[0]
        args = dict(action='create',payload={'intent':{'objective':'Review the settled grid outcomes.'}},
                    run_id=rid,expected_revision=store.current_revision(conn,rid),
                    coordinator_lease_ref=lease,idempotency_key='v61-static-team')
        team = TeamManager(ctx).manage(**args)
        assert TeamManager(ctx).manage(**args)['replayed'] and team['team']['run_id'] == rid
        flow = service._pinned_workflow(conn, run)
        assert repin.ensure(ctx, run, flow)["contract_sha256"] == before["run"]["contract_sha256"]
        assert not conn.execute("SELECT 1 FROM approvals WHERE action_kind='run_repin' AND run_id=?", (rid,)).fetchone()
        with service.pin_store(conn):
            service.require_pins(run, flow)
        denied = {}
        for field, error in [('contract_sha256',ContentHashMismatch),('workflow_sha256',VersionImmutable),
                             ('agent_policy_sha256',ContentHashMismatch)]:
            try:
                service.require_store_pins(conn, {**dict(run),field:'f'*64}, flow)
            except error as exc:
                denied[field] = exc.code
            else:
                raise AssertionError('Modified pin accepted: '+field)
        resync = [json.loads(row[0]) for row in conn.execute(
            "SELECT payload_json FROM events WHERE kind='store.contract_resynced'")]
        assert any(r.get('from_sha256') == before['schema'] and r.get('to_sha256') == after['schema'] for r in resync)
        with closing(store.open_store(':memory:')) as fresh:
            assert not store.contract_pin_matches(fresh,before['schema'],after['schema'])
            # A test store with the correct contract pair and wrong source DDL must still reject.
            wrong = {**resync[-1], 'from_db_schema_sha256':'0'*64}
            store.append_event(fresh,kind='store.contract_resynced',actor='backend',subject_kind='store',
                               evidence_level='EXECUTION_OBSERVED',payload=wrong)
            assert not store.contract_pin_matches(fresh,before['schema'],after['schema'])
    return {'baseline':BASE,'archive_sha256':fixture['sha256'],'before':before,'after':after,'cold_repeat_unchanged':True,
            'resume_status':resumed['status'],'migration_receipts':resync,'team_id':team['team']['team_id'],
            'team_replay':True,'repin_approvals':0,'wrong_pins':denied,'missing_receipt_rejected':True,'wrong_source_ddl_rejected':True}


if __name__ == '__main__':
    import argparse
    import tempfile
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,default=ROOT,help='Git lookup for released V6 commit')
    parser.add_argument('--out',type=Path,required=True)
    a = parser.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as work:
        result = exercise(Path(work),a.out.resolve(),a.baseline.resolve())
    (a.out/'result.json').write_text(json.dumps(result,indent=2)+'\n')
