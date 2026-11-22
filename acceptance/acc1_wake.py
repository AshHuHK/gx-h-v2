#!/usr/bin/env python3
"""Kill GX at an official wake ACK, then recover the same attempt through bin/gx-p0 run."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from unittest.mock import patch

from acc1_protocol import binary_facts, fixture, find_binary, _proxy_preflight
from native_probe import PACKAGE
from native_probe_peer import Peer
from gx_core import budgets
from gx_core.capabilities.shipped import ensure_grid
from gx_core.domain import clock
from gx_core.jobs.service import JobService, PURPOSE_WORKFLOW
from gx_core.native_release import native_state
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


def relay(argv):
    """Transparent stdio until the wake. Preserve the already-accepted native turn after GX dies."""
    target = Path(os.environ['GX_WAKE_ORACLE'])
    gx_pid = os.getppid()
    info = {}
    started = threading.Event()
    with subprocess.Popen([os.environ['GX_WAKE_ELF'], *argv], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as native:
        def forward_requests():
            for line in sys.stdin.buffer:
                m = json.loads(line)
                p = m.get('params', {})
                if m.get('method') == 'turn/start' and any('Resume the task.' in i.get('text', '')
                                                           for i in p.get('input', [])):
                    info.update(rpc_id=m['id'], thread=p['threadId'], client_id=p['clientUserMessageId'])
                    started.set()
                native.stdin.write(line)
                native.stdin.flush()

        reader = threading.Thread(target=forward_requests, daemon=True)
        reader.start()
        for line in native.stdout:
            m = json.loads(line)
            if not started.is_set():
                sys.stdout.buffer.write(line)
                sys.stdout.buffer.flush()
            elif m.get('id') == info['rpc_id'] and 'result' in m:
                info['official_turn_ack'] = m['result']['turn']['id']
                os.kill(gx_pid, signal.SIGKILL)
            elif m.get('method') == 'turn/completed':
                turn = m['params']['turn']
                if turn['id'] == info.get('official_turn_ack'):
                    info.update(native_completion=turn['status'], fault='SIGKILL GX immediately at official ACK')
                    target.write_text(json.dumps(info))
                    native.terminate()
                    break
    return 0


def seed(state, probe, env):
    """Build a focused same-lease Job using public GX owner APIs in test-owned state."""
    with patch.dict(os.environ, env), native_state(state), closing(store.open_store(state / 'gx.db')) as conn:
        schema.install(conn)
        budget = budgets.create_root(conn, run_id=None, profile_name='p0', actor='probe')
        run = records.create_run(conn, objective='Check a numerical Job result', project_ref='wake',
                                 actor='probe', budget_ref=budget,
                                 inputs={'inputs': {'p0': True, 'p0_spec': {'turn_token_budget': 2000}}})
        for status in ('AUDITING', 'PLANNING', 'ACTIVE'):
            store.transition(conn, machine='run', subject_id=run, to_state=status, actor='probe')
        task = records.create_task(conn, run_id=run, kind='research_round', role='evidence_reviewer', actor='probe')
        store.transition(conn, machine='task', subject_id=task, to_state='READY', actor='probe')
        store.save_checkpoint(conn, run_id=run, workflow_id='wake-probe', workflow_version='1',
                              node='await_job', state={})
        ensure_grid(JobService(conn).ctx)
        with Roots(conn, run_id=run, cwd=probe.cwd, env=env, token_budget=2000) as native:
            probe.peer.gate.clear()
            work = native.dispatch(task)
            owner = JobService(conn, actor=work['thread_id'])
            payload = {'fixture_version': '1', 'synthetic': True, 'method_id': 'fixture-grid', 'method_version': '1',
                       'candidates': [{'candidate_id': 'a', 'x': 2}], 'delay_seconds': 1}
            raw = owner.artifacts.publish_bytes(namespace=run + '/inputs', rel_path='grid.json', kind='job_input',
                         data=json.dumps(payload), run_id=run, media_type='application/json')
            manifest = owner.artifacts.make_input_manifest(namespace=run + '/grid/inputs', run_id=run,
                                           entries=[{'role': 'grid', 'artifact_ref': raw.artifact_ref}])
            job = owner.submit(run_id=run, task_id=task, lease_ref=work['lease_id'], purpose=PURPOSE_WORKFLOW,
                      capability_version='fixture-grid@1', input_manifest_ref=manifest.artifact_ref, parameters={'operation': 'wake-test'},
                      idempotency_key='one-wait-job', binding={'task_id': task, 'attempt_id': work['root_key']})
            jid = job['job_id']
            assert native.wait_job(task, jid)['waiting']
            probe.peer.gate.set()
            native.client.run_until(lambda f: f.message.get('method') == 'turn/completed' and
                            f.message['params']['turn']['id'] == work['turn_id'], timeout=10)
        return {'run': run, 'budget': budget, 'job': jid, 'task': task, 'thread': work['thread_id'],
                'initial_turn': work['turn_id'], 'lease': work['lease_id'], 'attempt': work['attempt']}


def snapshot(state, ids):
    with closing(store.open_store(state / 'gx.db')) as conn:
        def rows(sql):
            return [dict(r) for r in conn.execute(sql)]
        return {'ledger': budgets.state(conn, ids['budget']),
                'deliveries': rows('SELECT event_id,thread_id,turn_id,state,client_id,method,reservation_ref '
                                   'FROM p0_root_deliveries ORDER BY rowid'),
                'roots': rows('SELECT task_id,thread_id,lease_id,attempt,wait_job_id FROM p0_roots'),
                'tasks': rows('SELECT task_id,status,lease_id,attempt,attempts_consumed FROM tasks'),
                'leases': rows('SELECT lease_id,task_id,attempt,expires_at,released_at FROM leases'),
                'jobs': rows('SELECT job_id,status,scheduler_ref FROM jobs'),
                'usage': rows('SELECT thread_id,turn_id,total_tokens,terminal_status FROM p0_root_turn_usage')}


def measure(probe, *, expired_wait):
    pdir = probe.directory
    state = pdir / 'state'
    env = {**probe.env, 'GX_P0_TEST': '1', 'GX_P0_MODEL': 'gpt-6.1-sol', 'GX_P0_TEST_PROVIDER': probe.provider,
           'GX_P0_CODEX': str(probe.binary), 'PYTHONDONTWRITEBYTECODE': '1'}
    ids = seed(state, probe, env)
    if expired_wait:
        # Advance only the fixture worker's lease to simulate downtime without touching the Job,
        # backend result, event history, or task. The next status/reaction is the public CLI.
        time.sleep(1.5)
        with closing(store.open_store(state / 'gx.db')) as conn:
            conn.execute('UPDATE leases SET expires_at=? WHERE lease_id=?', (clock.iso_after(-2), ids['lease']))
    initial = snapshot(state, ids)
    oracle_file = pdir / 'official.json'
    cli = [str(PACKAGE / 'bin' / 'gx-p0'), 'run', '--state', str(state)]
    fault_env = {**env, 'GX_P0_CODEX': str(Path(__file__).resolve()), 'GX_WAKE_ELF': str(probe.binary),
                 'GX_WAKE_ORACLE': str(oracle_file)}
    count = len(probe.peer.rows)
    cut = subprocess.run([*cli, '--timeout', '15'], env=fault_env, capture_output=True, timeout=30)
    if cut.returncode != -signal.SIGKILL:
        return {'passed': False, 'failed_check': 'original waiter reached official wake ACK',
                'simulated_wait_lease_expiry': expired_wait, 'ids': ids, 'initial': initial,
                'public_cli_exit': cut.returncode, 'public_cli_stderr': cut.stderr.decode()[:1500],
                'after_run': snapshot(state, ids), 'provider_requests': len(probe.peer.rows),
                'checks': {'original_waiter_woken': False}}
    deadline = time.monotonic() + 15
    while not oracle_file.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert oracle_file.exists(), 'detached official turn failed to complete'
    oracle = json.loads(oracle_file.read_text())
    faulted = snapshot(state, ids)
    before = len(probe.peer.rows)
    wake, = [d for d in faulted['deliveries'] if d['event_id'].startswith('wake:')]
    assert wake['state'] == 'sending' and wake['turn_id'] is None
    assert faulted['roots'][0]['wait_job_id'] == ids['job'] and oracle['native_completion'] == 'completed'
    assert before == count + 1
    restored = subprocess.run([*cli, '--timeout', '3'], env=env, capture_output=True, timeout=20)
    assert restored.returncode in (0, 75), (restored.returncode, restored.stderr.decode()[:1000])
    after = snapshot(state, ids)
    status = json.loads(restored.stdout)
    wake_after, = [d for d in after['deliveries'] if d['event_id'].startswith('wake:')]
    # Read only public native history in a separate process connection after the unmodified CLI exited.
    probe.connect()
    turns = probe.rpc('thread/read', {'threadId': ids['thread'], 'includeTurns': True})['thread']['turns']
    native_inputs = [{'turn': turn['id'], 'status': turn['status'], 'clientId': item['clientId']}
                     for turn in turns for item in turn['items'] if item.get('type') == 'userMessage'
                     and item.get('clientId') == wake['client_id']]
    checks = {'same_native_input_once': native_inputs == [{'turn': oracle['official_turn_ack'], 'status': 'completed',
                                                           'clientId': wake['client_id']}],
              'same_gx_receipt': wake_after['state'] == 'acknowledged' and
                                 wake_after['turn_id'] == oracle['official_turn_ack'],
              'same_original_lease_attempt': (after['tasks'][0]['lease_id'], after['tasks'][0]['attempt'],
                      after['tasks'][0]['attempts_consumed']) == (ids['lease'], ids['attempt'], 0),
              'wait_marker_cleared': after['roots'][0]['wait_job_id'] is None,
              'single_real_job': len(after['jobs']) == 1 and after['jobs'] == faulted['jobs']
                                 and after['jobs'][0]['status'] == 'SUCCEEDED',
              'no_recovery_model_requests': len(probe.peer.rows) == before,
              'single_wake_charge': after['ledger']['spent'].get('tokens', 0)
                                       == initial['ledger']['spent'].get('tokens', 0) + 35
                                     and after['ledger']['spent'].get('api_calls', 0)
                                       == initial['ledger']['spent'].get('api_calls', 0) + 1
                                     and not after['ledger']['reserved']}
    return {'test': 'single Job wake lost-ACK recovery', 'scope': 'owner-API seeded worker, public gx-p0 run twice; '
            'official loopback; real numerical Job; injected OS death; 3-second recovery observation',
            'provider_requests': {'before_wake': count, 'after_wake': before, 'after_recover': len(probe.peer.rows)},
            'simulated_wait_lease_expiry': expired_wait, 'gx_sigkill_returncode': cut.returncode, 'recovery_cli_exit': restored.returncode,
            'driver_reason': status.get('driver'), 'ids': ids, 'initial': initial, 'oracle': oracle,
            'after_sigkill': faulted, 'after_recovery': after, 'native_wake_inputs': native_inputs,
            'checks': checks, 'passed': all(checks.values())}


if __name__ == '__main__':
    if 'app-server' in sys.argv[1:]:
        raise SystemExit(relay(sys.argv[1:]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--expire-wait', action='store_true')
    args = parser.parse_args()
    _proxy_preflight()
    binary = find_binary()
    with fixture(binary, peer_type=Peer) as probe:
        result = {**binary_facts(binary), **measure(probe, expired_wait=args.expire_wait)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + '\n')
    print(result['checks'])
    raise SystemExit(0 if result['passed'] else 1)
