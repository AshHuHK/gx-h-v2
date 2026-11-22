"""A real HTTP 429 during an acknowledged Codex worker turn and cold owner recovery."""
import argparse
import json
from pathlib import Path
import threading

from native_activity import capture, gate_outcome, project, task_ready
from native_budget import wait
from native_events import EventProbe
from acc1_protocol import binary_facts, fixture
from native_probe import find_binary, _proxy_preflight
from native_probe_peer import Handler, Peer
from gx_core.domain import clock
from gx_core.p0.roots import Roots


class RefusalHandler(Handler):
    def do_POST(self):
        if self.path!='/v1/responses':
            self.send_error(404)
            return
        body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        row,_=self.server.record(body)
        if self.server.refuse:
            payload=json.dumps({'error':{'code':'usage_limit_reached','type':'usage_limit_reached',
                                         'message':'Loopback quota window exhausted'}}).encode()
            status=429
            mime='application/json'
        else:
            self.server.admitted.set()
            self.server.release.wait(15)
            ident=row['id']
            data=[{'type':'response.created','response':{'id':ident}},
                  {'type':'response.output_item.done','item':self.server.message(ident,'Arithmetic complete.')},
                  {'type':'response.completed','response':{'id':ident,'usage':row['usage']}}]
            payload=''.join('data: '+json.dumps(i)+'\n\n' for i in data).encode()
            status=200
            mime='text/event-stream'
        try:
            self.send_response(status)
            self.send_header('Content-Type',mime)
            self.send_header('Content-Length',str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except (BrokenPipeError,ConnectionResetError):
            pass


class RefusalPeer(Peer):
    def __init__(self):
        super().__init__()
        self.RequestHandlerClass=RefusalHandler
        self.admitted=threading.Event()
        self.release=threading.Event()
        self.refuse=True


def measure(probe):
    with project(probe) as (db,run,budget,env):
        task=task_ready(db,run)
        def state():
            return dict(db.execute('SELECT task_id,status,attempt,attempts_consumed,lease_id FROM tasks WHERE task_id=?',(task,)).fetchone())
        def connection():
            return Roots(db,run_id=run,cwd=probe.cwd,env=env,token_budget=100)
        with connection() as roots:
            capture(roots,probe)
            work=roots.dispatch(task)
            assert wait(roots,lambda:any(e['method']=='turn/completed' and e.get('turn')==work['turn_id'] for e in probe.log))
            first=state()
            limits=[dict(row) for row in db.execute('SELECT provider,source,blocked_until FROM p0_provider_limits')]
            waits=[dict(row) for row in db.execute('SELECT reason,wait_key,ended_at FROM p0_attempt_waits')]
            terminal=next(e for e in probe.log if e['method']=='turn/completed' and e.get('turn')==work['turn_id'])
        with connection() as roots:
            capture(roots,probe)
            requests=len(probe.peer.rows)
            clock.advance(2)
            roots.poll(.03)
            denied=gate_outcome(lambda:roots.dispatch(task))
            after_denied=state()
            no_request=len(probe.peer.rows)==requests
            deadline=max([row['blocked_until'] or 0 for row in limits],default=0)
            clock.advance(max(2,deadline-clock.now().timestamp()+2))
            probe.peer.refuse=False
            try:
                resumed=gate_outcome(lambda:roots.dispatch(task))
                resumed_run=resumed.get('turn',{})
                if resumed_run:
                    assert wait(roots,lambda:probe.peer.admitted.is_set())
                after=state()
                failures=db.execute("SELECT count(*) FROM science_settlements WHERE task_id=? AND kind IN "
                                    "('agent_execution_failure','infrastructure_failure')",(task,)).fetchone()[0]
            finally:
                probe.peer.release.set()
        checks={'native_failed_turn_reported':terminal.get('status')=='failed' and bool(terminal.get('error')),
                'persisted_provider_refusal':any(l['source']=='turn/completed' and l['provider']==probe.provider for l in limits),
                'holds_existing_attempt':any(w['reason']=='provider_backoff' for w in waits) and not failures,
                'cold_retry_blocked_before_model':denied.get('details',{}).get('reason')=='provider_backoff' and no_request,
                'identical_lease_attempt_through_reset':all(row['attempt']==1 and row['lease_id']==work['lease_id'] for row in (first,after_denied,after)),
                'no_agent_failure_debit':first['attempts_consumed']==after_denied['attempts_consumed']==after['attempts_consumed'],
                'exactly_one_post_reset_request':len(probe.peer.rows)==requests+1 and resumed_run.get('thread_id')==work['thread_id'] and
                              resumed_run.get('turn_id')!=work['turn_id']}
        return {'run':run,'task':task,'first_native_turn':work['turn_id'],'terminal_error':terminal.get('error'),
                'terminal_status':terminal.get('status'),'provider_limits':limits,'waits':waits,'first_state':first,
                'blocked_retry':denied,'state_while_blocked':after_denied,'after_reset_state':after,
                'after_reset_turn':resumed_run.get('turn_id'),'after_reset_refusal':resumed if not resumed_run else None,'failure_settlements':failures,'requests':len(probe.peer.rows),
                'checks':checks}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    _proxy_preflight()
    binary=find_binary()
    with fixture(binary,peer_type=RefusalPeer,probe_type=EventProbe) as probe:
        result=measure(probe)
    result.update(**binary_facts(binary),scope='official Codex stdio and actual GX worker with HTTP 429 from loopback')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    args.out.write_text(json.dumps(result,indent=2)+'\n')
    print(result['checks'])
    raise SystemExit(0 if all(result['checks'].values()) else 1)
