"""Exercise each original receipt fault plus a real in-progress crossing and an unproved negative."""
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

source, runtime, out = (Path(s).resolve() for s in sys.argv[1:])
env = {**os.environ, 'PYTHONPATH':str(source/'gx-h')+os.pathsep+str(source/'acceptance')}
entry = source/'acceptance/v31/terminal_protocol_probe.py'
cases = ('lost_interrupt','lost_receipts','delayed_receipts','lost_receipts_in_progress')
commands = []
for case in cases:
    argv = [sys.executable,'-B',str(entry),'--only',case,'--repetitions','1','--closure-usage',
            '--runtime',str(runtime),'--output',str(out/(case+'.json'))]
    with (out/(case+'.log')).open('w') as log:
        completed = subprocess.run(argv,cwd=source,env=env,stdout=log,stderr=subprocess.STDOUT)
    commands.append({'argv':argv,'exit':completed.returncode})
    (out/'commands.json').write_text(json.dumps(commands,indent=2)+'\n')
    assert completed.returncode==0, (case, completed.returncode)

# Duplicate the truthful first native in-progress observation, but never release
# the queued outbound interrupt. No fake terminal frame or history is supplied.
sys.path[:0] = [env['PYTHONPATH'].split(os.pathsep)[0], str(entry.parent)]
import terminal_protocol_probe as protocol
from gx_core.hostobs.app_server_client import AppServerTimeout
partial = out/'never-forwarded.partial.json'
with patch.object(protocol.Wire, 'release_original_interrupt', lambda self, history: None):
    try:
        protocol.run(1,'lost_receipts_in_progress',closure_usage=True,runtime=runtime,partial_output=partial)
    except AppServerTimeout as failure:
        reason = str(failure)
    else:
        raise AssertionError('a never-forwarded interrupt was unexpectedly verified')
original=json.loads(partial.read_text())
trial,=original['unfinished_trials']
assert original['completed_trial_count']==0 and original['passed'] is False
assert trial['interrupt_history'] and {h['status'] for h in trial['interrupt_history']}=={'inProgress'}
assert len(trial['interrupt_writes'])==1 and not trial['server_interrupt_replies']
assert not trial['tool_responses_written'] and not trial.get('cancellation_proof')
assert len(trial['model_requests'])==1 and trial['model_requests'][0]['t']<=trial['committed_at']
assert 'original_interrupt_released_after_history' not in trial
(out/'negative.json').write_text(json.dumps({'result':'EXPECTED_FAIL','reason':reason,'history_reads':len(trial['interrupt_history']),
    'scope':{'thread':trial['thread_id'],'turn':trial['turn_id'],'call':trial['call_id']},
    'outbound_interrupt_writes':len(trial['interrupt_writes']),'forwarded':0,'normal_responses':0,
    'post_commit_model_requests':0,'task_result':trial['committed']['result']},indent=2)+'\n')
print('Four scoped official recoveries passed; an original Turn that remained in progress was rejected.')
