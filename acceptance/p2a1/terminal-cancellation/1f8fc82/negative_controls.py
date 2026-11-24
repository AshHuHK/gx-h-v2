"""Adversarial receipt controls replaying a genuine 0.169.0 cancellation after dropping both live mirrors."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

release, evidence = (Path(arg).resolve() for arg in sys.argv[1:])
sys.path[:0] = [str(release / 'gx-h'), str(release / 'acceptance/v31')]
from terminal_protocol_probe import CANCELLATION_REASON, verify_cancellation

source = json.loads((evidence / 'lost-mirrors.json').read_text())['trials'][0]
actual = source['cancellation_proof']['receipt']
assert source['cancellation_proof']['live_sequence'] is None
assert {f['method'] for f in source['injected_receipt_faults']} == {'rawResponseItem/completed', 'item/completed'}
assert not source['post_terminal_model_requests'] and not source['tool_responses_written']
results = {}
cases = ('original', 'absent_rollout', 'no_completion', 'foreign_thread', 'foreign_turn', 'foreign_call',
         'other_tool', 'other_reason', 'duplicate_core', 'normal_tool_response', 'post_commit_request',
         'contradictory_public_history')
for case in cases:
    trial = deepcopy(source)
    for field in ('cancellation_proof', 'cancellation_diagnostics'):
        trial.pop(field, None)
    core = deepcopy(actual)
    payload = core['payload']
    item = payload['item']
    if case == 'foreign_thread':
        payload['thread_id'] = trial['turn_id']
    if case == 'foreign_turn':
        payload['turn_id'] = trial['thread_id']
    if case == 'foreign_call':
        item['id'] = 'call-from-another-submission'
    if case == 'other_tool':
        item['tool'] = 'gx_read_artifact'
    if case == 'other_reason':
        item['error'] = 'tool reported a different failure'
    if case == 'normal_tool_response':
        trial['tool_responses_written'].append({'id':trial['request_id'],'result':{'contentItems':[], 'success':False}})
    if case == 'post_commit_request':
        injected = deepcopy(trial['model_requests'][0])
        injected['t'] = trial['committed_at'] + 1
        trial['model_requests'].append(injected)
    if case == 'contradictory_public_history':
        trial['turn_history']['call']['tool'] = 'gx_read_artifact'
    with tempfile.TemporaryDirectory(prefix='gx-receipt-replay-') as tmp:
        sessions = Path(tmp) / 'sessions'
        sessions.mkdir()
        if case != 'absent_rollout':
            records = [] if case == 'no_completion' else [core, core] if case == 'duplicate_core' else [core]
            archive = sessions / ('rollout-fixture-' + trial['thread_id'] + '.jsonl')
            archive.write_text(''.join(json.dumps(record)+'\n' for record in records))
        roots = SimpleNamespace(env={'CODEX_HOME':tmp})
        # The replay stream has reached EOF, just after the bounded live-drain deadline.
        with patch('terminal_protocol_probe.time.monotonic', side_effect=[0,2]):
            try:
                verify_cancellation(roots,trial)
            except AssertionError:
                outcome = 'rejected'
            else:
                outcome = 'proved'
        assert outcome == ('proved' if case == 'original' else 'rejected'), case
        diagnostic = trial.get('cancellation_diagnostics')
        results[case] = {'outcome':outcome, 'matching_core_count':None if diagnostic is None else
                         len(diagnostic.get('same_scope_core_completions', []))}
assert actual['payload']['item']['error'] == CANCELLATION_REASON
(evidence / 'negative-controls.json').write_text(json.dumps({
    'type':'scripted counterfactual replay of the captured original, with genuine control and modified negative inputs',
    'source_trial':source['id'], 'thread':source['thread_id'], 'turn':source['turn_id'],
    'call':source['call_id'], 'cases':results},indent=2)+'\n')
print(f'{len(cases)-1} negative controls rejected, genuine persisted receipt verified')
