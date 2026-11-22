"""Controlled protocol concurrency against real GX entry points and disk SQLite."""
import json
import multiprocessing
import unittest
from contextlib import closing, contextmanager
from unittest.mock import patch

from gx_core.hostobs import native_observation
from gx_core.persistence import store
from tests import test_module_collaboration as modules


@contextmanager
def after_preparation(method, effect):
    original = getattr(native_observation, method)
    def prepare(*args, **kwargs):
        observed = original(*args, **kwargs)
        effect()
        return observed
    with patch.object(native_observation, method, prepare):
        yield


def racing_claim(fixture, args, thread, barrier, results, name):
    # Forked workers use fresh connections. Both take the same assignment snapshot
    # before either applicant can acquire SQLite's writer lock.
    with closing(store.connect(fixture.state.db_path)) as conn:
        fixture.conn = conn
        with after_preparation("prepare_claim", lambda: barrier.wait(timeout=10)):
            result = fixture.invoke("gx_task_claim", args, thread=thread)
        results.put((name, result["ok"], (result.get("error") or {}).get("code")))


class Fencing(unittest.TestCase):
    def setUp(self):
        self.f = modules.ModuleCollaboration()
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()

    def assignment(self):
        f = self.f
        action, cursor = f.ready("module_plan", "keys")
        team = f.module_teams["keys"]
        lead = f.leads["keys"]
        f.manage("assign", {"team_id": team, "task_id": action["task_id"], "subject": f.subject(lead)})
        return {"task_id": action["task_id"], "worker_role": action["role"],
                "expected_attempt": action["expected_attempt"], "as_of_event_id": cursor}, lead

    def message(self):
        f = self.f
        team = f.module_teams["prices"]
        args = {"run_id": f.run_id, "action": "send", "coordinator_lease_ref": f.started["coordinator_lease_ref"],
                "expected_revision": f.status()["revision"], "idempotency_key": f.key(),
                "payload": {"sender_team_id": team, "recipient_team_id": team, "kind": "notice",
                            "correlation_key": f.key(), "body": "Price audit continues"}}
        f.ok("gx_team_message", args)

    def control(self, action):
        f = self.f
        f.ok("gx_run_control", {"run_id": f.run_id, "action": action, "reason": "interleave before commit",
            "coordinator_lease_ref": f.started["coordinator_lease_ref"], "expected_revision": f.status()["revision"],
            "idempotency_key": f.key()})

    def test_claim_survives_unrelated_team_message(self):
        args, lead = self.assignment()
        with after_preparation("prepare_claim", self.message):
            claim = self.f.ok("gx_task_claim", args, thread=lead)
        self.assertEqual(claim["attempt"], 1)
        self.assertEqual(self.f.task(args["task_id"])["lease_id"], claim["lease_ref"])

    def test_submit_survives_unrelated_team_message(self):
        f = self.f
        work = f.begin("module_plan", "keys")
        with after_preparation("prepare", self.message):
            f.submit_plan(work, [("rows", "How many rows?")])
        self.assertEqual(f.task(work[1]["task"]["task_id"])["status"], "ACCEPTED")

    def test_cancel_before_claim_commit_refuses_old_callback(self):
        args, lead = self.assignment()
        with after_preparation("prepare_claim", lambda: self.control("cancel")):
            result = self.f.invoke("gx_task_claim", args, thread=lead)
        self.assertFalse(result["ok"], result)
        self.assertIn(result["error"]["code"], ("STALE_PLAN", "ILLEGAL_TRANSITION", "PHASE_DENIED"))
        self.assertEqual(self.f.task(args["task_id"])["attempt"], 0)
        self.assertEqual(self.f.task(args["task_id"])["status"], "CANCELLED")

    def test_pause_continue_cannot_revalidate_prepared_claim(self):
        args, lead = self.assignment()
        def pause_continue():
            self.control("pause")
            self.control("continue")
        with after_preparation("prepare_claim", pause_continue):
            result = self.f.invoke("gx_task_claim", args, thread=lead)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "STALE_PLAN")
        self.assertEqual(self.f.task(args["task_id"])["attempt"], 0)

    def test_leader_replacement_invalidates_prepared_claim(self):
        args, lead = self.assignment()
        f = self.f
        def replace():
            f.manage("leader", {"team_id": f.module_teams["keys"], "subject": f.subject(f.leads["prices"]),
                                "lease_seconds": 600})
        with after_preparation("prepare_claim", replace):
            result = f.invoke("gx_task_claim", args, thread=lead)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "STALE_PLAN")
        self.assertIsNone(f.task(args["task_id"])["lease_id"])

    def delegated_offer(self):
        f = self.f
        task_id = f.ready("module_plan", "keys")[0]["task_id"]
        team, lead = f.module_teams["keys"], f.leads["keys"]
        leader = f.manage("leader", {"team_id": team, "subject": f.subject(lead), "lease_seconds": 600})["team"]
        grant = {"team_id": team, "subject": f.subject(lead), "scopes": ["assign"], "task_ids": [task_id]}
        f.manage("grant", grant)
        f.ok("gx_team_manage", {"run_id": f.run_id, "action": "assign",
            "expected_revision": f.status()["revision"], "expected_generation": leader["leader_generation"],
            "idempotency_key": f.key(), "payload": {"team_id": team, "task_id": task_id,
                                                    "subject": f.subject(lead)}}, thread=lead)
        return task_id, lead, grant

    def test_revoked_delegated_task_cannot_be_acquired(self):
        f = self.f
        task_id, lead, grant = self.delegated_offer()
        f.manage("grant", {**grant, "task_ids": []})
        denied = f.invoke("gx_task_acquire", {"task_id": task_id}, thread=lead)
        self.assertFalse(denied["ok"], denied)
        self.assertEqual(denied["error"]["code"], "STALE_PLAN")
        self.assertIsNone(f.task(task_id)["lease_id"])

    def test_existing_worker_lease_survives_changes_to_leaders_future_delegations(self):
        f = self.f
        task_id, lead, grant = self.delegated_offer()
        first = f.ok("gx_task_acquire", {"task_id": task_id}, thread=lead)
        f.manage("grant", {**grant, "task_ids": []})
        replay = f.ok("gx_task_acquire", {"task_id": task_id}, thread=lead)
        self.assertTrue(replay["eligible"])
        self.assertEqual(replay["lease_ref"], first["lease_ref"])
        f.manage("handoff", {"team_id": grant["team_id"], "task_id": task_id,
                 "recipient_team_id": f.module_teams["prices"], "subject": f.subject(f.leads["prices"])})
        after_handoff = f.ok("gx_task_acquire", {"task_id": task_id}, thread=lead)
        self.assertFalse(after_handoff["eligible"])

    def test_handoff_invalidates_prepared_claim(self):
        args, lead = self.assignment()
        f = self.f
        def handoff():
            f.manage("handoff", {"team_id": f.module_teams["keys"], "task_id": args["task_id"],
                     "recipient_team_id": f.module_teams["prices"], "subject": f.subject(f.leads["prices"])})
        with after_preparation("prepare_claim", handoff):
            result = f.invoke("gx_task_claim", args, thread=lead)
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "STALE_PLAN")
        self.assertIsNone(f.task(args["task_id"])["lease_id"])

    def test_released_lease_cannot_submit(self):
        f = self.f
        lead, claim = f.begin("module_plan", "keys")
        task = claim["task"]["task_id"]
        ref = f.ok("gx_artifact_put", {"run_id": f.run_id, "task_id": task, "kind": "module_plan",
              "media_type": "application/json", "content": json.dumps({"items": []}),
              "idempotency_key": f.key()}, thread=lead)["artifact_ref"]
        args = f.submit_arguments(task, claim, ref)
        with after_preparation("prepare", lambda: store.release_lease(f.conn, claim["lease_ref"])):
            result = f.invoke("gx_task_submit", args, thread=lead)
        self.assertFalse(result["ok"], result)
        self.assertIn(result["error"]["code"], ("STALE_PLAN", "LEASE_EXPIRED"))
        self.assertEqual(f.conn.execute("SELECT COUNT(*) FROM submissions WHERE task_id=?", (task,)).fetchone()[0], 0)

    def test_team_cas_survives_unrelated_team_revision_and_fences_second_edit(self):
        f = self.f
        team = f.module_teams["keys"]
        rev = f.conn.execute("SELECT revision FROM teams WHERE team_id=?", (team,)).fetchone()[0]
        args = {"run_id": f.run_id, "action": "revise", "expected_revision": f.status()["revision"],
                "expected_team_revision": rev, "idempotency_key": f.key(),
                "coordinator_lease_ref": f.started["coordinator_lease_ref"],
                "payload": {"team_id": team, "intent": {"objective": "Recount source rows"}}}
        f.manage("revise", {"team_id": f.module_teams["prices"], "intent": {"objective": "Audit prices again"}})
        first = f.ok("gx_team_manage", args)
        self.assertEqual(first["team"]["revision"], rev + 1)
        second = f.invoke("gx_team_manage", dict(args, idempotency_key=f.key()))
        self.assertFalse(second["ok"], second)
        self.assertEqual(second["error"]["code"], "STALE_REVISION")
        self.assertEqual(second["error"]["details"]["team_id"], team)

    def test_two_processes_race_same_task_attempt(self):
        args, lead = self.assignment()
        ctx = multiprocessing.get_context("fork")
        barrier, results = ctx.Barrier(2), ctx.Queue()
        racers = [ctx.Process(target=racing_claim, args=(self.f, args, lead, barrier, results, name))
                  for name in ("first", "second")]
        try:
            for process in racers:
                process.start()
            for process in racers:
                process.join(15)
                self.assertEqual(process.exitcode, 0)
            replies = [results.get(timeout=1) for _ in racers]
            self.assertCountEqual([r[1] for r in replies], [True, False], replies)
            self.assertIn(next(r[2] for r in replies if not r[1]), ("STALE_PLAN", "LEASE_HELD"), replies)
            task = self.f.task(args["task_id"])
            self.assertEqual(task["attempt"], 1)
            live = list(self.f.conn.execute("SELECT lease_id FROM leases WHERE task_id=? AND released_at IS NULL",
                                            (args["task_id"],)))
            self.assertEqual([r["lease_id"] for r in live], [task["lease_id"]])
        finally:
            for process in racers:
                if process.is_alive():
                    process.terminate()
                    process.join()
            results.close()


class BaselineMigration(unittest.TestCase):
    def test_original_store_opens_with_epoch_and_keeps_existing_run_pins(self):
        import subprocess
        import tempfile
        from pathlib import Path
        from gx_core import contracts
        from gx_core.native_release import native_state
        from gx_core.persistence import records

        baseline = "23d581c27f46e65324246dff269b6ce77c9b3e9a"
        def historical(name):
            return subprocess.check_output(["git", "show", f"{baseline}:contracts/{name}"],
                                           cwd=contracts.HARNESS_ROOT, text=True)
        old = json.loads(historical("CONTRACT_SHA"))
        old_sql = historical("db_schema.sql")
        with tempfile.TemporaryDirectory(prefix="gx-p02-migration-") as tmp:
            root = Path(tmp) / "state"
            with native_state(root, env={}) as state:
                with closing(store.connect(state.db_path)) as conn:
                    conn.executescript(old_sql)
                    self.assertNotIn("control_epoch", [r["name"] for r in conn.execute("PRAGMA table_info(runs)")])
                    conn.executemany("INSERT INTO schema_meta(key,value) VALUES(?,?)",
                        [("contract_version", str(contracts.contract_version())),
                         ("db_schema_sha256", old["db_schema.sql"]), ("contract_sha256", old["__combined__"])])
                    run = records.create_run(conn, objective="Preserve earlier result", project_ref="migration",
                                             actor="fixture", pins={"contract_sha256": old["__combined__"]})
            with native_state(root, env={}) as state, closing(store.open_store(state.db_path)) as conn:
                self.assertEqual(store.read_meta(conn)["contract_sha256"], contracts.contract_digest()["__combined__"])
                self.assertEqual(tuple(conn.execute("SELECT control_epoch, objective, contract_sha256 FROM runs "
                                                    "WHERE run_id=?", (run,)).fetchone()),
                                 (0, "Preserve earlier result", old["__combined__"]))
                conn.execute("UPDATE runs SET status='PAUSED' WHERE run_id=?", (run,))
                self.assertEqual(conn.execute("SELECT control_epoch FROM runs WHERE run_id=?", (run,)).fetchone()[0], 1)
                self.assertEqual(store.ensure_schema(conn), "current")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM events WHERE kind='store.contract_resynced'").fetchone()[0], 1)
