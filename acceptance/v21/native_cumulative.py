"""Missing provider usage, later cumulative correction and cold duplicate delivery with unavailable GX rollout access."""
import argparse
from contextlib import closing, contextmanager, ExitStack
import json
import os
from pathlib import Path
import sys

from native_activity import capture, project
from native_budget import wait
from native_events import EventProbe
from acc1_protocol import binary_facts, fixture
from native_probe import DYN_SPEC, find_binary, _proxy_preflight
from gx_core import budgets
from gx_core.domain import clock
from gx_core.p0 import metering
from gx_core.p0.roots import Roots
from gx_core.persistence import store


@contextmanager
def prohibit_rollout_access(home):
    """GX has no permission to open these files. Native Codex retains its own independent storage."""
    events=[]
    active=[True]
    def deny(kind, args):
        if kind!='open' or not active[0] or not isinstance(args[0],(bytes,str)):
            return
        path=Path(os.fsdecode(args[0]))
        if path.name.startswith('rollout-') and path.suffix=='.jsonl' and path.is_relative_to(home):
            events.append({'operation':kind,'file_type':'official_rollout_jsonl','gx_result':'PermissionError'})
            raise PermissionError('The probe denies GX access to the native rollout')
    sys.addaudithook(deny)
    try:
        yield events
    finally:
        active[0]=False


def accounting(conn,run,budget):
    view=metering.inspect(conn,run)
    account=budgets.state(conn,budget)
    return {'turns':view['turns'],'adjustments':view['adjustments'],'threads':view['threads'],
            'spent':account['spent'],'reserved':account['reserved'],'reservation_count':len(account['reservations'])}


def measure(probe, path):
    probe.peer.scenario('missing_last_usage')
    with prohibit_rollout_access(probe.home) as denials, project(probe,grant=100,grace=1) as (conn,run,budget,env):
        def connection(db):
            return Roots(db,run_id=run,cwd=probe.cwd,env=env,token_budget=100,main_tools=[DYN_SPEC],
                         main_call=lambda name,args,envelope:{'arithmetic':4})
        def terminal(roots,turn):
            return wait(roots,lambda:any(f['method']=='turn/completed' and f.get('turn')==turn for f in probe.log))
        with ExitStack() as active:
            roots=active.enter_context(connection(conn))
            capture(roots,probe)
            thread=roots.ensure_main()
            first=roots.human_turn('Call the arithmetic tool and conclude.',message_id='first')
            assert terminal(roots,first)
            initial=accounting(conn,run,budget)
            clock.advance(2)
            roots.poll(.05)
            estimate=accounting(conn,run,budget)
            db=conn
            if path=='cold':
                active.close()
                db=active.enter_context(closing(store.open_store(probe.directory/'gx.db')))
                roots=active.enter_context(connection(db))
                capture(roots,probe)
            assert roots.ensure_main()==thread
            before=accounting(db,run,budget)
            probe.peer.scenario('single_response')
            probe.peer.item=lambda row,index:probe.peer.message(row['id'],'4')
            next_turn=roots.human_turn('What is 2+2?',message_id='next')
            assert terminal(roots,next_turn)
            immediate=accounting(db,run,budget)
            clock.advance(2)
            roots.poll(.05)
            after=accounting(db,run,budget)
        model_calls=len(probe.peer.rows)
        replay=[]
        with closing(store.open_store(probe.directory/'gx.db')) as final_conn, connection(final_conn) as roots:
            capture(roots,probe)
            for _ in range(2):
                replay.append(roots.human_turn('What is 2+2?',message_id='next'))
                roots.poll(.05)
            repeated=accounting(final_conn,run,budget)
        observed=[{'turn':r.get('turn'),'count':r['tokenUsage']['total']['totalTokens']}
                  for r in probe.log if 'tokenUsage' in r]
        raw=[{'turn':r.get('turn'),'tokens':(r.get('usage') or {}).get('totalTokens')} for r in probe.log
             if r['method']=='rawResponse/completed']
        corrections=after['adjustments']
        checks={'first_response_reports_35_and_final_omits_usage':[r['tokens'] for r in raw if r['turn']==first]==[35,None],
                'terminal_missing_usage_initially_reserved':initial['turns'][0]['terminal_status']=='completed' and
                       initial['reserved'].get('tokens')==100 and not initial['spent'],
                'estimated_after_grace':estimate['turns'][0]['confidence']=='ESTIMATED' and
                       estimate['turns'][0]['observed_lower_bound']==35 and estimate['spent'].get('tokens')==100 and not estimate['reserved'],
                'followup_paid_once':next_turn!=first and model_calls==3,
                'later_cumulative_reconciles':len(corrections)==1 and corrections[0]['delta']==70-sum(t['settled_tokens'] for t in after['turns']) and
                       corrections[0]['reported_total']==70 and after['spent']=={'tokens':70,'api_calls':2},
                'original_estimate_and_uncertainty_auditable':after['turns'][0]['settled_tokens']==100 and
                       after['turns'][0]['estimated_at'] and after['turns'][0]['attribution']=='UNATTRIBUTED_THREAD_USAGE',
                'second_turn_charged_with_credible_attribution':(
                     after['turns'][1]['confidence']=='PROVIDER_CONFIRMED' and after['turns'][1]['settled_tokens']==35
                     if path=='warm' else after['turns'][1]['attribution']=='UNATTRIBUTED_THREAD_USAGE'),
                'duplicate_cold_delivery_has_no_extra_cost':replay==[next_turn,next_turn] and after==repeated and len(probe.peer.rows)==model_calls}
        return {'thread':thread,'run':run,'first_turn':first,'second_turn':next_turn,'initial':initial,'estimated':estimate,
                'path':path,'before_followup':before,'immediate_after_followup':immediate,'after_followup':after,'after_duplicate_cold_delivery':repeated,
                'replayed_ids':replay,'native_raw_receipts':raw,'official_cumulative_observations':observed,
                'model_requests':len(probe.peer.rows),'rollout_denials':denials,
                'rollout_mode':'All Python GX opens of official rollout paths denied throughout. Separate pinned native process retains its own store.',
                'grace_seconds':1,'gx_clock_advance_seconds':[2,2],'checks':checks}



def cold_repeated(probe):
    """Two estimated turns and a new, strictly growing aggregate from the same cold thread."""
    with prohibit_rollout_access(probe.home) as denials, project(probe,grant=100,grace=1) as (db,run,budget,env):
        def connection():
            return Roots(db,run_id=run,cwd=probe.cwd,env=env,token_budget=100,main_tools=[DYN_SPEC],
                         main_call=lambda name,args,envelope:{'result':4})
        def run_turn(roots, event_id):
            turn=roots.human_turn('Perform the arithmetic measurement.', message_id=event_id)
            assert wait(roots, lambda:any(r['method']=='turn/completed' and r.get('turn')==turn for r in probe.log))
            return turn
        with connection() as roots:
            capture(roots,probe)
            thread=roots.ensure_main()
            probe.peer.scenario('missing_last_usage')
            first=run_turn(roots,'first')
            clock.advance(2)
            roots.poll(.05)
        with connection() as roots:
            capture(roots,probe)
            assert roots.ensure_main()==thread
            probe.peer.scenario('missing_last_usage')
            second=run_turn(roots,'second')
            immediate=accounting(db,run,budget)
            clock.advance(2)
            roots.poll(.05)
            after_grace=accounting(db,run,budget)
            probe.peer.scenario('single_response')
            probe.peer.item=lambda row,index:probe.peer.message(row['id'],'4')
            third=run_turn(roots,'third')
            final=accounting(db,run,budget)
        with connection() as roots:
            capture(roots,probe)
            assert roots.human_turn('Perform the arithmetic measurement.',message_id='third')==third
            roots.poll(.05)
            replay=accounting(db,run,budget)
        second_counts=[r['tokenUsage']['total']['totalTokens'] for r in probe.log
                       if r.get('turn')==second and r['method']=='thread/tokenUsage/updated']
        raw=[r for r in probe.log if r.get('turn')==second and r['method']=='rawResponse/completed']
        checks={'cold_incomplete_turn_has_no_raw_and_repeated_native_count':not raw and second_counts==[70,70],
                'no_final_assertion_on_unchanged_tail':immediate['turns'][1]['budget_status']=='HELD' and not immediate['adjustments'],
                'two_provisional_charges_after_grace':after_grace['spent']=={'api_calls':2,'tokens':200} and
                     all(t['confidence']=='ESTIMATED' for t in after_grace['turns']) and not after_grace['adjustments'],
                'strictly_growing_later_cumulative_105':final['spent']=={'api_calls':3,'tokens':105} and not final['reserved'],
                'both_original_estimates_remain_auditable':all(t['estimated_at'] and t['settled_tokens']==100
                     and t['attribution']=='UNATTRIBUTED_THREAD_USAGE' for t in final['turns'][:2]),
                'adjustment_group_matches_one_cumulative_checkpoint':sum(a['delta'] for a in final['adjustments'])==-130
                     and {a['reported_total'] for a in final['adjustments']}=={105},
                'replay_does_not_recharge':replay==final and len(probe.peer.rows)==5}
        return {'thread':thread,'turns':[first,second,third],'second_turn_cumulative_counts':second_counts,
                'second_turn_raw_count':len(raw),'immediate_after_second':immediate,'after_second_grace':after_grace,
                'final':final,'after_replay':replay,'rollout_denials':denials,'model_requests':len(probe.peer.rows),
                'scope':'Two incomplete native turns followed by one complete response. Elapsed GX grace simulated.',
                'checks':checks}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--connection',choices=['warm','cold','cold-repeated'],required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    _proxy_preflight()
    binary=find_binary()
    with fixture(binary,probe_type=EventProbe) as probe:
        result=cold_repeated(probe) if args.connection=='cold-repeated' else measure(probe,args.connection)
    result['checks']['new_gx_store_attempted_zero_rollout_opens']=not result['rollout_denials']
    result.update(**binary_facts(binary),scope='official Codex 0.169.0 stdio with scripted loopback provider; real SQLite and GX Roots')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2)+'\n')
    print(result['checks'])
    return 0 if all(result['checks'].values()) else 1


if __name__=='__main__':
    raise SystemExit(main())
