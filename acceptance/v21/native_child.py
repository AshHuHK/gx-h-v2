"""Native child accounting with no GX access to rollout and an optional missing child receipt."""
import argparse
import json
from pathlib import Path

from native_activity import capture, project
from native_budget import wait
from native_cumulative import accounting, prohibit_rollout_access
from native_events import EventProbe
from acc1_child_late import measure as known_child_receipt
from acc1_protocol import MeterPeer, binary_facts, fixture
from native_probe import find_binary, _proxy_preflight
from gx_core.domain import clock
from gx_core.p0.roots import Roots


class MissingChildPeer(MeterPeer):
    def item(self,row,index):
        result=super().item(row,index)
        if row['child']:
            row['usage']=None
        return result


def missing_usage(probe):
    probe.peer.scenario('delayed_child')
    with project(probe,grant=100,grace=1) as (db,run,budget,env):
        with Roots(db,run_id=run,cwd=probe.cwd,env=env,token_budget=100) as roots:
            capture(roots,probe)
            root=roots.ensure_main()
            parent=roots.human_turn('Delegate one arithmetic check and complete.',message_id='primary')
            assert wait(roots,lambda:any(f['method']=='turn/completed' and f.get('turn')==parent for f in probe.log))
            parent_done=accounting(db,run,budget)
            # Keep the official child running until a normal poll can read its live session ancestry.
            assert wait(roots,lambda:db.execute("SELECT 1 FROM p0_native_child_meters WHERE root_id=? "
                                                "AND metadata_verified=1",(root,)).fetchone(),timeout=8)
            probe.peer.release_child.set()
            assert wait(roots,lambda:any(f['method']=='turn/completed' and f.get('thread')!=root for f in probe.log))
            clock.advance(2)
            roots.poll(.05)
            after=accounting(db,run,budget)
            children=[dict(c) for c in db.execute('SELECT * FROM p0_native_child_meters')]
            usage=[dict(c) for c in db.execute('SELECT * FROM p0_native_child_usage')]
            probe.peer.scenario('single_response')
            probe.peer.item=lambda row,n:probe.peer.message(row['id'],'4')
            follow=roots.human_turn('Return 2+2.',message_id='second')
            assert wait(roots,lambda:any(f['method']=='turn/completed' and f.get('turn')==follow for f in probe.log))
            final=accounting(db,run,budget)
        raw=[{'thread':r['thread'],'turn':r['turn'],'tokens':(r.get('usage') or {}).get('totalTokens')} for r in probe.log
             if r['method']=='rawResponse/completed']
        child=children[0]
        checks={'parent_observed_terminal_child_pending':parent_done['turns'][0]['terminal_status']=='completed'
                       and parent_done['reserved'].get('tokens')==100,
                'official_parent_child_connection':len(children)==1 and child['metadata_verified']==1 and child['root_id']==root
                       and usage[0]['owner_turn']==parent,
                'missing_child_not_assumed_free':len([r for r in raw if r['thread']==child['child_id'] and r['tokens'] is None])==1
                     and not any(r['thread']==child['child_id'] and r['tokens']==0 for r in raw)
                     and usage[0]['measured_tokens'] is None,
                'conservative_estimate_released_hold':after['turns'][0]['confidence']=='ESTIMATED' and
                     after['turns'][0]['settled_tokens']==100 and after['turns'][0]['observed_lower_bound']>=70
                     and after['spent']=={'api_calls':1,'tokens':100} and not after['reserved'],
                'no_duplicate_child_or_model_call':len(probe.peer.rows)==4 and follow!=parent,
                'unresolved_child_not_refunded':not final['adjustments'] and final['turns'][0]['confidence']=='ESTIMATED'}
        return {'run':run,'root':root,'child':child,'child_turn':usage,'parent_terminal':parent_done,'after_grace':after,
                'after_followup':final,'native_receipts':raw,'followup_turn':follow,'model_requests':len(probe.peer.rows),'checks':checks}


def rapid_child(probe):
    # This client receives the genuine spawn, child responses and terminal without ticking
    # the outer scheduler. After disconnect, the child can only be read as an unloaded thread.
    probe.peer.scenario('delayed_child')
    with project(probe,grant=100,grace=1) as (db,run,budget,env):
        def connect():
            return Roots(db,run_id=run,cwd=probe.cwd,env=env,token_budget=100)
        with connect() as roots:
            capture(roots,probe)
            root=roots.ensure_main()
            turn=roots.human_turn('Delegate one arithmetic check, then finish.',message_id='rapid')
            roots.client.run_until(lambda f:f.message.get('method')=='turn/completed'
                                   and f.message['params']['threadId']==root,timeout=10)
            probe.peer.release_child.set()
            roots.client.run_until(lambda f:f.message.get('method')=='turn/completed'
                                   and f.message['params']['threadId']!=root,timeout=10)
            child=db.execute('SELECT child_id FROM p0_native_child_meters WHERE root_id=?',(root,)).fetchone()[0]
        with connect() as roots:
            capture(roots,probe)
            roots.ensure_main()
            facts=roots._request('thread/read',{'threadId':child,'includeTurns':False})['thread']
            clock.advance(2)
            roots.poll(.05)
            snapshot=accounting(db,run,budget)
            row=dict(db.execute('SELECT * FROM p0_native_child_meters WHERE child_id=?',(child,)).fetchone())
            usage=dict(db.execute('SELECT * FROM p0_native_child_usage WHERE child_id=?',(child,)).fetchone())
        checks={'official_candidate_has_same_root_and_turn':row['root_id']==root and row['parent_id']==root and
                        row['owner_turn']==turn and usage['owner_turn']==turn,
                'cold_unloaded_child_identity_is_incomplete':facts['status']['type']=='notLoaded' and
                       facts['sessionId']==child and row['metadata_verified']==0,
                'missing_child_remains_unknown':usage['measured_tokens'] is None and not snapshot['adjustments'],
                'one_provisional_charge_and_no_new_calls':snapshot['spent']=={'api_calls':1,'tokens':100} and
                       snapshot['turns'][0]['confidence']=='ESTIMATED' and not snapshot['reserved'] and len(probe.peer.rows)==3}
        return {'run':run,'thread':root,'turn':turn,'stored_child':row,'stored_child_usage':usage,
                'official_child_fields':{k:facts.get(k) for k in ('id','parentThreadId','sessionId','status')},
                'snapshot':snapshot,'checks':checks}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case',choices=['complete','missing','rapid'],required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    _proxy_preflight()
    binary=find_binary()
    peer=MeterPeer if args.case=='complete' else MissingChildPeer
    with fixture(binary,peer_type=peer,probe_type=EventProbe) as probe, prohibit_rollout_access(probe.home) as denied:
        result={'complete':known_child_receipt,'missing':missing_usage,'rapid':rapid_child}[args.case](probe)
    result['checks']['new_gx_store_attempted_zero_rollout_opens']=not denied
    result.update(**binary_facts(binary),denied_rollout_access=denied,
                  scope='real native child and parent, official pinned binary, loopback provider, fresh database, GX rollout opens denied')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2)+'\n')
    print(result['checks'])
    raise SystemExit(0 if all(result['checks'].values()) else 1)
