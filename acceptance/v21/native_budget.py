"""Two simultaneously active native GX Roots, gate their provider usage, observe hard-budget interrupts."""
import argparse
from collections import Counter
from contextlib import closing
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

ACCEPTANCE = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ACCEPTANCE))
from acc1_protocol import binary_facts, fixture
from native_probe import DYN_SPEC, find_binary, _proxy_preflight
from native_probe_peer import DYNAMIC, Handler, Peer, tool
from gx_core import budgets
from gx_core.domain import clock
from gx_core.errors import GxError
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


class HoldResponse(Handler):
    def do_POST(self):
        if self.path!='/v1/responses':
            self.send_error(404)
            return
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        inputs=json.dumps(body.get('input',[]))
        route=next((task for task in self.server.gates if task in inputs), None)
        if not route and 'Prime local quota' in inputs:
            ident = 'prime-provider-quota'
            usage = {'input_tokens':0,'output_tokens':0,'total_tokens':0}
            payload = ''.join('data: '+json.dumps(row)+'\n\n' for row in (
                {'type':'response.created','response':{'id':ident}},
                {'type':'response.output_item.done','item':self.server.message(ident,'Ready')},
                {'type':'response.completed','response':{'id':ident,'usage':usage}})).encode()
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('x-codex-primary-used-percent','12')
            self.send_header('x-codex-primary-window-minutes','15')
            self.send_header('x-codex-primary-reset-at',str(int(time.time())+120))
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        if not route:
            self.send_error(400,'Unknown fixture task')
            return
        with self.server.lock:
            counts=self.server.counts
            counts[route]+=1
            index=counts[route]
        if index==1:
            self.server.ready[route].set()
            self.server.gates[route].wait(20)
        else:
            self.server.next_requests[route].set()
            self.server.done.wait(20)
        ident=f'{route}-{index}'
        first=self.server.quota[route]
        item=tool(DYNAMIC,{},ident) if index==1 else self.server.message(ident,'Computed')
        usage={'input_tokens':first-5,'output_tokens':5,'total_tokens':first} if index==1 else None
        payload=b''.join(('data: '+json.dumps(row)+'\n\n').encode() for row in
               [{'type':'response.created','response':{'id':ident}},
                {'type':'response.output_item.done','item':item},
                {'type':'response.completed','response':{'id':ident,'usage':usage}}])
        try:
            self.send_response(200)
            self.send_header('Content-Type','text/event-stream')
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError):
            pass


class BudgetPeer(Peer):
    def __init__(self):
        super().__init__()
        self.RequestHandlerClass=HoldResponse
        self.gates,self.ready,self.next_requests,self.quota={},{},{},{}
        self.counts=Counter()
        self.done=threading.Event()

    def route(self,task,actual):
        self.gates[task]=threading.Event()
        self.ready[task]=threading.Event()
        self.next_requests[task]=threading.Event()
        self.quota[task]=actual


def wait(roots,predicate,timeout=6):
    deadline=time.monotonic()+timeout
    while not predicate() and time.monotonic()<deadline:
        roots.poll(.03)
    return bool(predicate())


def measure(probe,baseline):
    env={**probe.env,'GX_P0_TEST':'1','GX_P0_TEST_PROVIDER':probe.provider,
         'GX_P0_MODEL':'gpt-6.1-sol','GX_P0_CODEX':str(probe.binary)}
    with patch.dict(os.environ,env), closing(store.open_store(probe.directory/'gx.db')) as conn:
        schema.install(conn)
        host={'policy_selection':{'model_provider':probe.provider,'test_mode':True,
                                 'test_base_url':f'http://127.0.0.1:{probe.peer.server_port}/v1'}}
        if not baseline:
            # The ordinary low-quota notification lets this custom provider admit parallel work.
            # Charge that one fixture request to a separate run and budget.
            primer_budget=budgets.create_root(conn,run_id=None,profile_name='p0',actor='probe')
            primer=records.create_run(conn,objective='Discover provider quota',project_ref='prime-quota',
                                      actor='probe',budget_ref=primer_budget,host=host)
            with Roots(conn,run_id=primer,cwd=probe.cwd,env=env,token_budget=20) as priming:
                prime_turn=priming.human_turn('Prime local quota',message_id='quota-observation')
                assert wait(priming,lambda:conn.execute('SELECT 1 FROM p0_root_turn_usage WHERE turn_id=? '
                                    "AND terminal_status='completed'",(prime_turn,)).fetchone())
                assert conn.execute('SELECT 1 FROM p0_provider_limits WHERE provider=?',(probe.provider,)).fetchone()
        budget=budgets.create_root(conn,run_id=None,profile_name='p0',actor='probe',limits={'tokens':100,'api_calls':5})
        run=records.create_run(conn,objective='Run two independent arithmetic checks',project_ref='multi-meter',
            actor='probe',budget_ref=budget,host=host, inputs={'inputs':{'p0':True,'p0_spec':{
                'turn_token_budget':40,'metering':{'grace_seconds':1}}}})
        for status in ('AUDITING','PLANNING','ACTIVE'):
            store.transition(conn,machine='run',subject_id=run,to_state=status,actor='probe')
        tasks=[]
        for actual in (30,73):
            task=records.create_task(conn,run_id=run,kind='research_round',role='evidence_reviewer',actor='probe')
            store.transition(conn,machine='task',subject_id=task,to_state='READY',actor='probe')
            tasks.append(task)
            probe.peer.route(task,actual)
        calls=[]
        def callback(binding,name,args,envelope):
            calls.append({**envelope,'task':binding['task_id']})
            return {'ok':True}
        with Roots(conn,run_id=run,cwd=probe.cwd,env=env,token_budget=40,
                   worker_tools=[{**DYN_SPEC,'type':'function'}],worker_call=callback) as roots:
            listener=roots.client.on_notification
            traces=[]
            if not baseline:
                from gx_core.p0 import metering
            def capture(frame):
                probe.notification(frame)
                listener(frame)
                if not baseline and frame.message['method'] in ('rawResponse/completed','thread/tokenUsage/updated'):
                    params=frame.message['params']
                    traces.append({**metering.inspect(conn,run)['risk'],'native_method':frame.message['method'],
                                   'thread_id':params.get('threadId'),'turn_id':params.get('turnId'),
                                   'response_id':params.get('responseId')})
            roots.client.on_notification=capture
            work=[roots.dispatch(task) for task in tasks]
            ids={w['thread_id']:w['turn_id'] for w in work}
            assert len(ids)==2 and wait(roots,lambda:all(e.is_set() for e in probe.peer.ready.values()))
            before=budgets.state(conn,budget)
            def relevant(method):
                return [f for f in probe.log if f['method']==method and f.get('turn')==ids.get(f.get('thread'))]
            try:
                for task in tasks:
                    probe.peer.gates[task].set()
                    # Each native streamed update is observed before the succeeding fixture transition.
                    assert wait(roots,lambda:len(relevant('thread/tokenUsage/updated'))>=tasks.index(task)+1)
                interrupted=wait(roots,lambda:len(relevant('turn/completed'))>=2,timeout=2 if baseline else 8)
                observed=budgets.state(conn,budget)
                if not baseline:
                    clock.advance(2)
                    roots.poll(.05)
                    inspect=metering.inspect(conn,run)
                    prior_requests=dict(probe.peer.counts)
                    try:
                        roots.human_turn('Third paid call',message_id='over-cap')
                    except GxError as exc:
                        refused={'code':exc.code,'details':exc.details}
                    else:
                        refused={}
                    refusal_extra_requests=dict(probe.peer.counts)!=prior_requests
                    observed=budgets.state(conn,budget)
                    clock.reset()
                else:
                    inspect={}
            finally:
                probe.peer.done.set()
                for gate in probe.peer.gates.values():
                    gate.set()
            terminals=[{k:e[k] for k in ('thread','turn','status')} for e in relevant('turn/completed')]
            usage=[{'thread':e['thread'],'turn':e['turn'],'count':e['tokenUsage']['total']['totalTokens']}
                    for e in relevant('thread/tokenUsage/updated')]
            checks={}
            if not baseline:
                checks={'parallel_grants':before['reserved'].get('tokens')==80,
                        'reported_103_while_two_active':any(r['observed_in_flight_tokens']==103 and r['active_root_count']==2 for r in traces),
                        'threshold_observed_on_raw_before_interrupt':any(r['native_method']=='rawResponse/completed'
                             and r['observed_plus_settled']==103 and r['active_root_count']==2 for r in traces),
                        'both_native_interrupt_terminals':len(terminals)==2 and all(t['status']=='interrupted' for t in terminals),
                        'interrupt_receipts_match_exact_turns':{(i['thread_id'],i['turn_id']) for i in inspect['interrupts']
                                  if i['sent_at'] and i['ended_at'] and i['terminal_status']=='interrupted'}==set(ids.items()),
                        'both_charges_final_or_marked':len(inspect['turns'])==2 and all(t['budget_status']=='SETTLED' and
                                  t['confidence'] in ('PROVIDER_CONFIRMED','ESTIMATED','PROVIDER_RECONCILED') for t in inspect['turns']),
                        'hard_limit_blocks_further_paid_work':bool(refused) and not refusal_extra_requests,
                        'honest_unbounded_provider_inflight_exposure':all(r['provider_maximum_overshoot_tokens'] is None for r in traces)}
            return {'run':run,'checks':checks,'risk_trace':traces,'denied_after_limit':refused if not baseline else None,'task_ids':tasks,'native_turns':ids,'provider_inflight_counts':dict(probe.peer.counts),
                    'initial_ledger':before,'after_update':observed,'official_usage':usage,'official_terminals':terminals,
                    'tool_callbacks':calls,'metering':inspect,'reached_terminal_condition':interrupted,
                    'checked_hard_cap':100,'root_grants':[40,40],
                    'guard_observed':bool(terminals) and any(t['status']=='interrupted' for t in terminals)}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--baseline',action='store_true')
    args=parser.parse_args()
    _proxy_preflight()
    binary=find_binary()
    with fixture(binary,peer_type=BudgetPeer) as probe:
        result={**binary_facts(binary),**measure(probe,args.baseline)}
    result['mode']='official native stdio, 2 concurrent real GX Roots, scripted model usage; temporary SQLite'
    result['expected']='pre-change hard limit is not enforced' if args.baseline else 'bounded interrupt on hard limit'
    assert len(result['official_usage'])>=2
    result['passed']=not result['guard_observed'] if args.baseline else (result['guard_observed'] and all(result['checks'].values()))
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2)+'\n')
    print(result['passed'],result['official_usage'],result['official_terminals'])
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
