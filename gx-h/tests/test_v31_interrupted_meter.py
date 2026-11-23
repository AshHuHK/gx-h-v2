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


if __name__ == '__main__':
    unittest.main()
