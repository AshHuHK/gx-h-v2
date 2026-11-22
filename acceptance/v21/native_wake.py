"""Follow the public crash-recovered Job wake through its configured normal billing grace."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

from native_cumulative import accounting
from native_budget import wait
from gx_core.errors import GxError
from gx_core.native_release import native_state
from gx_core.orchestration.service import run_control
from gx_core.p0.driver import context
from gx_core.p0.roots import Roots
from acc1_protocol import binary_facts, fixture
from acc1_wake import measure as lost_ack
from native_probe import PACKAGE, find_binary, _proxy_preflight
from native_probe_peer import Peer
from gx_core.domain import clock
from gx_core.p0 import metering
from gx_core.persistence import store


def measure(probe):
    original=lost_ack(probe,expired_wait=True)
    probe.close()
    ids=original['ids']
    state=probe.directory/'state'
    db=state/'gx.db'
    env={**probe.env,'GX_P0_TEST':'1','GX_P0_MODEL':'gpt-6.1-sol','GX_P0_TEST_PROVIDER':probe.provider,
         'GX_P0_CODEX':str(probe.binary),'PYTHONDONTWRITEBYTECODE':'1'}
    def view():
        with closing(store.open_store(db)) as conn:
            return accounting(conn,ids['run'],ids['budget'])
    paused=subprocess.run([str(PACKAGE/'bin/gx-p0'),'stop','--state',str(state),'--run-id',ids['run']],
                          env=env,capture_output=True,timeout=15)
    assert paused.returncode==0,paused.stderr.decode()[:800]
    before=view()
    wake=next(t for t in before['turns'] if t['event_id'].startswith('wake:'))
    with closing(store.open_store(db)) as conn:
        grace=metering.policy(conn,ids['run'])['grace_seconds']
    # Use the default grace and the real wall clock. No GX state or clocks are edited here.
    remaining=max(.05,grace-clock.age_seconds(wake['terminal_at'])+.2)
    time.sleep(remaining)
    runs=[]
    model_count=len(probe.peer.rows)
    for _ in range(2):
        event=subprocess.run([str(PACKAGE/'bin/gx-p0'),'run','--state',str(state),'--run-id',ids['run'],'--timeout','2'],
                             env=env,capture_output=True,timeout=20)
        assert event.returncode in (0,75),(event.returncode,event.stderr.decode()[:800])
        runs.append({'exit':event.returncode,'model_requests':len(probe.peer.rows),'ledger':view()})
    later=runs[0]['ledger']
    end=runs[1]['ledger']
    wake=next(t for t in later['turns'] if t['event_id'].startswith('wake:'))
    first=next(t for t in later['turns'] if t['turn_id']==ids['initial_turn'])
    prior=original['initial']['ledger']['spent']
    checks={k:v for k,v in original['checks'].items() if k!='single_wake_charge'}
    checks.update(
        observed_complete_native_wake=original['oracle']['native_completion']=='completed',
        exact_attempt_charge_rows=len(later['turns'])==2 and len({t['reservation_ref'] for t in later['turns']})==2,
        default_grace_ended_before_accounting=clock.age_seconds(wake['terminal_at'])>=grace,
        wake_charged_once_at_known_confidence=wake['budget_status']=='SETTLED' and wake['settled_tokens']>0 and (
            (wake['confidence']=='ESTIMATED' and wake['settled_tokens']==max(wake['token_budget'],wake['observed_lower_bound'])
             and wake['measured_tokens'] is None and wake['observed_lower_bound']==35) or
            (wake['confidence']=='PROVIDER_CONFIRMED' and wake['measured_tokens']==wake['settled_tokens']==35)),
        net_accounts_explain_both_turns=later['spent']['api_calls']==prior['api_calls']+1 and
            later['spent']['tokens']==first['settled_tokens']+wake['settled_tokens']+
            sum(x['delta'] for x in later['adjustments']) and not later['reserved'],
        no_extra_model_call_job_or_charge=all(r['model_requests']==model_count for r in runs) and later==end)
    followup={}
    with patch.dict(os.environ,env), native_state(state), closing(store.open_store(db)) as conn:
        run_control(context(conn),{'run_id':ids['run'],'action':'continue',
                    'reason':'probe explicitly requests a same-owner followup after accounting',
                    'idempotency_key':'explicit-followup-resume'},defer_advance=True)
        def connect():
            return Roots(conn,run_id=ids['run'],cwd=probe.cwd,env=env,token_budget=2000)
        event_id='operator:independent-followup-measurement'
        text='Perform one additional independent arithmetic measurement.'
        try:
            with connect() as roots:
                listener=roots.client.on_notification
                observations=[]
                def observed(frame):
                    if frame.message.get('method')=='thread/tokenUsage/updated':
                        params=frame.message['params']
                        observations.append({'thread':params['threadId'],'turn':params['turnId'],
                                             'cumulative':params['tokenUsage']['total']['totalTokens']})
                    listener(frame)
                roots.client.on_notification=observed
                receipt=roots.deliver(ids['thread'],text,event_id=event_id)
                status=wait(roots,lambda:conn.execute("SELECT 1 FROM p0_root_turn_usage WHERE turn_id=? "
                            "AND terminal_status='completed'",(receipt['turn_id'],)).fetchone())
                assert status
                followup={'turn':receipt['turn_id'],'thread':receipt['thread_id'],'event_id':event_id,
                          'after':view(),'official_observations':observations,'new_requests':len(probe.peer.rows)-model_count}
            with connect() as roots:
                replayed=roots.deliver(ids['thread'],text,event_id=event_id)
                followup.update(replayed_turn=replayed['turn_id'],after_replay=view(),
                                replay_new_requests=len(probe.peer.rows)-model_count-followup['new_requests'])
        except GxError as exc:
            followup['refused']={'code':exc.code,'reason':str(exc),'details':exc.details}
            followup['task']=dict(conn.execute('SELECT status,attempt,lease_id FROM tasks WHERE task_id=?',(ids['task'],)).fetchone())
    followup['checkpoint_status']='observed' if 'after' in followup else 'unavailable-on-original-lease'
    if 'after' in followup:
        account=followup['after']
        checks['authorized_successor_has_new_turn_and_one_request']=(followup['turn']!=wake['turn_id'] and
                        followup['thread']==ids['thread'] and followup['new_requests']==1)
        checks['subsequent_cumulative_reconciles_original_estimate']=(account['spent']['tokens']==105 and
                        account['spent']['api_calls']==3 and len(account['adjustments'])==1 and
                        account['adjustments'][0]['reservation_ref']==wake['reservation_ref'] and
                        account['adjustments'][0]['reported_total']==105 and account['adjustments'][0]['delta']==35-2000)
        checks['matching_native_late_counter_and_unknown_original_cost']=(
            any(c=={'thread':ids['thread'],'turn':followup['turn'],'cumulative':105} for c in followup['official_observations'])
            and any(t['reservation_ref']==wake['reservation_ref'] and t['settled_tokens']==2000 and t['measured_tokens'] is None
                      and t['attribution']=='UNATTRIBUTED_THREAD_USAGE' for t in account['turns']))
        checks['authorized_successor_replay_no_effect']=(followup.get('replayed_turn')==followup['turn'] and
                        followup.get('replay_new_requests')==0 and followup.get('after_replay')==followup['after'])
    else:
        checks['released_attempt_refuses_further_charge']=(
            followup.get('refused',{}).get('code')=='LEASE_EXPIRED' and followup.get('task',{}).get('lease_id') is None
            and len(probe.peer.rows)==model_count and view()==end)
    return {'scope':'Official wake ACK followed by real GX SIGKILL. Fresh default v2.1 store. Public CLI charged/replayed '
                    'after wall-clock grace. The original short observation and exact-unit expectation are retained.',
            'short_observation':original,'public_stop_exit':paused.returncode,'paused_during_grace':True,'grace_seconds':grace,'additional_wait_seconds':remaining,'before_grace':before,
            'after_public_recovery':runs,'explicit_followup':followup,'checks':checks}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    _proxy_preflight()
    binary=find_binary()
    with fixture(binary,peer_type=Peer) as probe:
        result=measure(probe)
    result.update(**binary_facts(binary))
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2)+'\n')
    print(result['checks'])
    raise SystemExit(0 if all(result['checks'].values()) else 1)
