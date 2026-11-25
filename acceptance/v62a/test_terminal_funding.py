"""A project can close before the module report author's final usage is known."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

if __package__:
    from .interleavings_fixture import Scenario, draft
else:
    from interleavings_fixture import Scenario, draft
from gx_core import budgets
from gx_core.p0 import metering, v6_native, workspace
from gx_core.p0.driver import Driver
from gx_core.persistence import store


def status(conn, ref):
    return conn.execute("SELECT status FROM budget_reservations WHERE reservation_ref=?", (ref,)).fetchone()[0]


class TerminalModuleFunding(unittest.TestCase):
    def test_late_final_turn_releases_terminal_module_and_run_once_after_cold_open(self):
        path = Path(self.enterContext(tempfile.TemporaryDirectory())) / "study"
        with Scenario(path) as s:
            source = s.materials[0]
            opened = s.open("report-after-read", inputs=[source])
            s.call(s.lead, v6_native.DECIDE, {"decision": draft()}, "wait-for-report")
            actor = s.bind(opened["task_id"], metered=True)
            s.call(actor, "gx_read_artifact", {"ref": source}, "read-source")
            report = s.report(actor, source)
            lead = s.next_actor("run")
            close = {"op": "close_run", "reason": "The inventory was delivered.",
                     "claim_refs": [], "unresolved": []}
            s.call(lead, v6_native.DECIDE, {"decision": draft(close, evidence=[report["report_ref"]])}, "close")
            s.advance()
            self.assertEqual("STOPPED", s.conn.execute("SELECT status FROM runs WHERE run_id=?", (s.run_id,)).fetchone()[0])
            child = s.conn.execute("SELECT budget_ref FROM v62_modules WHERE status='COMPLETE'").fetchone()[0]
            parent = budgets.state(s.conn, child)["parent_ref"]
            project = budgets.state(s.conn, parent)["parent_ref"]
            turn = s.conn.execute("SELECT reservation_ref FROM p0_root_deliveries WHERE thread_id=? AND turn_id=?",
                                  (actor.thread, actor.turn)).fetchone()[0]
            funds = [budgets.FUND_PREFIX + child, budgets.FUND_PREFIX + parent]
            workspace.release_finished(s.conn)
            self.assertEqual(["HELD"] * 3, [status(s.conn, ref) for ref in [turn, *funds]])

        with Scenario(path, reopen=True) as s:
            # Replay an acknowledged local native usage observation through the existing
            # official-usage reconciliation owner. No network or paid model is involved.
            with store.transaction(s.conn, transaction_owner="Acceptance", operation="deliver_usage", run_id=s.run_id):
                s.conn.execute("""INSERT INTO p0_root_turn_usage
                    (thread_id,turn_id,terminal_status,total_tokens,measured_tokens,children_known)
                    VALUES(?,?,'completed',7,7,1)""", (actor.thread, actor.turn))
                metering.reconcile_turn(s.conn, actor.thread, actor.turn)
            self.assertEqual("SETTLED", status(s.conn, turn))
            self.assertEqual("HELD", status(s.conn, funds[0]))

        # Cold-open twice, including a restart between accounting and project release.
        for _ in range(2):
            with Scenario(path, reopen=True) as s, patch.dict("os.environ", {"GX_CODEX_OFFLINE": "1"}):
                driver = Driver(s.conn, s.state, s.run_id)
                try:
                    driver.reconcile_costs()
                finally:
                    driver.close()
                workspace.release_finished(s.conn)
                for ref in funds:
                    self.assertEqual("SETTLED", status(s.conn, ref))
                    self.assertEqual(1, s.conn.execute("""SELECT count(*) FROM events WHERE kind='budget.released'
                        AND json_extract(payload_json,'$.reservation_ref')=?""", (ref,)).fetchone()[0])
                for ref in (child, parent, project):
                    spent = budgets.state(s.conn, ref)["spent"]
                    self.assertEqual((1, 7), (spent["api_calls"], spent["tokens"]))
                self.assertEqual(1, s.conn.execute("SELECT count(*) FROM v62_module_reports").fetchone()[0])
