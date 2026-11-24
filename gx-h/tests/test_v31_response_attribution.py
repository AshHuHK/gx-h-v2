"""Provider response boundaries, code-mode joins, incomplete evidence and the existing budget."""
from contextlib import closing
import sqlite3
import unittest

from gx_core.p0 import response_attribution as attr
from gx_core.p0.root_delivery import RootDelivery
from tests import test_p0_root_delivery as fixtures


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RootDeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.conn = self.fixture.peer.conn
        self.delivery = self.fixture.delivery
        self.run = self.fixture.peer.run_id
        self.turn = self.delivery.deliver('root', 'inspect evidence', event_id='first')['turn_id']
        self.envelope = {'threadId': 'root', 'turnId': self.turn}
        self.delivery.observe('turn/started', {'threadId': 'root', 'turn': {'id': self.turn}})

    def item(self, kind, call, **fields):
        self.delivery.observe('rawResponseItem/completed', {**self.envelope, 'item': {
            'type': kind, 'call_id': call, **fields}})

    def complete(self, rid, usage=None):
        self.delivery.observe('rawResponse/completed', {**self.envelope, 'responseId': rid, 'usage': usage})

    def callback(self, call, name, result):
        self.delivery.attribution.outcome({**self.envelope, 'callId': call, 'tool': name}, result)

    def report(self):
        return attr.report(self.conn, self.run)

    def test_measured_raw_breakdown_missing_fields_replay_and_single_authoritative_bill(self):
        self.item('function_call', 'read', name='gx_read_artifact')
        self.callback('read', 'gx_read_artifact', 'SUCCESS')
        usage = {'inputTokens': 20, 'outputTokens': 5, 'cachedInputTokens': 9, 'reasoningOutputTokens': 2,
                 'totalTokens': 25}
        self.complete('one', usage)
        self.complete('one', usage)
        self.complete('two', {'inputTokens': 0, 'outputTokens': 1, 'cachedInputTokens': 0,
                              'reasoningOutputTokens': 0, 'totalTokens': 1})
        self.delivery.observe('thread/tokenUsage/updated', {**self.envelope, 'tokenUsage': {
            'total': {'totalTokens': 26}, 'last': {'inputTokens': 999}}})
        self.delivery.observe('turn/completed', {'threadId': 'root', 'turn': {'id': self.turn, 'status': 'completed'}})
        report = self.report()
        self.assertEqual(report['response_count'], 2)
        first, second = report['responses']
        self.assertEqual(tuple(first[k] for k in attr.TOKEN_FIELDS), (20, 9, 5, 2, 25))
        self.assertEqual(tuple(second[k] for k in attr.TOKEN_FIELDS), (0, None, 1, None, 1))
        self.assertEqual([r['request_ordinal'] for r in (first, second)], [1, 2])
        self.assertEqual((first['request_class'], second['request_class']), ('READ', 'UNCLASSIFIED'))
        self.assertIsNone(first['provider'])
        self.assertIsNone(first['model'])
        self.assertEqual(report['first_request_input_tokens'][0]['input_tokens'], 20)
        self.assertEqual(self.fixture.budget()['spent'], {'api_calls': 1, 'tokens': 26})
        # A read-only report has no budget side effect, even when reopened.
        with closing(sqlite3.connect(self.conn.execute('PRAGMA database_list').fetchone()['file'])) as reopened:
            reopened.row_factory = sqlite3.Row
            self.assertEqual(attr.report(reopened, self.run), report)
        self.assertEqual(self.fixture.budget()['spent'], {'api_calls': 1, 'tokens': 26})

    def test_nested_tools_before_and_after_marker_mixed_repair_and_terminal(self):
        self.item('custom_tool_call', 'outer', name='exec')
        self.callback('exec-1', 'gx_read_artifact', 'SUCCESS')
        self.complete('r1')
        self.callback('exec-2', 'gx_submit_product', 'SCHEMA_INVALID')
        self.callback('exec-2', 'gx_submit_product', 'SCHEMA_INVALID')
        self.item('custom_tool_call_output', 'outer', output=[{'type': 'input_text', 'text': 'Script completed\n'}])
        self.item('function_call', 'direct', name='gx_submit_product')
        self.complete('r2', {'inputTokens': -1, 'outputTokens': True})
        self.callback('direct', 'gx_submit_product', 'TERMINAL_REJECTED')
        self.complete('r3')
        rows = self.report()['responses']
        self.assertEqual([r['request_class'] for r in rows], ['MIXED', 'REPAIR', 'POST_SUBMIT'])
        self.assertTrue(all(r[k] is None for r in rows for k in attr.TOKEN_FIELDS))
        self.assertEqual({(t['source'], t['outcome']) for t in rows[0]['tool_calls']},
                         {('GX', 'SUCCESS'), ('GX', 'SCHEMA_INVALID'), ('MODEL', 'Script completed')})
        self.assertEqual(rows[1]['tool_calls'][-1]['link'], 'call_id')
        self.assertFalse(self.report()['unlinked_tools'])

    def test_yield_and_simultaneous_code_cells_never_assign_an_ambiguous_callback(self):
        self.item('custom_tool_call', 'outer1', name='exec')
        self.item('custom_tool_call', 'outer2', name='exec')
        self.complete('a')
        self.callback('exec-ambiguous', 'gx_submit_product', 'ERROR')
        self.item('custom_tool_call_output', 'outer1', output='Script completed\n')
        self.item('custom_tool_call_output', 'outer2', output='Script running with cell ID 2\n')
        self.item('custom_tool_call', 'outer3', name='exec')
        self.callback('exec-yielded', 'gx_read_artifact', 'SUCCESS')
        self.complete('b')
        self.assertEqual([t['call_id'] for t in self.report()['unlinked_tools']], ['exec-ambiguous', 'exec-yielded'])
        self.assertEqual([r['request_class'] for r in self.report()['responses']], ['UNCLASSIFIED', 'UNCLASSIFIED'])

    def test_cold_attach_unknown_ordinal_missing_usage_and_conflicting_replay(self):
        self.complete('a', {'totalTokens': 5, 'inputTokens': 4})
        self.complete('a', {'totalTokens': 5, 'inputTokens': 3})
        self.delivery = RootDelivery(self.fixture.peer)
        self.complete('b')
        self.delivery.observe('thread/tokenUsage/updated', {**self.envelope, 'tokenUsage': {
            'total': {'totalTokens': 100}, 'last': {'inputTokens': 95, 'totalTokens': 95}}})
        r = self.report()
        self.assertEqual(len(r['responses']), 2)
        self.assertTrue(r['responses'][0]['conflicting'])
        self.assertIsNone(r['responses'][1]['request_ordinal'])
        self.assertIsNone(r['responses'][1]['input_tokens'])
        self.assertIsNone(r['first_request_input_tokens'][0]['input_tokens'])
        self.assertEqual(r['by_class']['UNCLASSIFIED']['usage']['input_tokens'],
                         {'observed_count': 0, 'observed_sum': None})

    def test_namespaced_direct_submission_requires_the_exact_terminal_tool(self):
        self.item('function_call', 'bad', namespace='gx', name='gx_read_artifact')
        self.complete('r0')
        self.item('function_call', 'submit', namespace='gx', name='gx_submit_product')
        self.complete('r1')
        self.delivery.attribution.outcome({**self.envelope, 'namespace': 'gx', 'tool': 'gx_submit_product',
                                          'callId': 'submit'}, 'ACCEPTED')
        self.complete('r2')
        self.assertEqual([r['request_class'] for r in self.report()['responses']],
                         ['UNCLASSIFIED', 'PRODUCE', 'POST_SUBMIT'])

    def test_native_child_uses_causal_root_and_its_own_stream(self):
        self.conn.execute("INSERT INTO p0_native_child_meters(child_id,run_id,root_id,parent_id,owner_turn,metadata_verified) "
                          "VALUES('child',?,'root','root',?,1)", (self.run, self.turn))
        facts = {'threadId': 'child', 'turnId': 'child-turn'}
        self.delivery.observe_descendant('turn/started', {'threadId': 'child', 'turn': {
            'id': 'child-turn', 'rootTurnId': self.turn}})
        self.delivery.observe_descendant('rawResponseItem/completed', {**facts, 'item': {
            'type': 'function_call', 'namespace': 'collaboration', 'call_id': 's', 'name': 'send_message'}})
        self.delivery.observe_descendant('rawResponse/completed', {**facts, 'responseId': 'child-r',
                                        'usage': {'inputTokens': 4, 'outputTokens': 2, 'totalTokens': 6}})
        self.delivery.observe_descendant('item/completed', {**facts, 'item': {
            'type': 'collabAgentToolCall', 'id': 's', 'status': 'completed'}})
        self.complete('parent-r', {'inputTokens': 7})
        child, parent = self.report()['responses']
        self.assertEqual((child['run_id'], child['root_id'], child['thread_id'], child['turn_id'],
                          child['ownership_verified'], child['request_ordinal'], child['request_class']),
                         (self.run, 'root', 'child', 'child-turn', 1, 1, 'OTHER_CONTROL'))
        self.assertIsNone(child['requested_model'])
        self.assertEqual(child['tool_calls'][0]['outcome'], 'completed')
        self.assertEqual((parent['thread_id'], parent['request_ordinal']), ('root', 1))


if __name__ == '__main__':
    unittest.main()
