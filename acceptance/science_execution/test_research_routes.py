"""Report publication, actual receipt, scientific adoption and native timing are independent."""
import json
from pathlib import Path
import tempfile
import unittest

from acceptance.v62a.interleavings_fixture import Scenario, draft
from gx_core.p0.research_routes import _history_turns, _interval, routes


class ResearchRoutes(unittest.TestCase):
    def test_real_gx_handoff_wait_and_sourced_publication(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = directory/'source.json'
        source.write_text('{"count":7}')
        grant = dict(research_effort='deep',plugins=[],capabilities=[],research_modules=['data_inventory@1'],
                     input_files=[dict(path=str(source),role='material')])
        s = self.enterContext(Scenario(directory/'work',spec_updates=grant))
        readback = lambda: routes(s.conn,s.run_id,s.state)
        self.assertEqual(readback()['modules'], [])
        raw = s.materials[0]
        m = s.open('first',inputs=[raw],caps=[])
        a = s.bind(m['task_id'])
        s.call(a,'gx_read_artifact',{'ref':raw},'original')
        s.call(s.lead,'gx_decide_revision',{'decision':draft()},'wait')
        pub = s.report(a,raw)
        ref = pub['report_ref']
        self.assertEqual([h['status'] for h in readback()['handoffs']], ['RELEASED'])
        s.next_actor('run')
        self.assertEqual([h['status'] for h in readback()['handoffs']], ['DELIVERED'])
        child = s.open('follow-up',inputs=[ref],caps=[],round=2)
        b = s.bind(child['task_id'])
        s.call(s.lead,'gx_decide_revision',{'decision':draft(evidence=[ref])},'cite-and-wait')
        h = next(x for x in readback()['handoffs'] if x['receiver_scope'] == 'run')
        self.assertEqual((h['status'],h['effects']), ('CITED',[]))
        # A frozen grant by itself delivers a handle. A completed native read supplies the report body.
        observed = s.call(b,'gx_read_artifact',{'ref':ref},'read-report')
        adopted = s.report(b,ref,text='The reported count supports retaining this scope.')
        prior = readback()
        self.assertEqual(next(h for h in prior['handoffs'] if h['receiver_scope'] == b.scope)['status'],'DELIVERED')
        profile = s.conn.execute('SELECT provider FROM p0_roots WHERE thread_id=?',(b.thread,)).fetchone()[0]
        file = s.state.parent/'codex/sessions'/f'rollout-test-{b.thread}.jsonl'
        file.parent.mkdir(parents=True)
        def event(kind, payload):
            return json.dumps(dict(type=kind,timestamp='2026-01-01T00:00:00Z',payload=payload))
        def completion(cid,tool,args,body):
            item = dict(type='DynamicToolCall',id=cid,tool=tool,arguments=args,success=True,
                        content_items=[dict(type='inputText',text=body)])
            return event('event_msg',dict(type='item_completed',item=item,turn_id=b.turn,thread_id=b.thread))
        original_read = s.conn.execute('SELECT call_id,result_json FROM p0_root_calls WHERE thread_id=? AND result_json LIKE ? ORDER BY rowid LIMIT 1',(b.thread,'%findings%')).fetchone()
        self.assertEqual(json.loads(original_read['result_json']),observed)
        file.write_text('\n'.join([event('session_meta',dict(id=b.thread,model_provider=profile)),
                event('event_msg',dict(type='task_started',turn_id=b.turn)),
                completion(original_read['call_id'],'gx_read_artifact',{'ref':ref},original_read['result_json']),
                completion(adopted['author']['call_id'],'gx_submit_module_report',{},json.dumps(adopted))]))
        proven = readback()
        h = next(x for x in proven['handoffs'] if x['receiver_scope'] == b.scope)
        self.assertEqual(h['status'],'ENACTED')
        self.assertEqual(h['effects'][0]['report_ref'],adopted['report_ref'])
        self.assertEqual(h['effects'][0]['findings'],['The reported count supports retaining this scope.'])
        self.assertFalse(h['real_model_handoff'])
        self.assertEqual(proven['real_turn_overlaps'],[])

    def test_native_boundaries_do_not_infer_queue_time_or_missing_end(self):
        directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        p = directory/'turn.jsonl'
        def event(kind, stamp, **payload):
            return json.dumps(dict(type=kind,timestamp=f'2026-01-01T00:00:{stamp}Z',payload=payload))
        lines = [event('session_meta','00',id='root',session_id='root',model_provider='openai'),
                 event('event_msg','04',type='task_started',turn_id='t'),event('turn_context','05',turn_id='t',model='m'),
                 event('response_item','06',type='message',role='assistant')]
        p.write_text('\n'.join(lines))
        root = dict(thread_id='root',provider='openai')
        waiting = _history_turns(p,root)['t']
        self.assertIsNone(waiting['end'])
        self.assertIsNone(_interval(waiting,waiting))
        lines.append(event('event_msg','10',type='task_complete',turn_id='t'))
        p.write_text('\n'.join(lines))
        seen = _history_turns(p,root)['t']
        self.assertEqual(_interval(seen, dict(start='2026-01-01T00:00:07Z',end='2026-01-01T00:00:11Z')),3)
        self.assertIsNone(_interval(seen, dict(start='2026-01-01T00:00:10Z',end='2026-01-01T00:00:20Z')))
        self.assertFalse(_history_turns(p,dict(thread_id='different',provider='openai')))
        self.assertFalse(_history_turns(p,dict(thread_id='root',provider='gx_loopback')))


if __name__ == '__main__':
    unittest.main()
