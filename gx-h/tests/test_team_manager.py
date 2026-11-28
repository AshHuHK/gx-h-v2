"""Team control invariants on scratch SQLite stores; no native execution evidence.

The small rows made here are known unit cases. They are not acceptance records of
actual people, Host agents, scientific work or fresh computations.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from gx_core import contracts
from gx_core.context import Ctx, Principal
from gx_core.domain import clock
from gx_core.errors import (
    ArtifactHashMismatch, ContentHashMismatch, DependencyUnmet, IdempotencyConflict, IllegalTransition,
    LeaseExpired, PermissionDenied, SchemaInvalid, ScopeDenied, StaleRevision,
)
from gx_core.orchestration import seams
from gx_core.persistence import records, store
from gx_core.teams.manager import TeamManager, require_assignment


class ManagerFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="gx-team-manager-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "teams.db"
        self.conn = store.open_store(self.path)
        self.addCleanup(lambda: self.conn.close())
        self.addCleanup(clock.reset)
        self.policy, policy_sha = seams.agent_policy()
        self.run_id = records.create_run(
            self.conn, objective="Known control-plane case", project_ref="scratch", actor="unit",
            agent_policy=self.policy, pins={"contract_sha256": contracts.contract_digest()["__combined__"],
                                            "agent_policy_sha256": policy_sha},
            host={"kind": "other_verified"})
        self.coordinator = "coordinator"
        self.lease = store.acquire_coordinator_lease(
            self.conn, run_id=self.run_id, host="other_verified", owner=self.coordinator)["lease_id"]
        self.serial = 0

    def ctx(self, subject=None, **kwargs):
        return Ctx(self.conn, Principal(subject=subject or self.coordinator, **kwargs))

    def manager(self, subject=None, **kwargs):
        return TeamManager(self.ctx(subject, **kwargs))

    def arguments(self, **kwargs):
        self.serial += 1
        out = dict(run_id=self.run_id, expected_revision=store.current_revision(self.conn, self.run_id),
                   coordinator_lease_ref=self.lease, idempotency_key=f"case-{self.serial}")
        out.update(kwargs)
        return out

    def manage(self, action, payload, **kwargs):
        return self.manager().manage(action=action, payload=payload, **self.arguments(**kwargs))

    def team(self, objective="Known team", parent=None, *, active=False):
        payload = {"intent": {"objective": objective}}
        if parent is not None:
            payload["parent_team_id"] = parent
        team = self.manage("create", payload)["team"]
        if active:
            team = self.manage("transition", {"team_id": team["team_id"], "status": "ACTIVE"})["team"]
        return team

    def leader(self, team, subject="leader", scopes=("intent", "assign", "members", "message", "lifecycle"), task_ids=()):
        out = self.manage("leader", {"team_id": team["team_id"], "subject": subject, "lease_seconds": 3600})
        self.manage("grant", {"team_id": team["team_id"], "subject": subject,
                              "scopes": list(scopes), "task_ids": list(task_ids)})
        return out["team"]["leader_generation"]

    def task(self, *, run_id=None):
        # Unit-level task creation deliberately does not stand for product acceptance.
        return records.create_task(self.conn, run_id=run_id or self.run_id, kind="conclude", role="planner",
                                   actor="unit", required_artifacts=[], task_input={})

    def member(self, team, subject="worker"):
        return self.manage("member", {"team_id": team["team_id"], "subject": subject, "action": "add"})

    def assign(self, team, task, subject="worker"):
        return self.manage("assign", {"team_id": team["team_id"], "task_id": task, "subject": subject})

    def image(self):
        return "\n".join(self.conn.iterdump())

    def known_task_lease(self, task, subject):
        """A canonical task-machine lease for a declared unit recovery case."""
        with store.transaction(self.conn) as tx:
            store.transition(tx, machine="task", subject_id=task, to_state="READY", actor="unit", bump=False)
            lease = store.issue_lease(tx, task_id=task, owner=subject, worker_role="planner", principal=subject,
                                      seconds=3600, attempt=1)
            store.transition(tx, machine="task", subject_id=task, to_state="CLAIMED", actor="unit", bump=False,
                             extra_updates={"owner": subject, "lease_id": lease["lease_id"], "attempt": 1})
        return lease

    def known_verified_worker(self, subject="worker", ref="other_verified:unit-session:unit-worker"):
        # A declared known case for the transcript-bound identity branch. Not a Host receipt.
        task = self.task()
        lease = store.issue_lease(self.conn, task_id=task, owner=subject, worker_role="planner", principal=subject,
                                  seconds=3600, attempt=1)
        self.conn.execute(
            "INSERT INTO worker_observations(worker_observation_id,task_id,lease_id,host,observed_worker_ref,"
            "state,evidence_level,observation_source,observed_at) VALUES(?,?,?,?,?,?,?,?,?)",
            ("known-observation-" + task, task, lease["lease_id"], "other_verified", ref,
             "EXECUTION_OBSERVED", "EXECUTION_OBSERVED", "host_transcript", clock.now_iso()))
        store.release_lease(self.conn, lease["lease_id"])
        return ref


class TeamLifecycle(ManagerFixture):
    def test_create_replay_is_atomic_and_different_content_conflicts(self):
        args = self.arguments(action="create", payload={"intent": {"objective": "Known question"}})
        first = self.manager().manage(**args)
        before = self.image()
        again = self.manager().manage(**args)
        self.assertTrue(again["replayed"])
        self.assertEqual(first["team"]["team_id"], again["team"]["team_id"])
        self.assertEqual(before, self.image())
        args["payload"] = {"intent": {"objective": "Different question"}}
        with self.assertRaises(IdempotencyConflict):
            self.manager().manage(**args)
        self.assertEqual(before, self.image())

    def test_intents_are_versioned_and_preserved(self):
        team = self.team()
        changed = self.manage("revise", {"team_id": team["team_id"], "intent": {"objective": "Revised question"}})
        rows = list(self.conn.execute("SELECT * FROM team_intents WHERE team_id=? ORDER BY version", (team["team_id"],)))
        self.assertEqual([row["version"] for row in rows], [1, 2])
        self.assertEqual(json.loads(rows[0]["intent_json"])["objective"], "Known team")
        self.assertEqual(changed["team"]["intent_version"], 2)

    def test_real_coordinator_lease_cannot_be_borrowed(self):
        before = self.image()
        with self.assertRaises(LeaseExpired):
            self.manager("intruder").manage(action="create", payload={"intent": {"objective": "Spoof"}}, **self.arguments())
        self.assertEqual(before, self.image())

    def test_request_claims_cannot_grant_management(self):
        before = self.image()
        with self.assertRaises(ScopeDenied):
            self.manager("intruder").manage(action="create", payload={"intent": {"objective": "Spoof", "role": "coordinator"}},
                                             **self.arguments(coordinator_lease_ref=None))
        self.assertEqual(before, self.image())

    def test_readonly_principal_cannot_mutate_even_with_a_real_lease(self):
        with self.assertRaises(PermissionDenied):
            self.manager(scope="readonly").manage(action="create", payload={"intent": {"objective": "Spoof"}}, **self.arguments())

    def test_stale_revision_refusal_leaves_no_intent_or_idempotency_row(self):
        args = self.arguments(action="create", payload={"intent": {"objective": "Second"}})
        self.team()
        before = self.image()
        with self.assertRaises(StaleRevision):
            self.manager().manage(**args)
        self.assertEqual(before, self.image())

    def test_root_zero_depth_four_and_entire_subtree_reparent(self):
        root = self.team(active=True)
        chain = [root]
        for depth in range(1, 5):
            chain.append(self.team(parent=chain[-1]["team_id"], active=True))
            self.assertEqual(chain[-1]["logical_depth"], depth)
        before = self.image()
        with self.assertRaises(SchemaInvalid):
            self.team(parent=chain[-1]["team_id"])
        self.assertEqual(before, self.image())
        sibling = self.team(active=True)
        # A depth-0 tree of height four does not fit beneath any new parent.
        with self.assertRaises(SchemaInvalid):
            self.manage("reparent", {"team_id": root["team_id"], "parent_team_id": sibling["team_id"]})
        with self.assertRaises(SchemaInvalid):
            self.manage("reparent", {"team_id": root["team_id"], "parent_team_id": chain[-1]["team_id"]})

    def test_draining_stops_child_creation_and_close_requires_children(self):
        parent = self.team(active=True)
        child = self.team(parent=parent["team_id"], active=True)
        self.manage("transition", {"team_id": parent["team_id"], "status": "DRAINING"})
        with self.assertRaises(DependencyUnmet):
            self.team(parent=parent["team_id"])
        with self.assertRaises(DependencyUnmet):
            self.manage("transition", {"team_id": parent["team_id"], "status": "CLOSED"})
        self.manage("transition", {"team_id": child["team_id"], "status": "DRAINING"})
        self.manage("transition", {"team_id": child["team_id"], "status": "CLOSED"})
        closed = self.manage("transition", {"team_id": parent["team_id"], "status": "CLOSED"})
        self.assertEqual(closed["team"]["status"], "CLOSED")

    def test_replaced_leader_cannot_use_old_grant_or_generation(self):
        team = self.team(active=True)
        generation = self.leader(team, "old")
        self.leader(team, "new")
        before = self.image()
        args = self.arguments(coordinator_lease_ref=None, expected_generation=generation)
        with self.assertRaises(LeaseExpired):
            self.manager("old").manage(action="revise", payload={"team_id": team["team_id"], "intent": {"objective": "Spoof"}}, **args)
        self.assertEqual(before, self.image())

    def test_leader_needs_an_explicit_operation_grant(self):
        team = self.team(active=True)
        generation = self.leader(team, scopes=("message",))
        with self.assertRaises(ScopeDenied):
            self.manager("leader").manage(action="revise", payload={"team_id": team["team_id"], "intent": {"objective": "Spoof"}},
                                           **self.arguments(coordinator_lease_ref=None, expected_generation=generation))

    def test_old_contract_pin_blocks_all_fresh_team_mutations(self):
        team = self.team(active=True)
        self.conn.execute("UPDATE runs SET contract_sha256=? WHERE run_id=?", ("0" * 64, self.run_id))
        before = self.image()
        with self.assertRaises(ContentHashMismatch):
            self.manage("revise", {"team_id": team["team_id"], "intent": {"objective": "No silent repin"}})
        with self.assertRaises(ContentHashMismatch):
            self.manager().message(action="send", payload={"sender_team_id": team["team_id"], "recipient_team_id": team["team_id"],
                                                            "kind": "notice", "correlation_key": "old-pin"}, **self.arguments())
        self.assertEqual(before, self.image())


class AssignmentAndHandoff(ManagerFixture):
    def known_job(self, task, state="OUTCOME_UNKNOWN"):
        self.serial += 1
        job_id = f"known-job-{self.serial}"
        self.conn.execute(
            "INSERT INTO jobs(job_id,run_id,task_id,capability_id,capability_version,status,idempotency_key,"
            "submit_intent_json,input_manifest_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (job_id, self.run_id, task, "known-unit-capability", "1", state, job_id, "{}", "known-input", clock.now_iso()))
        return job_id

    def test_leader_cannot_steal_an_unassigned_mainline_task(self):
        team, task = self.team(active=True), self.task()
        generation = self.leader(team)
        self.member(team)
        before = self.image()
        with self.assertRaises(ScopeDenied):
            self.manager("leader").manage(action="assign", payload={"team_id": team["team_id"], "task_id": task, "subject": "worker"},
                                           **self.arguments(coordinator_lease_ref=None, expected_generation=generation))
        self.assertEqual(before, self.image())
        self.leader(team, task_ids=[task])
        generation = self.conn.execute("SELECT leader_generation FROM teams WHERE team_id=?", (team["team_id"],)).fetchone()[0]
        out = self.manager("leader").manage(action="assign", payload={"team_id": team["team_id"], "task_id": task, "subject": "worker"},
                                             **self.arguments(coordinator_lease_ref=None, expected_generation=generation))
        self.assertEqual(out["result"]["task_id"], task)

    def test_cross_run_task_and_context_cannot_be_assigned(self):
        team = self.team(active=True)
        self.member(team)
        another = records.create_run(self.conn, objective="Other case", project_ref="scratch", actor="unit")
        with self.assertRaises(ScopeDenied):
            self.assign(team, self.task(run_id=another))

    def test_claim_requires_assignee_membership_and_observed_identity(self):
        team, task = self.team(active=True), self.task()
        self.member(team)
        self.assign(team, task)
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task,)).fetchone()
        with store.transaction(self.conn):
            with self.assertRaises(ScopeDenied):
                require_assignment(self.ctx("other"), row)
            with self.assertRaises(ScopeDenied):
                require_assignment(self.ctx("worker"), row)
        ref = self.known_verified_worker()
        with store.transaction(self.conn):
            self.assertEqual(require_assignment(self.ctx("worker"), row)["verified_worker_ref"], ref)
        self.manage("member", {"team_id": team["team_id"], "subject": "worker", "action": "remove"})
        with store.transaction(self.conn):
            with self.assertRaises(ScopeDenied):
                require_assignment(self.ctx("worker"), row)

    def test_legacy_tasks_keep_their_old_admission_contract(self):
        task = self.task()
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task,)).fetchone()
        self.assertIsNone(require_assignment(self.ctx("worker"), row))

    def test_membership_leave_and_leader_takeover_do_not_revoke_worker_lease(self):
        team, task = self.team(active=True), self.task()
        self.member(team)
        self.assign(team, task)
        lease = self.known_task_lease(task, "worker")
        self.manage("member", {"team_id": team["team_id"], "subject": "worker", "action": "remove"})
        self.leader(team, "new")
        self.assertIsNone(self.conn.execute("SELECT released_at FROM leases WHERE lease_id=?", (lease["lease_id"],)).fetchone()[0])

    def test_pending_handoff_preserves_task_and_lease_and_blocks_both_closures(self):
        source, target, task = self.team(active=True), self.team(active=True), self.task()
        self.member(source, "old-worker")
        self.member(target, "new-worker")
        self.assign(source, task, "old-worker")
        lease = self.known_task_lease(task, "old-worker")
        payload = {"team_id": source["team_id"], "task_id": task, "recipient_team_id": target["team_id"], "subject": "new-worker"}
        pending = self.manage("handoff", payload)
        self.assertEqual(pending["result"]["status"], "PENDING_TRANSFER")
        self.assertEqual(self.conn.execute("SELECT team_id FROM task_assignments WHERE task_id=?", (task,)).fetchone()[0], source["team_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM tasks WHERE task_id=?", (task,)).fetchone()[0], 1)
        self.assertIsNone(self.conn.execute("SELECT released_at FROM leases WHERE lease_id=?", (lease["lease_id"],)).fetchone()[0])
        self.manage("transition", {"team_id": target["team_id"], "status": "DRAINING"})
        with self.assertRaises(DependencyUnmet) as caught:
            self.manage("transition", {"team_id": target["team_id"], "status": "CLOSED"})
        self.assertEqual(caught.exception.details["incoming_transfers"], 1)
        self.manage("handoff", {**payload, "cancel_pending": True})
        self.manage("transition", {"team_id": target["team_id"], "status": "CLOSED"})
        self.assertIsNone(self.conn.execute("SELECT released_at FROM leases WHERE lease_id=?", (lease["lease_id"],)).fetchone()[0])

    def test_handoff_applies_after_worker_settlement_without_changing_task_id(self):
        source, target, task = self.team(active=True), self.team(active=True), self.task()
        self.member(source, "old-worker")
        self.member(target, "new-worker")
        self.assign(source, task, "old-worker")
        lease = self.known_task_lease(task, "old-worker")
        payload = {"team_id": source["team_id"], "task_id": task, "recipient_team_id": target["team_id"], "subject": "new-worker"}
        self.manage("handoff", payload)
        store.release_lease(self.conn, lease["lease_id"])
        done = self.manage("handoff", payload)
        row = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task,)).fetchone()
        self.assertEqual((row["team_id"], row["subject"], row["assignment_generation"]), (target["team_id"], "new-worker", 2))
        self.assertEqual(done["result"]["task_id"], task)

    def test_unknown_job_keeps_handoff_pending_after_lease_expires(self):
        source, target, task = self.team(active=True), self.team(active=True), self.task()
        self.member(source, "old-worker")
        self.member(target, "new-worker")
        self.assign(source, task, "old-worker")
        lease = self.known_task_lease(task, "old-worker")
        self.known_job(task)
        store.release_lease(self.conn, lease["lease_id"])
        out = self.manage("handoff", {"team_id": source["team_id"], "task_id": task,
                                       "recipient_team_id": target["team_id"], "subject": "new-worker"})
        self.assertEqual(out["result"]["status"], "PENDING_TRANSFER")
        self.assertEqual(self.conn.execute("SELECT status FROM jobs WHERE task_id=?", (task,)).fetchone()[0], "OUTCOME_UNKNOWN")

    def test_team_close_does_not_release_an_unknown_job_or_held_budget(self):
        from gx_core import budgets
        budget = budgets.create_root(self.conn, run_id=self.run_id, profile_name="tiny", actor="known-unit-case")
        self.conn.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (budget, self.run_id))
        team, task = self.team(active=True), self.task()
        self.member(team)
        self.assign(team, task)
        self.known_job(task)
        held = budgets.reserve(self.conn, budget_ref=budget, subject_kind="task", subject_id=task,
                               amounts={"worker_tasks": 1}, actor="known-unit-case")
        store.transition(self.conn, machine="task", subject_id=task, to_state="CANCELLED", actor="known-unit-case", bump=False)
        self.manage("transition", {"team_id": team["team_id"], "status": "DRAINING"})
        before = budgets.state(self.conn, budget)
        with self.assertRaises(DependencyUnmet) as caught:
            self.manage("transition", {"team_id": team["team_id"], "status": "CLOSED"})
        self.assertEqual(caught.exception.details["jobs"], 1)
        self.assertEqual(caught.exception.details["reservations"], 1)
        self.assertEqual(budgets.state(self.conn, budget), before)
        self.assertEqual(self.conn.execute("SELECT status FROM budget_reservations WHERE reservation_ref=?", (held["reservation_ref"],)).fetchone()[0], "HELD")

    def test_assignment_guard_refuses_to_run_outside_the_claim_transaction(self):
        team, task = self.team(active=True), self.task()
        self.member(team)
        self.assign(team, task)
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task,)).fetchone()
        with self.assertRaises(SchemaInvalid):
            require_assignment(self.ctx("worker"), row)

    def test_paused_ancestor_withholds_new_assignment_claim(self):
        parent = self.team(active=True)
        child, task = self.team(parent=parent["team_id"], active=True), self.task()
        self.member(child)
        self.assign(child, task)
        self.manage("transition", {"team_id": parent["team_id"], "status": "PAUSED"})
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id=?", (task,)).fetchone()
        with store.transaction(self.conn):
            with self.assertRaises(DependencyUnmet):
                require_assignment(self.ctx("worker"), row)


class TeamMessagesAndReadModel(ManagerFixture):
    def send(self, source, target, **over):
        payload = {"sender_team_id": source["team_id"], "recipient_team_id": target["team_id"],
                   "kind": "challenge", "correlation_key": "known-correlation", "body": "Retain the counterexample"}
        payload.update(over)
        return self.manager().message(action="send", payload=payload, **self.arguments())

    def test_correlation_dedup_does_not_append_another_business_event(self):
        source, target = self.team(), self.team()
        first = self.send(source, target)
        events = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        revision = store.current_revision(self.conn, self.run_id)
        same = self.send(source, target)
        self.assertEqual(first["message"]["message_id"], same["message"]["message_id"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], events)
        self.assertEqual(store.current_revision(self.conn, self.run_id), revision)
        with self.assertRaises(IdempotencyConflict):
            self.send(source, target, body="Changed product")

    def test_message_payload_cannot_impersonate_another_sender(self):
        source, target = self.team(), self.team()
        with self.assertRaises(SchemaInvalid):
            self.send(source, target, sender_subject="someone-else")

    def test_only_the_recipient_may_ack_and_repeat_ack_has_no_business_effect(self):
        source, target = self.team(active=True), self.team(active=True)
        first = self.send(source, target)
        source_gen = self.leader(source, "sender")
        target_gen = self.leader(target, "recipient")
        payload = {"message_id": first["message"]["message_id"]}
        with self.assertRaises(LeaseExpired):
            self.manager("sender").message(action="ack", payload=payload,
                                            **self.arguments(coordinator_lease_ref=None, expected_generation=source_gen))
        self.manager("recipient").message(action="ack", payload=payload,
                                           **self.arguments(coordinator_lease_ref=None, expected_generation=target_gen))
        before = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        self.manager("recipient").message(action="ack", payload=payload,
                                           **self.arguments(coordinator_lease_ref=None, expected_generation=target_gen))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0], before)

    def test_scope_and_bounded_snapshot_do_not_change_store(self):
        source, target = self.team(), self.team()
        sent = self.send(source, target)
        before = self.image()
        out = self.manager().snapshot(run_id=self.run_id, limit=1)
        self.assertEqual(out["counts"]["teams"], 2)
        self.assertEqual(out["truncated"]["teams"], 1)
        self.assertEqual(out["messages"][0]["event_id"], sent["event_id"])
        self.assertEqual(before, self.image())
        self.assertEqual(self.manager().snapshot(run_id=self.run_id, after_event_id=sent["event_id"])["messages"], [])
        with self.assertRaises(ScopeDenied):
            self.manager("foreign", project_refs=("another",)).snapshot(run_id=self.run_id)
        self.assertEqual(before, self.image())

    def test_message_artifacts_are_rehashed_and_paths_are_not_references(self):
        from gx_core.artifacts.service import ArtifactService
        source, target = self.team(), self.team()
        service = ArtifactService(self.conn)
        artifact = service.publish_bytes(namespace=self.run_id, rel_path="unit-evidence.json", kind="known_case",
                                         data=b'{"declared_unit_case":true}', run_id=self.run_id)
        sent = self.send(source, target, artifact_refs=[artifact.artifact_ref])
        self.assertEqual(sent["message"]["payload"]["artifact_refs"], [artifact.artifact_ref])
        service.path_of(artifact.artifact_ref).write_bytes(b'{"changed_known_case":true}')
        before = self.image()
        with self.assertRaises(ArtifactHashMismatch):
            self.send(source, target, correlation_key="changed-artifact", artifact_refs=[artifact.artifact_ref])
        self.assertEqual(before, self.image())
        with self.assertRaises(SchemaInvalid):
            self.send(source, target, correlation_key="bad-reference", artifact_refs=["/tmp/a-path"])

    def test_acknowledgement_summaries_are_bounded_and_report_omissions(self):
        source, target = self.team(), self.team()
        sent = self.send(source, target)
        message_id = sent["message"]["message_id"]
        for index in range(20):
            # Known rows for the bounded read branch, not actual agent receipts.
            self.conn.execute("INSERT INTO team_message_acks VALUES(?,?,?,?)",
                              (message_id, str(index).zfill(3) + "x" * 500, target["team_id"], clock.now_iso()))
        view = self.manager().snapshot(run_id=self.run_id)["messages"][0]
        self.assertEqual(view["acknowledged_total"], 20)
        self.assertGreater(view["acknowledged_truncated"], 0)
        self.assertLess(len(json.dumps(view).encode()), 9000)


if __name__ == "__main__":
    unittest.main()
