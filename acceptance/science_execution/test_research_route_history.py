"""A private rollout can be partly unreadable while its other turn records remain usable."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from gx_core.p0.research_routes import _history_turns


class RouteHistory(unittest.TestCase):
    def setUp(self):
        self.path = Path(self.enterContext(tempfile.TemporaryDirectory()))/'rollout.jsonl'
        self.root = dict(thread_id='thread', provider='openai')
        self.header = self.row('session_meta', id='thread', model_provider='openai')
        self.started = self.row('event_msg', type='task_started', turn_id='t')

    @staticmethod
    def row(kind, **payload):
        return json.dumps(dict(type=kind, payload=payload, timestamp='2026-01-01T00:00:00Z')).encode()

    def parse(self, *rows):
        self.path.write_bytes(b'\n'.join(rows))
        return _history_turns(self.path, self.root)

    def test_unusable_header_yields_no_attributed_turns(self):
        for header in (b'[]', b'{"type":"session_meta","payload":"unexpected"}', b'{', b'\xff'):
            with self.subTest(header=header):
                self.assertEqual(self.parse(header, self.started), {})

    def test_bad_records_preserve_subsequent_native_boundaries_and_report_read(self):
        text = '{"findings":[{"text":"received"}]}'
        item = dict(type='DynamicToolCall', id='read', tool='gx_read_artifact', success=True,
                    arguments={'ref':'report'}, content_items=[{'type':'inputText','text':text}])
        def completed(payload):
            return self.row('event_msg', type='item_completed', thread_id='thread', turn_id='t', item=payload)
        malformed = [b'[]', b'{"type":"event_msg","payload":"unexpected"}', b'{', b'\xff',
                     self.row('event_msg', type='task_started', turn_id=['bad']), completed('unexpected'),
                     completed(dict(item, id=['bad'])), completed(dict(item, content_items=[1])),
                     completed(dict(item, content_items=None)), completed(dict(item, arguments='bad')),
                     completed(dict(item, arguments={'ref':['bad']})),
                     self.row('response_item', type='function_call', call_id={'id':'bad'}),
                     self.row('token_usage_record', thread_id='thread', turn_id='t', response_id=['bad'])]
        seen = self.parse(self.header, self.started, *malformed)['t']
        self.assertFalse(seen['response'])
        self.assertIsNone(seen['end'])
        self.assertEqual(seen['report_reads'], {})
        rows = [self.header, self.started, *malformed, self.row('turn_context', turn_id='t', model='m'),
                self.row('token_usage_record', thread_id='thread', turn_id='t', response_id='real'),
                completed(item), self.row('event_msg', type='task_complete', turn_id='t'), b'{']
        verified = self.parse(*rows)['t']
        self.assertEqual((verified['response'], verified['model'], verified['end']), (True, 'm', '2026-01-01T00:00:00Z'))
        self.assertEqual(verified['report_reads'], {'report':('read', len(rows)-2, hashlib.sha256(text.encode()).hexdigest())})
