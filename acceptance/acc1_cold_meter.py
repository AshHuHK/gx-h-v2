"""Retain only one intermediate native meter in GX, then test the public same-thread recovery."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
from unittest.mock import patch

from acc1_protocol import binary_facts, fixture, find_binary, _proxy_preflight
from native_probe import DYN_SPEC
from gx_core import budgets
from gx_core.errors import GxError
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


def measure(probe, scenario):
    env = {**probe.env, 'GX_P0_TEST': '1', 'GX_P0_MODEL': 'gpt-6.1-sol',
           'GX_P0_TEST_PROVIDER': probe.provider, 'GX_P0_CODEX': str(probe.binary)}
    db = probe.directory / 'gx.db'
    probe.peer.scenario(scenario)
    with patch.dict(os.environ, env):
        def connect(conn):
            return Roots(conn, run_id=run, cwd=probe.cwd, env=env, token_budget=1000,
                         main_tools=[DYN_SPEC], main_call=lambda tool, args, envelope: {'fixture': 'ok'})

        def state(conn):
            return {'ledger': budgets.state(conn, budget),
                    'usage': [dict(r) for r in conn.execute('SELECT * FROM p0_root_turn_usage')],
                    'deliveries': [dict(r) for r in conn.execute('SELECT event_id,thread_id,turn_id,state '
                                                                 'FROM p0_root_deliveries')]}

        with closing(store.open_store(db)) as conn:
            schema.install(conn)
            budget = budgets.create_root(conn, run_id=None, profile_name='p0', actor='probe')
            run = records.create_run(conn, objective='Measure two calls in one turn', project_ref='meter',
                                     actor='probe', budget_ref=budget)
            with connect(conn) as roots:
                root = roots.ensure_main()
                listener = roots.client.on_notification
                cutoff = []
                def before_cut(frame):
                    msg = frame.message
                    if cutoff:
                        return
                    listener(frame)
                    if msg['method'] == 'thread/tokenUsage/updated' and msg['params']['threadId'] == root:
                        cutoff.append(msg['params']['tokenUsage']['total']['totalTokens'])
                roots.client.on_notification = before_cut
                turn = roots.human_turn('Call the arithmetic check, then finish.', message_id='original')
                roots.client.run_until(lambda f: f.message.get('method') == 'turn/completed'
                                       and f.message['params']['turn']['id'] == turn, timeout=12)
                assert cutoff == [35]
                initial = state(conn)
                assert initial['ledger']['reserved'] and len(probe.peer.rows) == 2
                roots.client.transport.process.kill()
                roots.client.transport.process.wait(3)
        # Discard all GX and App Server objects. The new client can only use existing public
        # operations, the real persisted history and the first GX observation from above.
        with closing(store.open_store(db)) as conn, connect(conn) as roots:
            assert roots.ensure_main() == root
            for _ in range(3):
                roots.poll(.05)
            recovered = state(conn)
            before = len(probe.peer.rows)
            probe.peer.scenario('plain')
            probe.peer.item = lambda row, n: probe.peer.message(row['id'], '4')
            outcome = None
            try:
                followup = roots.human_turn('Return 2+2.', message_id='followup')
            except GxError as exc:
                outcome = {'code': exc.code, 'message': str(exc)}
            else:
                roots.client.run_until(lambda f: f.message.get('method') == 'turn/completed'
                                       and f.message['params']['turn']['id'] == followup, timeout=8)
                outcome = {'turn': followup}
            final = state(conn)
            paid = len(probe.peer.rows) - before
            if scenario == 'missing_last_usage':
                checks = {'held_until_attributable': not recovered['ledger']['spent'] and
                          bool(recovered['ledger']['reserved']), 'zero_followup_model_calls': paid == 0,
                          'refused_before_followup_reservation': 'code' in outcome and len(final['deliveries']) == 1
                              and len(final['ledger']['reservations']) == 1}
            else:
                checks = {'cold_recovered_final_70': recovered['ledger']['spent'] == {'tokens': 70, 'api_calls': 1}
                              and not recovered['ledger']['reserved'], 'allowed_one_followup': paid == 1,
                          'correct_two_turn_charge': final['ledger']['spent'] == {'tokens': 105, 'api_calls': 2}
                              and not final['ledger']['reserved']}
            return {'run': run, 'root': root, 'turn': turn, 'case': scenario, 'initial': initial,
                    'first_GX_sample': cutoff, 'cold_after_observation': recovered, 'followup_result': outcome,
                    'followup_model_requests': paid, 'after_followup': final, 'checks': checks,
                    'passed': all(checks.values())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', required=True, choices=('two_samples','missing_last_usage'))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    _proxy_preflight()
    binary = find_binary()
    with fixture(binary) as probe:
        result = {**binary_facts(binary), **measure(probe, args.case)}
    args.out.parent.mkdir(exist_ok=True, parents=True)
    args.out.write_text(json.dumps(result, indent=2) + '\n')
    print(result['checks'])
    raise SystemExit(0 if result['passed'] else 1)
