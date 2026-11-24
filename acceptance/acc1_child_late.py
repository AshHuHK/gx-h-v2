"""Exercise the parent GX reservation while its verified native child is still running."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from acc1_protocol import MeterPeer, binary_facts, fixture, find_binary, _proxy_preflight
from native_probe_peer import CHILD
from gx_core import budgets
from gx_core.errors import GxError
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


class DelayedChildRequestPeer(MeterPeer):
    """Keep the delegated HTTP request outstanding until the test releases the child."""
    def record(self, body):
        if any(item.get('recipient') == '/root/' + CHILD for item in body.get('input', [])):
            if not self.release_child.wait(20):
                raise TimeoutError('the original child request was never released')
        return super().record(body)


def followup_request(before, after):
    return (len(after) == len(before) + 1 and after[:-1] == before
            and not after[-1]['child'] and after[-1]['scenario'] == 'plain')


def measure(probe):
    env = {**probe.env, 'GX_P0_TEST': '1', 'GX_P0_MODEL': 'gpt-6.1-sol',
           'GX_P0_TEST_PROVIDER': probe.provider, 'GX_P0_CODEX': str(probe.binary)}
    probe.peer.scenario('delayed_child')
    with patch.dict(os.environ, env), closing(store.open_store(probe.directory / 'gx.db')) as conn:
        schema.install(conn)
        budget = budgets.create_root(conn, run_id=None, profile_name='p0', actor='meter-probe')
        run = records.create_run(conn, objective='Check asynchronous delegated arithmetic',
                                 project_ref='late-child', actor='meter-probe', budget_ref=budget)
        with Roots(conn, run_id=run, cwd=probe.cwd, env=env, token_budget=1000) as roots:
            root = roots.ensure_main()
            initial_turn = roots.human_turn('Delegate a single arithmetic check and return.')
            roots.client.run_until(lambda f: f.message.get('method') == 'turn/completed'
                                   and f.message['params']['threadId'] == root, timeout=12)
            def state():
                with probe.peer.lock:
                    requests = [{key: row[key] for key in ('id', 'child', 'scenario')} for row in probe.peer.rows]
                return {'ledger': budgets.state(conn, budget),
                        'provider_requests': requests,
                        'deliveries': [dict(r) for r in conn.execute('SELECT event_id,turn_id,state '
                                                                      'FROM p0_root_deliveries')],
                        'child': [dict(r) for r in conn.execute('SELECT * FROM p0_native_child_meters')],
                        'child_usage': [dict(r) for r in conn.execute('SELECT * FROM p0_native_child_usage')]}
            parent_done = state()
            assert parent_done['child_usage'] and parent_done['ledger']['reserved'], parent_done
            second_input = 'Return the result of 2 + 2.'
            try:
                premature = {'turn': roots.human_turn(second_input, message_id='followup')}
            except GxError as exc:
                premature = {'error': exc.code, 'explanation': str(exc)}
            before_release = state()
            # This independently records the public-call outcome, even if the implementation
            # under test admits the second turn incorrectly.
            if 'turn' in premature:
                probe.peer.scenario('plain')
            probe.peer.release_child.set()
            roots.client.run_until(lambda f: f.message.get('method') == 'turn/completed'
                                   and f.message['params']['threadId'] != root, timeout=15)
            deadline = time.monotonic() + 6
            while time.monotonic() < deadline and not budgets.state(conn, budget)['spent']:
                roots.poll(.1)
            after = state()
            checks = {'parent_held_at_completion': bool(parent_done['ledger']['reserved']),
                      'no_followup_turn_while_child_pending': 'turn' not in premature,
                      'no_new_reservation_while_child_pending': len(parent_done['ledger']['reservations']) ==
                                                                len(before_release['ledger']['reservations']),
                      'original_parent_and_child_requests': (
                          [r for r in parent_done['provider_requests'] if not r['child']] ==
                          [r for r in after['provider_requests'] if not r['child']]
                          and sum(not r['child'] for r in after['provider_requests']) == 2
                          and sum(r['child'] for r in after['provider_requests']) == 1),
                      'correct_child_attribution': after['child'][0]['root_id'] == root and
                           after['child'][0]['metadata_verified'] == 1 and
                           after['child_usage'][0]['owner_turn'] == initial_turn,
                      'settled_once': after['ledger']['spent'] == {'tokens': 253, 'api_calls': 1}
                                      and not after['ledger']['reserved']}
            if all(checks.values()):
                # A plain single-call response avoids creating a second child or tool call.
                probe.peer.scenario('plain')
                probe.peer.item = lambda row, n: probe.peer.message(row['id'], '4')
                followup = roots.human_turn(second_input, message_id='followup')
                roots.client.run_until(lambda f: f.message.get('method') == 'turn/completed'
                                       and f.message['params']['turn']['id'] == followup, timeout=10)
                final = state()
                checks['followup_then_settled'] = final['ledger']['spent'] == {'tokens': 288, 'api_calls': 2}
                checks['one_followup_model_request'] = followup_request(after['provider_requests'], final['provider_requests'])
            else:
                final = state()
            return {'run': run, 'root': root, 'original_turn': initial_turn, 'parent_completed': parent_done,
                    'premature_request': premature, 'still_waiting': before_release, 'child_completed': after,
                    'after_followup': final, 'checks': checks, 'passed': all(checks.values())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--delay-child-request', action='store_true',
                        help='admit the original child HTTP request after the parent completes')
    args = parser.parse_args()
    _proxy_preflight()
    binary = find_binary()
    with fixture(binary, peer_type=DelayedChildRequestPeer if args.delay_child_request else MeterPeer) as probe:
        result = {**binary_facts(binary), **measure(probe)}
    args.out.parent.mkdir(exist_ok=True, parents=True)
    args.out.write_text(json.dumps(result, indent=2) + '\n')
    print(result['checks'])
    raise SystemExit(0 if result['passed'] else 1)
