"""Read-only checkout review; all synthetic ledgers live in SQLite :memory:."""
from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
import sys
from unittest.mock import patch

checkout = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path.cwd()
sys.path[:0] = [str(checkout / 'gx-h'), str(checkout / 'gx-h/tests')]
from gx_core import budgets
from gx_core.domain import clock
from gx_core.p0 import metering, meter_limits
from gx_core.p0.root_delivery import RootDelivery
from gx_core.persistence import records, store
from test_p0_meter_recovery import TurnMeterTests, Peer

@contextmanager
def ledger():
    f = TurnMeterTests()
    f.db = store.open_store(':memory:')
    try:
        f.run = records.create_run(f.db, objective='Synthetic arithmetic', project_ref='review', actor='probe')
        f.budget = budgets.create_root(f.db, run_id=f.run, profile_name='p0', limits={'tokens':1500,'api_calls':12}, actor='probe')
        f.db.execute('UPDATE runs SET budget_ref=? WHERE run_id=?', (f.budget, f.run))
        f.peer = Peer(f.db, f.run)
        f.delivery = RootDelivery(f.peer)
        yield f
    finally:
        f.db.close()
        clock.reset()

def leaf(f, name):
    return budgets.open_child(f.db, parent_ref=f.budget, subject_kind='team', subject_id=name, limits={'tokens':400,'api_calls':4}, actor='probe')['budget_ref']

def next_turn(f, fund=None):
    route = patch('gx_core.p0.team_admission.team_funding', return_value=fund) if fund else nullcontext()
    with route:
        event = 'turn-' + str(len(f.peer.turns) + 1)
        turn = f.delivery.deliver('root', event, event_id=event)['turn_id']
    f.delivery.observe('turn/started', {'threadId':'root','turn':{'id':turn}})
    return turn

def exact(f, rid, amount, total, fund=None):
    turn = next_turn(f, fund)
    f.raw(turn, rid, amount)
    f.sample(turn, total)
    f.complete()
    return turn

def expire(f):
    clock.advance(31)
    metering.maintain(f.db, f.run)

def snapshot(f, refs=()):
    d=metering.inspect(f.db, f.run)
    risk=meter_limits.run_risk(f.db, f.run)
    return {'turns':[(x['turn_id'],x['budget_status'],x['confidence'],x['settled_tokens'],x['observed_lower_bound']) for x in d['turns']],
            'adjustments':[(x['basis'],x['delta']) for x in d['adjustments']],
            'spent':[budgets.state(f.db, ref)['spent'] for ref in refs],
            'risk':{k:risk.get(k) for k in ('settled_tokens','observed_plus_settled','unfunded_closed_thread_tokens')},
            'unattributed':d['cost_report']['UNATTRIBUTED_DELTA']['tokens']}

def cross(f, separate):
    a=leaf(f,'A'); b=leaf(f,'B') if separate else a
    t=next_turn(f,a); f.raw(t,'late',None); f.complete(); expire(f)
    u=next_turn(f,b); f.sample(u,145); f.complete()
    before=snapshot(f,[a,b]); f.sample(t,120); f.raw(t,'late',120)
    return {'at_T2_terminal':before,'after_T1_exact':snapshot(f,[a,b])}

def other(f):
    exact(f,'original',20,20)
    g='unreserved-goal'
    f.delivery.observe('turn/started',{'threadId':'root','turn':{'id':g}})
    f.raw(g,'extra',200); f.sample(g,220)
    f.delivery.observe('turn/completed',{'threadId':'root','turn':{'id':g,'status':'completed'}})
    t=next_turn(f); f.raw(t,'no-usage',None); f.sample(t,225); f.complete(); expire(f)
    return snapshot(f,[f.budget])

def closed(f):
    a=leaf(f,'A'); exact(f,'a',20,20,a); f.sample('unreserved-goal',70)
    first=snapshot(f,[a]); budgets.release(f.db,budgets.FUND_PREFIX+a,actor='test',reason='close A')
    return {'before':first,'after':snapshot(f,[a])}

def duplicate(f):
    exact(f,'same',20,20); exact(f,'same',20,40)
    return snapshot(f,[f.budget])

def legacy(f):
    # Shape of a native settlement imported without a traceable detailed bill.
    # The original 100 tokens already exist in the budget's spent balance. There
    # is no original per-turn actual amount or matching budget.settled event.
    t=next_turn(f)
    ref=f.db.execute('SELECT reservation_ref FROM p0_meter_native_turns WHERE turn_id=?',(t,)).fetchone()[0]
    now=clock.now_iso()
    with store.transaction(f.db):
        f.db.execute("UPDATE budgets SET spent_json=?, reserved_json='{}' WHERE budget_ref=?",(json.dumps({'api_calls':1,'tokens':100}),f.budget))
        f.db.execute("UPDATE budget_reservations SET status='SETTLED', settled_at=? WHERE reservation_ref=?",(now,ref))
        f.db.execute("UPDATE p0_meter_turns SET terminal_at=?,confidence='HISTORICAL_SETTLED',attribution='HISTORICAL_TURN',settled_tokens=NULL WHERE reservation_ref=?",(now,ref))
        f.db.execute("INSERT INTO p0_root_turn_usage(thread_id,turn_id,terminal_status,total_tokens,children_known) VALUES('root',?,'completed',100,1)",(t,))
        metering.observe(f.db,f.run,'root','root',t,100)
    f.peer.turns[-1]['status']='completed'
    assert not f.db.execute("SELECT 1 FROM events WHERE kind='budget.settled' AND json_extract(payload_json,'$.reservation_ref')=?",(ref,)).fetchone()
    before=snapshot(f,[f.budget]); exact(f,'current',20,120)
    return {'before':before,'after_new_exact20':snapshot(f,[f.budget])}

def signed(f, final, obs, label):
    t=next_turn(f); f.raw(t,'late',None); f.complete(); expire(f)
    u=next_turn(f); f.raw(t,'late',final); f.sample(t,final); f.sample(u if label=='paid' else 'unreserved-goal',obs)
    return snapshot(f,[f.budget])

def historical_unreserved(f):
    legacy(f)
    goal='extra'
    f.delivery.observe('turn/started',{'threadId':'root','turn':{'id':goal}})
    f.raw(goal,'extra50',50)
    f.sample(goal,170)
    f.delivery.observe('turn/completed',{'threadId':'root','turn':{'id':goal,'status':'completed'}})
    closed=snapshot(f,[f.budget])
    t=next_turn(f)
    f.sample(t,175)
    return {'after_host_goal':closed,'with_open_turn':snapshot(f,[f.budget])}

def historical_unreserved_raw_only(f):
    legacy(f)
    goal='extra'
    f.delivery.observe('turn/started',{'threadId':'root','turn':{'id':goal}})
    f.raw(goal,'missing-final-cumulative',250)
    f.delivery.observe('turn/completed',{'threadId':'root','turn':{'id':goal,'status':'interrupted'}})
    closed=snapshot(f,[f.budget])
    next_turn(f)
    return {'after_host_goal':closed,'with_open_turn':snapshot(f,[f.budget])}

def signed_after_close(f, value):
    a=leaf(f,'A')
    b=leaf(f,'B')
    t=next_turn(f,a)
    f.raw(t,'first',None)
    f.complete()
    expire(f)
    budgets.release(f.db,budgets.FUND_PREFIX+a,actor='probe',reason='close A')
    u=next_turn(f,b)
    f.raw(t,'first',value)
    f.sample(t,value)
    f.sample(u,value+8)
    f.raw(u,'later',8)
    f.complete()
    return snapshot(f,[a,b,f.budget])

def historical_two_holds(f):
    t=next_turn(f)
    f.raw(t,'first-response',180)
    f.complete()
    newer=budgets.reserve(f.db,budget_ref=f.budget,subject_kind='native_turn',
                          subject_id='legacy-successor',amounts={'tokens':100,'api_calls':1},actor='probe')
    f.db.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,"
                 "human,interrupt,state,method,turn_id,reservation_ref,token_budget,prior_tokens) "
                 "VALUES(?,'legacy-successor','root','older','hash',0,0,'acknowledged','turn/start','later',?,100,NULL)",
                 (f.run,newer['reservation_ref']))
    f.delivery.observe('turn/started',{'threadId':'root','turn':{'id':'later'}})
    f.raw('later','second-response',160)
    risk=meter_limits.run_risk(f.db,f.run)
    return {k:risk[k] for k in ('reserved_tokens','observed_plus_settled','committed_tokens')}

if __name__=='__main__':
    for name,fn in [('cross_funds',lambda f:cross(f,True)),('same_fund',lambda f:cross(f,False)),('unreserved',other),('closed',closed),('duplicate',duplicate),('historical',legacy),('positive',lambda f:signed(f,140,150,'paid')),('negative',lambda f:signed(f,60,95,'unreserved')),('historical_unreserved',historical_unreserved),('positive_after_close',lambda f:signed_after_close(f,140)),('negative_after_close',lambda f:signed_after_close(f,60)),('historical_two_holds',historical_two_holds),('historical_unreserved_raw_only',historical_unreserved_raw_only)]:
        with ledger() as f:
            print(json.dumps({name:fn(f)},sort_keys=True))

