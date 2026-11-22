"""A pending Responses stream emits rate-limit headers before an operator interrupts it."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

from acc1_protocol import binary_facts, fixture, find_binary, rollout_records, _proxy_preflight
from native_probe import DYN_SPEC
from native_probe_peer import DYNAMIC, Handler, Peer, tool
from gx_core import budgets
from gx_core.errors import GxError
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


class LimitHeaders(Handler):
    def do_POST(self):
        if self.path != '/v1/responses':
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        row, n = self.server.record(body)
        ident = row['id']
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('x-codex-primary-used-percent', '12.0')
        self.send_header('x-codex-primary-window-minutes', '15')
        self.send_header('x-codex-primary-reset-at', '1953000000')
        self.end_headers()

        def send(event):
            self.wfile.write(('data: ' + json.dumps(event) + '\n\n').encode())
            self.wfile.flush()

        try:
            send({'type': 'response.created', 'response': {'id': ident}})
            if n == 2:
                self.server.started.set()
                self.server.release.wait(15)
                return
            item = tool(DYNAMIC, {}, ident) if n == 1 else self.server.message(ident, '4')
            send({'type': 'response.output_item.done', 'item': item})
            send({'type': 'response.completed', 'response': {'id': ident, 'usage': row['usage']}})
        except (BrokenPipeError, ConnectionResetError):
            pass


class InterruptedPeer(Peer):
    def __init__(self):
        super().__init__()
        self.RequestHandlerClass = LimitHeaders
        self.started = threading.Event()
        self.release = threading.Event()


def measure(probe):
    env = {**probe.env, 'GX_P0_TEST': '1', 'GX_P0_MODEL': 'gpt-6.1-sol',
           'GX_P0_TEST_PROVIDER': probe.provider, 'GX_P0_CODEX': str(probe.binary)}
    db = probe.directory / 'gx.db'
    with patch.dict(os.environ, env):
        def connect(conn):
            return Roots(conn, run_id=run, cwd=probe.cwd, env=env, token_budget=1000,
                         main_tools=[DYN_SPEC], main_call=lambda tool, args, envelope: {'ok': True})

        def observe(roots):
            original = roots.client.on_notification
            probe.gen += 1
            def recorded(frame):
                probe.notification(frame)
                original(frame)
            roots.client.on_notification = recorded

        def state(conn):
            return {'ledger': budgets.state(conn, budget),
                    'turns': [dict(r) for r in conn.execute('SELECT * FROM p0_root_turn_usage')],
                    'deliveries': [dict(r) for r in conn.execute('SELECT event_id,thread_id,turn_id,state '
                                                                 'FROM p0_root_deliveries ORDER BY rowid')]}

        def followup(roots, uid):
            try:
                turn = roots.human_turn('Report the result.', message_id=uid)
            except GxError as exc:
                return {'code': exc.code, 'message': str(exc)}
            roots.client.run_until(lambda f: f.message.get('method') == 'turn/completed'
                                   and f.message['params']['turn']['id'] == turn, timeout=10)
            return {'turn': turn}

        with closing(store.open_store(db)) as conn:
            schema.install(conn)
            budget = budgets.create_root(conn, run_id=None, profile_name='p0', actor='probe')
            run = records.create_run(conn, objective='Measure a cancelled model exchange',
                                     project_ref='interrupt', actor='probe', budget_ref=budget)
            with connect(conn) as roots:
                observe(roots)
                root = roots.ensure_main()
                turn = roots.human_turn('Use the arithmetic check, then report.', message_id='original')
                deadline = time.monotonic() + 10
                while not probe.peer.started.is_set() and time.monotonic() < deadline:
                    roots.poll(.05)
                assert probe.peer.started.is_set()
                # Let the native stream consume the already-flushed response headers and first SSE event.
                # The exact number of observed native counts below verifies the injection actually worked.
                roots.client.run_until(lambda f: False, timeout=2, poll_interval=.35)
                roots.client.request('turn/interrupt', {'threadId': root, 'turnId': turn})
                terminal = roots.client.run_until(lambda f: f.message.get('method') == 'turn/completed'
                    and f.message['params']['turn']['id'] == turn, timeout=12).message['params']['turn']
                assert terminal['status'] == 'interrupted'
                probe.peer.release.set()
                original_records = rollout_records(probe.home)
                after_interrupt = state(conn)
                end_count = len(probe.peer.rows)
                warm = followup(roots, 'warm-followup')
                warm_requests = len(probe.peer.rows) - end_count

        with closing(store.open_store(db)) as conn, connect(conn) as roots:
            observe(roots)
            assert roots.ensure_main() == root
            for _ in range(3):
                roots.poll(.05)
            after_cold_open = state(conn)
            cold_start = len(probe.peer.rows)
            cold = followup(roots, 'cold-followup')
            cold_requests = len(probe.peer.rows) - cold_start
            final = state(conn)

        frames = [f for f in probe.log if f.get('thread') == root and f.get('turn') == turn and
                  f['method'] in ('turn/started','turn/completed','rawResponse/completed','thread/tokenUsage/updated')]
        totals = [f['tokenUsage']['total']['totalTokens'] for f in frames
                  if f['method'] == 'thread/tokenUsage/updated' and f['generation'] == 1]
        responses = [f['usage']['totalTokens'] for f in frames if f['method'] == 'rawResponse/completed']
        checks = {'one_response_two_snapshots': responses == [35] and totals == [35,35],
                  'warm_preserved_hold': not after_interrupt['ledger']['spent'] and
                                          after_interrupt['ledger']['reserved'] == {'tokens':1000,'api_calls':1} and
                                          len(after_interrupt['ledger']['reservations']) == 1,
                  'no_warm_successor': 'code' in warm and warm_requests == 0,
                  'cold_preserved_hold': not after_cold_open['ledger']['spent'] and
                                          bool(after_cold_open['ledger']['reserved']),
                  'no_cold_successor': 'code' in cold and cold_requests == 0,
                  'one_original_reservation': len(final['ledger']['reservations']) == 1}
        return {'source': 'GX Roots + pinned official App Server, loopback SSE endpoint', 'run': run,
                'thread': root, 'turn': turn, 'native_terminal': terminal['status'], 'frames': frames,
                'rollout_types': original_records, 'paid_responses': responses, 'live_totals': totals,
                'warm_before': after_interrupt, 'warm_attempt': warm, 'warm_added_requests': warm_requests,
                'cold_before': after_cold_open, 'cold_attempt': cold, 'cold_added_requests': cold_requests,
                'final': final, 'checks': checks, 'passed': all(checks.values())}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    _proxy_preflight()
    binary = find_binary()
    with fixture(binary, peer_type=InterruptedPeer) as probe:
        try:
            result = {**binary_facts(binary), **measure(probe)}
        finally:
            probe.peer.release.set()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2) + '\n')
    print(result['checks'])
    raise SystemExit(0 if result['passed'] else 1)
