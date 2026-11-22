"""Observe production Roots notification selection, account admission, and lease renewal via official stdio."""
import argparse
from collections import Counter
from contextlib import closing, contextmanager
import json
import os
from pathlib import Path
import threading
from unittest.mock import patch

from native_events import EventProbe, RatePeer, TEXT_DELTA, validate_schema
from native_budget import wait
from acc1_protocol import binary_facts, fixture
from native_probe import DYN_SPEC, find_binary, _proxy_preflight
from native_probe_peer import DYNAMIC, Peer, tool
from gx_core import budgets, config
from gx_core.domain import clock
from gx_core.errors import GxError
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store

EXPECTED = {'item/agentMessage/delta', 'item/plan/delta', 'item/reasoning/textDelta',
            'item/reasoning/summaryTextDelta', 'item/commandExecution/outputDelta', 'item/fileChange/outputDelta'}


@contextmanager
def project(probe, grant=100, grace=1):
    env = {**probe.env, 'GX_P0_TEST':'1','GX_P0_TEST_PROVIDER':probe.provider,
           'GX_P0_MODEL':'gpt-6.1-sol','GX_P0_CODEX':str(probe.binary)}
    try:
        with patch.dict(os.environ, env), closing(store.open_store(probe.directory/'gx.db')) as conn:
            schema.install(conn)
            budget = budgets.create_root(conn,run_id=None,profile_name='p0',actor='fixture',
                                         limits={'tokens':10000,'api_calls':25})
            run = records.create_run(conn,objective='Independent arithmetic measurements',project_ref='native-activity',
                                     actor='fixture',budget_ref=budget,host={'policy_selection':{'test_mode':True,
                                     'model_provider':probe.provider,'test_base_url':f'http://127.0.0.1:{probe.peer.server_port}/v1'}},inputs={'inputs':{'p0':True,
                                     'p0_spec':{'turn_token_budget':grant,'metering':{'grace_seconds':grace}}}})
            for status in ('AUDITING','PLANNING','ACTIVE'):
                store.transition(conn,machine='run',subject_id=run,to_state=status,actor='fixture')
            yield conn, run, budget, env
    finally:
        clock.reset()


def task_ready(conn, run):
    task = records.create_task(conn,run_id=run,kind='research_round',role='evidence_reviewer',actor='fixture')
    store.transition(conn,machine='task',subject_id=task,to_state='READY',actor='fixture')
    return task


def capture(roots, probe):
    listener = roots.client.on_notification
    def notification(frame):
        probe.notification(frame)
        listener(frame)
    roots.client.on_notification = notification


def gate_outcome(action):
    try:
        result = action()
    except GxError as exc:
        return {'code':exc.code,'details':exc.details}
    return {'succeeded':True,'turn':result}


def routing(probe):
    with project(probe) as (conn, run, budget, env):
        callbacks = []
        def callback(name, args, envelope):
            callbacks.append({**envelope,'name':name})
            return {'value':4}
        def connection():
            return Roots(conn,run_id=run,cwd=probe.cwd,env=env,token_budget=100,
                         main_tools=[DYN_SPEC],main_call=callback)
        roots = connection()
        sent = []
        request = roots._request
        def trace(method, params):
            if method=='initialize':
                sent.append(dict(params['capabilities']))
            return request(method, params)
        roots._request = trace
        probe.peer.used_percent=100
        with roots:
            capture(roots,probe)
            turn=roots.human_turn('Compute 2+2 using the supplied tool.',message_id='one')
            assert wait(roots,lambda:any(e.get('turn')==turn and e['method']=='turn/completed' for e in probe.log))
            after = budgets.state(conn,budget)
            counts = Counter(e['method'] for e in probe.log if e.get('direction')=='notification')
            task = task_ready(conn,run)
            before=len(probe.peer.rows)
            refused=gate_outcome(lambda:roots.dispatch(task))
            unchanged=dict(conn.execute('SELECT status,attempt,lease_id FROM tasks WHERE task_id=?',(task,)).fetchone())
            routes=[dict(r) for r in conn.execute('SELECT * FROM p0_provider_limits')]
        with connection() as roots:
            capture(roots,probe)
            denied=gate_outcome(lambda:roots.human_turn('Return 5.',message_id='two'))
            blocked_calls=len(probe.peer.rows)-before
            blocked_budget=budgets.state(conn,budget)
            if 'succeeded' in denied:
                wait(roots,lambda:any(e['method']=='turn/completed' and e.get('turn')==denied['turn'] for e in probe.log))
            clock.advance(max(0,probe.peer.reset_at-int(clock.now().timestamp()))+2)
            probe.peer.used_percent=12
            resumed_outcome=gate_outcome(lambda:roots.human_turn('Return 5.',message_id='two'))
            resumed=resumed_outcome.get('turn')
            if resumed:
                assert wait(roots,lambda:any(e.get('turn')==resumed and e['method']=='turn/completed' for e in probe.log))
            resumed_calls=len(probe.peer.rows)-before-blocked_calls
        checks={'production_init_exactly_six':len(sent)==1 and set(sent[0]['optOutNotificationMethods'])==EXPECTED,
                'streamed_text_delta_filtered':counts[TEXT_DELTA]==0,
                'turn_item_usage_rate_and_raw_preserved':all(counts[k]>0 for k in ('turn/started','turn/completed',
                     'item/started','item/completed','thread/tokenUsage/updated','account/rateLimits/updated','rawResponse/completed')),
                'actual_tool_callback':len(callbacks)==1 and callbacks[0]['turn_id']==turn,
                'admitted_turn_drained':after['spent']=={'api_calls':1,'tokens':70},
                'new_task_rejected_before_claim':refused.get('details',{}).get('reason')=='provider_backoff' and
                      unchanged['status']=='READY' and not unchanged['lease_id'] and unchanged['attempt']==0,
                'cold_existing_root_backoff':denied.get('details',{}).get('reason')=='provider_backoff' and blocked_calls==0 and
                      len(blocked_budget['reservations'])==len(after['reservations']),
                'admitted_after_provider_reset':resumed!=turn and resumed_calls==1}
        return {'run':run,'initialize':sent,'native_turn':turn,'notification_counts':dict(counts),'callbacks':callbacks,
                'provider_rows':routes,'admitted_ledger':after,'blocked_task':refused,'task_after_refusal':unchanged,
                'cold_existing_root':denied,'blocked_model_calls':blocked_calls,'blocked_ledger':blocked_budget,
                'after_reset_model_calls':resumed_calls,'followup':resumed_outcome,'checks':checks}


class LeasePeer(Peer):
    def __init__(self):
        super().__init__()
        self.requests=[threading.Event() for _ in range(3)]
        self.responses=[threading.Event() for _ in range(3)]

    def item(self,row,index):
        self.requests[index-1].set()
        if not self.responses[index-1].wait(20):
            raise TimeoutError('fixture response unreleased')
        return tool(DYNAMIC,{},row['id']) if index<3 else self.message(row['id'],'Finished')


def lease(probe):
    with project(probe) as (conn,run,budget,env):
        task=task_ready(conn,run)
        def current():
            return dict(conn.execute('SELECT t.status,t.attempt,l.lease_id,l.expires_at,l.principal FROM tasks t '
                                     'JOIN leases l ON t.lease_id=l.lease_id WHERE t.task_id=?',(task,)).fetchone())
        callbacks=[]
        def callback(binding,name,args,envelope):
            callbacks.append({**envelope,**current(),'at':clock.now_iso(),'task':binding['task_id']})
            return {'arithmetic':4}
        with Roots(conn,run_id=run,cwd=probe.cwd,env=env,token_budget=100,
                   worker_tools=[{**DYN_SPEC,'type':'function'}],worker_call=callback) as roots:
            capture(roots,probe)
            try:
                work=roots.dispatch(task)
                assert wait(roots,lambda:probe.peer.requests[0].is_set())
                before=current()
                ttl=config.limits()['task_budget']['lease_seconds']
                clock.advance(ttl-300)
                # The normal poll sees fresh native progress (including the allowed turns/list check).
                probe.peer.responses[0].set()
                assert wait(roots,lambda:len(callbacks)==1 and probe.peer.requests[1].is_set())
                clock.advance(450)
                probe.peer.responses[1].set()
                assert wait(roots,lambda:probe.peer.requests[2].is_set())
                after=current()
                renewals=[json.loads(r['payload_json']) for r in conn.execute(
                    "SELECT payload_json FROM events WHERE kind='task.lease_renewed' AND subject_id=?",(task,))]
                receipts=[dict(r) for r in conn.execute('SELECT thread_id,call_id,turn_id FROM p0_root_calls')]
            finally:
                for gate in probe.peer.responses:
                    gate.set()
        checks={'two_accepted_callbacks_on_same_attempt':len(callbacks)==2 and all(
                    c['thread_id']==work['thread_id'] and c['turn_id']==work['turn_id'] and
                    c['lease_id']==before['lease_id'] and c['attempt']==before['attempt'] for c in callbacks),
                'second_callback_after_original_expiry':len(callbacks)==2 and callbacks[1]['at']>before['expires_at'],
                'same_lease_renewed':after['lease_id']==before['lease_id'] and after['expires_at']>before['expires_at'] and
                    any(r['lease_id']==before['lease_id'] and r['attempt']==before['attempt'] for r in renewals),
                'two_durable_tool_receipts':len(receipts)==2}
        return {'run':run,'task':task,'initial':before,'after':after,'callbacks':callbacks,'renewals':renewals,
                'durable_tool_receipts':receipts,'default_lease_seconds':ttl,
                'clock':'GX time advanced by default TTL minus 300 seconds, then by 450 seconds; real native process and callbacks.',
                'checks':checks}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=['routing','lease'],required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    _proxy_preflight()
    binary=find_binary()
    with fixture(binary,peer_type=RatePeer if args.case=='routing' else LeasePeer,probe_type=EventProbe) as probe:
        result=(routing if args.case=='routing' else lease)(probe)
    result.update(**binary_facts(binary),schema_sha256=validate_schema(EXPECTED),scope='official app server, local scripted provider, actual GX')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2)+'\n')
    print(result['checks'])
    return 0 if all(result['checks'].values()) else 1

if __name__=='__main__':
    raise SystemExit(main())
