"""Measure usage, replay and late native children on pinned Codex with a loopback Responses peer."""
import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import threading

from native_probe import Probe, find_binary, _proxy_preflight
from native_probe_peer import CHILD, DYNAMIC, Peer, tool


class MeterPeer(Peer):
    def __init__(self):
        super().__init__()
        self.release_child = threading.Event()

    def item(self, row, n):
        ident = row['id']
        if self.plan == 'delayed_child':
            if row['child']:
                if not self.release_child.wait(20):
                    raise RuntimeError('child gate timed out')
            elif n == 1:
                return tool('spawn_agent', {'task_name': CHILD, 'fork_turns': 'none',
                            'message': 'Return a single number.'}, ident, 'collaboration')
        elif n == 1:
            return tool(DYNAMIC, {}, ident)
        elif self.plan == 'missing_last_usage':
            row['usage'] = None
        return self.message(ident, 'Complete')


class MeterProbe(Probe):
    def notification(self, frame):
        super().notification(frame)
        params = frame.message['params']
        if frame.message['method'] in ('turn/started', 'turn/completed'):
            self.log[-1]['rootTurnId'] = params['turn'].get('rootTurnId')
        if frame.message['method'] == 'rawResponse/completed':
            self.log[-1].update(responseId=params['responseId'], usage=params['usage'])
        if frame.message['method'] == 'thread/tokenUsage/updated':
            self.log[-1]['tokenUsage'] = frame.message['params']['tokenUsage']


def usage_events(probe, thread, generation):
    return [event for event in probe.log if event.get('thread') == thread and event.get('generation') == generation
            and event.get('method') == 'thread/tokenUsage/updated']


def history(probe, tid):
    thread = probe.rpc('thread/read', {'threadId': tid, 'includeTurns': True})['thread']
    return {'id': thread['id'], 'parentThreadId': thread.get('parentThreadId'), 'source': thread.get('source'),
            'sessionId': thread.get('sessionId'), 'status': thread['status'],
            'turns': [{'id': row['id'], 'status': row['status'], 'rootTurnId': row.get('rootTurnId'), 'items': [item['type'] for item in row['items']]}
                      for row in thread['turns']]}


def replay(probe, tid):
    response = probe.rpc('thread/resume', {'threadId': tid})['thread']
    after = probe.client.last_received.receive_seq
    event = probe.event('thread/tokenUsage/updated', tid)
    seq = usage_events(probe, tid, probe.gen)[-1]['seq']
    assert seq > after and response['id'] == tid
    return {'rpc_seq': after, 'notification_seq': seq, **event,
            'turns': [{'id': t['id'], 'status': t['status'], 'rootTurnId': t.get('rootTurnId')} for t in response['turns']]}


def rollout_records(home):
    # Diagnostic only. Select public event kinds, correlation IDs, and scalar meters.
    # No user/model text, reasoning, credentials, or private event bodies are exported.
    records = {}
    for path in (home / 'sessions').rglob('rollout-*.jsonl'):
        stream = []
        for line in path.read_text().splitlines():
            record = json.loads(line)
            payload = record.get('payload', {})
            kind = payload.get('type')
            entry = {'record': record['type']}
            if kind:
                entry['kind'] = kind
            for key in ('turn_id', 'root_turn_id', 'response_id', 'thread_id'):
                if payload.get(key):
                    entry[key] = payload[key]
            for key in ('token_usage', 'usage'):
                if isinstance(payload.get(key), dict):
                    entry[key] = payload[key].get('total_tokens')
            if kind == 'token_count':
                entry['total_tokens'] = (payload.get('info') or {}).get('total_token_usage', {}).get('total_tokens')
            stream.append(entry)
        records[path.name] = stream
    return records


def run_case(probe, scenario):
    probe.peer.scenario(scenario)
    probe.connect()
    root = probe.thread(experimentalRawEvents=True)
    turn = probe.start_turn(root, 'Measure the input and output of this arithmetic check.')
    probe.finish(root, turn)
    root_at_end = usage_events(probe, root, probe.gen)
    children = {}
    if scenario == 'delayed_child':
        child, = {e['agent_thread_id'] for e in probe.log if e.get('kind') == 'started' and e.get('thread') == root}
        assert not usage_events(probe, child, probe.gen), 'child meter arrived before root completion'
        probe.peer.release_child.set()
        child_turn = probe.event('turn/completed', child)['turn']
        assert child_turn['status'] == 'completed'
        child_history = history(probe, child)
        assert (child_history['parentThreadId'] == child_history['source']['subAgent']['thread_spawn']['parent_thread_id']
                == child_history['sessionId'] == root)
        children[child] = {'turn': child_turn['id'], 'history': child_history,
                           'usage': usage_events(probe, child, probe.gen)}
    root_history = history(probe, root)
    # The fault controller sees the wire. A disconnected GX receiver keeping only the first
    # sample cannot see the later meter or its relationship to completion until reconciliation.
    records = rollout_records(probe.home)
    probe.kill()
    probe.connect()
    requests_before = len(probe.peer.rows)
    root_replay = replay(probe, root)
    for child, facts in children.items():
        # A separate read-only history call can verify ancestry; only this protocol probe
        # explicitly resumes the child to characterize what root-only recovery can observe.
        facts['after_parent_resume'] = usage_events(probe, child, probe.gen)
        facts['replay'] = replay(probe, child)
        assert facts['replay']['turnId'] == facts['turn']
        assert facts['replay']['tokenUsage']['total']['totalTokens'] == 183
    assert len(probe.peer.rows) == requests_before
    assert root_replay['turnId'] == turn, (root_replay, root_history)
    totals = [e['tokenUsage']['total']['totalTokens'] for e in root_at_end]
    expected = 35 if scenario == 'missing_last_usage' else 70
    assert totals[0] == 35 and totals[-1] == expected and root_replay['tokenUsage']['total']['totalTokens'] == expected
    return {'rollout_record_shapes': records, 'root_thread': root, 'original_turn': turn, 'root_history': root_history,
            'scripted_responses': [{'id': r['id'], 'child': r['child'], 'usage': r['usage']} for r in probe.peer.rows],
            'root_live_totals': totals, 'root_cold_replay': root_replay, 'children': children,
            'new_provider_requests_during_replay': len(probe.peer.rows) - requests_before,
            'official_frames': [e for e in probe.log if e['method'] in ('thread/resume', 'thread/tokenUsage/updated',
                           'turn/started', 'turn/completed', 'rawResponse/completed', 'SIGKILL') or e.get('item_type') == 'subAgentActivity']}


@contextmanager
def fixture(binary, *, peer_type=MeterPeer, probe_type=MeterProbe):
    with tempfile.TemporaryDirectory(prefix='gx-acc1-protocol-') as tmp:
        peer = peer_type()
        server = threading.Thread(target=peer.serve_forever, daemon=True)
        server.start()
        probe = probe_type(Path(tmp) / 'official', binary, peer)
        try:
            yield probe
        finally:
            peer.gate.set()
            if hasattr(peer, 'release_child'):
                peer.release_child.set()
            probe.close()
            peer.shutdown()
            peer.server_close()
            server.join()


def binary_facts(binary):
    version = subprocess.check_output([str(binary), '--version'], text=True).strip()
    assert version == 'codex-cli 0.169.0', version
    with binary.open('rb') as handle:
        return {'version': version, 'sha256': hashlib.file_digest(handle, 'sha256').hexdigest()}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', choices=('two_samples', 'missing_last_usage', 'delayed_child'), required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    _proxy_preflight()
    binary = find_binary()
    facts = binary_facts(binary)
    with fixture(binary) as probe:
        facts.update(case=args.case, transport='official stdio; loopback scripted Responses')
        facts.update(run_case(probe, args.case))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(facts, indent=2) + '\n')
    print(args.out, facts['root_live_totals'])
