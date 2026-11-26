"""An observed native submit cancellation and its paired counts survive a lost Turn notification."""
import unittest

from gx_core.p0 import metering
from gx_core.p0.root_delivery import RootDelivery
from tests import test_p0_meter_recovery as fixtures


class MissedTerminalTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TurnMeterTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.turn = self.f.start()
        self.f.raw(self.turn, 'response1', 35)

    def cancel(self):
        self.f.delivery.observe('item/completed', {'threadId': 'root', 'turnId': self.turn,
            'item': {'type': 'dynamicToolCall', 'id': 'submit', 'tool': 'gx_submit_product',
                     'status': 'failed', 'success': False, 'contentItems': []}})
        self.f.peer.turns[0]['status'] = 'interrupted'

    def ticket(self):
        return metering.inspect(self.f.db, self.f.run)['turns'][0]

    def test_history_before_final_snapshot_waits_then_confirms_and_replay_does_not_rebill(self):
        self.cancel()
        self.f.delivery.reconcile('root')
        self.assertEqual(self.ticket()['budget_status'], 'HELD')
        self.f.sample(self.turn, 35)
        self.f.delivery.reconcile('root')
        self.f.delivery.observe('turn/completed', {'threadId': 'root', 'turn': self.f.peer.turns[0]})
        self.assertEqual((self.ticket()['confidence'], self.ticket()['settled_tokens']), ('PROVIDER_CONFIRMED', 35))
        self.assertEqual(self.f.db.execute("SELECT count(*) FROM events WHERE kind='budget.settled'").fetchone()[0], 1)

    def test_incomplete_later_response_or_extra_count_preserve_hold(self):
        self.f.sample(self.turn, 35)
        self.cancel()
        self.f.raw(self.turn, 'absent', None)
        self.f.sample(self.turn, 35)
        self.f.delivery.reconcile('root')
        self.assertEqual(self.ticket()['budget_status'], 'HELD')

    def test_unpaired_snapshot_cannot_turn_a_lower_bound_into_a_bill(self):
        self.f.sample(self.turn, 35)
        self.f.sample(self.turn, 35)
        self.cancel()
        self.f.delivery.reconcile('root')
        self.assertEqual(self.ticket()['budget_status'], 'HELD')

    def test_fresh_connection_without_live_coverage_cannot_reconstruct_the_last_request(self):
        self.f.sample(self.turn, 35)
        self.cancel()
        RootDelivery(self.f.peer).reconcile('root')
        self.assertEqual(self.ticket()['budget_status'], 'HELD')


class ColdTerminalProducts(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.TurnMeterTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.turn = self.f.start()
        self.db = self.f.db
        self.db.execute("INSERT INTO p0_roots(root_key,thread_id,run_id,cwd,role,model,effort,brief) "
                        "VALUES('root-key','root',?,'/root-key','reviewer','model','high','review')", (self.f.run,))
        self.db.execute("INSERT INTO p0_root_terminals(thread_id,turn_id,call_id,reason,recorded_at) "
                        "VALUES('root',?,'submit','GX_ACCEPTED','2026-01-01')", (self.turn,))
        self.db.execute("UPDATE runs SET inputs_json=json_set(inputs_json,'$.inputs.p0_spec.metering',"
                        "json_object('rollout_diagnostics',json('false'),'grace_seconds',0)) WHERE run_id=?", (self.f.run,))
        self.f.raw(self.turn, 'r1', 21)
        self.f.sample(self.turn, 21)
        self.f.delivery.observe('rawResponseItem/completed', {'threadId': 'root', 'turnId': self.turn,
            'item': {'type': 'function_call', 'namespace': 'gx', 'name': 'gx_submit_product', 'call_id': 'submit'}})
        self.cancelled = {'type': 'dynamicToolCall', 'id': 'submit', 'namespace': 'gx', 'tool': 'gx_submit_product',
                          'status': 'failed', 'success': False, 'contentItems': []}

    def history(self, *extra):
        return {'id': 'root', 'path': str(self.f.directory / 'must-not-open'), 'turns': [
            {'id': self.turn, 'status': 'interrupted', 'itemsView': 'full', 'items': [self.cancelled, *extra]}]}

    def fresh(self):
        self.cold = self.f.delivery = RootDelivery(self.f.peer)

    def ticket(self):
        return metering.inspect(self.db, self.f.run)['turns'][0]

    def test_cold_public_snapshot_of_committed_call_settles_once_without_private_files(self):
        from unittest.mock import patch
        from pathlib import Path
        self.f.raw(self.turn, 'r2', 14)
        self.f.sample(self.turn, 35)
        self.fresh()
        with patch.object(Path, 'open', side_effect=AssertionError('public-only recovery opened a rollout')):
            self.cold.attached('root', self.history())
            self.cold.attached('root', self.history())
        self.assertEqual((self.ticket()['confidence'], self.ticket()['settled_tokens']), ('PROVIDER_CONFIRMED', 35))
        self.assertEqual(self.db.execute("SELECT count(*) FROM events WHERE kind='budget.settled'").fetchone()[0], 1)

    def test_intermediate_cumulative_waits_for_disarmed_snapshot_then_reconciles_once(self):
        self.f.raw(self.turn, 'r2', 14)
        self.fresh()
        self.cold.attached('root', self.history())
        self.assertTrue(self.cold.needs_terminal_snapshot('root', self.turn))
        self.assertEqual(self.ticket()['budget_status'], 'HELD')
        metering.maintain(self.db, self.f.run)
        self.assertEqual(self.ticket()['confidence'], 'ESTIMATED')
        for _ in range(2):
            self.cold.attached('root', self.history())
            self.f.sample(self.turn, 35)
        self.assertEqual(self.ticket()['confidence'], 'PROVIDER_RECONCILED')
        self.assertEqual([r['delta'] for r in metering.inspect(self.db, self.f.run)['adjustments']], [-65])
        self.assertFalse(self.cold.needs_terminal_snapshot('root', self.turn))

    def test_terminal_call_without_response_is_unknown_even_if_it_repeats_last_snapshot(self):
        self.f.sample(self.turn, 21)
        self.fresh()
        self.cold.attached('root', self.history())
        self.assertFalse(metering.terminal_response_observed(self.db,'root',self.turn,'submit'))
        self.assertFalse(self.cold.needs_terminal_snapshot('root', self.turn))
        self.assertEqual(self.ticket()['budget_status'], 'HELD')

    def test_explicitly_missing_terminal_usage_ends_wait_without_proving_a_charge(self):
        self.f.raw(self.turn, 'r2', None)
        self.f.sample(self.turn, 21)
        self.fresh()
        self.cold.attached('root', self.history())
        self.assertTrue(metering.terminal_response_observed(self.db,'root',self.turn,'submit'))
        self.assertFalse(self.cold.needs_terminal_snapshot('root', self.turn))
        self.assertEqual(self.ticket()['budget_status'], 'HELD')

    def test_only_the_exact_direct_final_tool_response_can_end_the_wait(self):
        self.f.raw(self.turn, 'r2', 14)
        for name, namespace, expected in (
                ('gx_decide_revision', 'gx', True), ('gx_submit_module_report', 'gx', True),
                ('gx_task_context', 'gx', False), ('gx_decide_revision', 'other', False)):
            with self.subTest(name=name, namespace=namespace):
                self.db.execute("UPDATE p0_response_tools SET tool=?,namespace=? WHERE source='MODEL' AND call_id='submit'",
                                (name, namespace))
                self.assertEqual(metering.terminal_response_observed(self.db, 'root', self.turn, 'submit'), expected)
                self.assertFalse(metering.terminal_response_observed(self.db, 'root', self.turn, 'different'))

    def test_conflicting_id_or_wrong_native_cancel_cannot_close(self):
        self.f.raw(self.turn, 'r2', 14)
        self.f.sample(self.turn, 35)
        self.fresh()
        self.cancelled['id'] = 'different-call'
        self.cold.attached('root', self.history())
        self.assertEqual(self.ticket()['budget_status'], 'HELD')
        self.cancelled['id'] = 'submit'
        self.db.execute("UPDATE p0_meter_responses SET conflicting=1 WHERE response_id='r1'")
        self.cold.attached('root', self.history())
        self.assertEqual(self.ticket()['budget_status'], 'HELD')

    def test_exact_original_cannot_charge_before_child_or_charge_an_extra_unreserved_turn(self):
        self.f.raw(self.turn, 'r2', 14)
        self.f.sample(self.turn, 35)
        self.fresh()
        self.db.execute("INSERT INTO p0_native_child_meters(child_id,run_id,root_id,parent_id,owner_turn) "
                        "VALUES('child',?,'root','root',?)", (self.f.run, self.turn))
        self.db.execute("INSERT INTO p0_native_child_usage(child_id,turn_id,owner_turn,prior_tokens) "
                        "VALUES('child','work',?,0)", (self.turn,))
        self.cold.attached('root', self.history())
        self.assertEqual(self.ticket()['budget_status'], 'HELD')
        self.cold.observe('rawResponse/completed', {'threadId': 'root', 'turnId':'unadmitted', 'responseId':'extra',
                                                    'usage':{'totalTokens':7}})
        self.f.sample('unadmitted', 42)
        self.db.execute("UPDATE p0_native_child_meters SET metadata_verified=1,awaiting_turn=0 WHERE child_id='child'")
        self.db.execute("UPDATE p0_native_child_usage SET measured_tokens=9,total_tokens=9,children_known=1,terminal=1 "
                        "WHERE child_id='child' AND turn_id='work'")
        self.cold.attached('root', self.history())
        self.assertEqual((self.ticket()['confidence'], self.ticket()['settled_tokens']), ('PROVIDER_CONFIRMED', 44))
        unknown, = metering.inspect(self.db,self.f.run,include_responses=True)['unreserved_native_turns']
        self.assertEqual((unknown['turn_id'], unknown['reported_thread_total'], unknown['observed_response_lower_bound']),
                         ('unadmitted',42,7))


if __name__ == '__main__':
    unittest.main()
