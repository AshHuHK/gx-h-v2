"""Check persisted RunMode selection, and trace both public starts through a cold resume."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from contextlib import closing

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / 'gx-h')]
from acceptance.v6.engineering.public_cli import cli
from acceptance.v61.baseline.snapshot import dispatch_inventory
from acceptance.v61.runmode.static_upgrade import exercise as upgrade_static
from gx_core.orchestration import run_mode


def capture(state, rid):
    with closing(sqlite3.connect((state / 'gx.db').as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute('SELECT * FROM runs WHERE run_id=?', (rid,)).fetchone()
        def rows(sql):
            return [dict(row) for row in db.execute(sql, (rid,))]
        return {
            'implementation': type(run_mode.for_run(row)).__name__,
            'status': row['status'],
            'workflow': [row['workflow_id'], row['workflow_version'], row['workflow_sha256']],
            'tasks': rows('SELECT task_id,kind,status FROM tasks WHERE run_id=? ORDER BY task_id'),
            'jobs': rows('SELECT job_id,idempotency_key,reservation_ref,status FROM jobs WHERE run_id=? ORDER BY job_id'),
            'checkpoints': rows('SELECT seq,node FROM checkpoints WHERE run_id=? ORDER BY seq'),
            'graph_heads': rows('SELECT * FROM v6_graph_versions WHERE run_id=? ORDER BY version'),
            'budget': rows('SELECT spent_json,reserved_json FROM budgets WHERE run_id=? ORDER BY budget_ref'),
        }


def exercise(state, args, out, implementation):
    state.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, 'GX_CODEX_OFFLINE': '1'}
    trace = []
    def call(action, *options, expected=0):
        result = cli(state, env, action, *options, expected_exit=expected)
        trace.append(result)
        (out / (implementation + '-cli.json')).write_text(json.dumps(trace, indent=2) + '\n')
        return result['body']

    opened = call('start', '--run-key', 'mode-check', *args)
    rid = opened['run_id']
    seen = {'opened': capture(state, rid)}
    cold = call('status', '--run-id', rid)
    assert cold['tasks'] == opened['tasks']
    assert call('start', '--run-key', 'mode-check', *args)['run_id'] == rid
    assert capture(state, rid) == seen['opened'], 'duplicate start affected the Run'
    call('stop', '--run-id', rid)
    seen['paused'] = capture(state, rid)
    assert call('status', '--run-id', rid)['status'] == 'PAUSED'
    if implementation == 'StaticWorkflow':
        # Wait for the real workers' outcomes before asking the driver to reconcile.
        # Waiting for the files makes the assertion independent of host speed.
        deadline = time.monotonic() + 30
        for job in seen['opened']['jobs']:
            while not (state / 'jobs' / job['job_id'] / 'outcome.json').exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"CPU worker did not publish outcome for {job['job_id']}")
                time.sleep(.1)
    call('resume', '--run-id', rid, '--timeout', '2', expected=75)
    call('status', '--run-id', rid)
    seen['cold_resumed'] = capture(state, rid)
    def identity(job):
        return (job['job_id'], job['idempotency_key'], job['reservation_ref'])

    for phase in seen.values():
        assert phase['implementation'] == implementation
        assert phase['workflow'] == seen['opened']['workflow']
        assert list(map(identity, phase['jobs'])) == list(map(identity, seen['opened']['jobs']))
    if implementation == 'ResearchGoal':
        assert opened['graph_head'] is None and opened['waiting_for']
        assert seen['opened']['workflow'] == [None, None, None]
        assert not seen['opened']['checkpoints'] and not seen['cold_resumed']['graph_heads']
        assert len(cold['tasks']) == 1
        assert all(s['tasks'] == seen['opened']['tasks'] and s['budget'] == seen['opened']['budget']
                   for s in seen.values())
    else:
        assert seen['opened']['workflow'][:2] == ['science_grid', '3']
        assert seen['opened']['checkpoints'] and len(seen['opened']['jobs']) == 2
        # GX can settle already submitted Jobs and unblock a later task during cold resume.
        assert seen['cold_resumed']['jobs'] and seen['cold_resumed']['tasks']
        assert all(job['status'] == 'SUCCEEDED' for job in seen['cold_resumed']['jobs'])
        call('status', '--run-id', rid)
        assert capture(state, rid) == seen['cold_resumed']
    return {'run_id': rid, 'snapshots': seen}


def main(out):
    out.mkdir(parents=True, exist_ok=True)
    inventory = dispatch_inventory(ROOT / 'gx-h/gx_core')
    for key in ('goal_markers', 'direct_mode_comparisons', 'grid_equality', 'other_grid_predicates'):
        assert not inventory[key], (key, inventory[key])
    with tempfile.TemporaryDirectory(prefix='gx-v61-modes-') as tmp:
        tmp = Path(tmp)
        # Static task and dataset come from the released example.
        spec = json.loads((ROOT / 'examples/paired-grid.json').read_text())
        spec.update(workflow='science_grid', workflow_version='3', plugins=['org.gx.science-grid@3.0.0'])
        path = tmp / 'grid.json'
        path.write_text(json.dumps(spec))
        static = exercise(tmp / 'static/state', ['--spec', path], out, 'StaticWorkflow')
        goal = exercise(tmp / 'goal/state', ['--goal', 'Determine what the authorized measurements support.'],
                        out, 'ResearchGoal')
        upgrade = upgrade_static(tmp / 'prior_release', out)
    return {'passed': True, 'static': static, 'goal': goal, 'prior_release_static': upgrade, 'scan': inventory,
            'scope': 'Public CLI entrypoints, independent process per operation. Model offline; the official '
                     'V6-STATIC and V6-LOOPBACK cases independently cover model callbacks through completion.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    result = main(args.out.resolve())
    (args.out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
