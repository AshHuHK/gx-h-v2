"""GX integration with scripted App Server callbacks and real SQLite/artifact state."""
import json
import unittest
from unittest.mock import patch

from gx_core.teams.service import TeamService
from gx_core.hostobs.app_server_client import ReceivedMessage
from gx_core.hostobs.native_app_server import NativeRouter, dynamic_tools
from tests import test_module_collaboration as collaboration


class FacadeFixture(unittest.TestCase):
    def setUp(self):
        self.project = collaboration.ModuleCollaboration()
        self.addCleanup(self.project.doCleanups)
        self.project.setUp()
        self.conn = self.project.conn
        self.authority = self.project.authority
        self.router = NativeRouter(self.conn, project_ref=collaboration.PROJECT,
            max_turns=64, max_workers=8, codex_policy=self.project.policy,
            tool_policy={"tool_profile": "native_v2", "allow_mutations": True, "max_callbacks": 64})
        # The scripted peer has registered its Root, children and their active turns.
        self.router.authority = self.authority

    def assign(self, module="keys", actor=None, *, reviewer=False):
        offered, _ = self.project.ready("module_plan", module)
        task_id = offered["task_id"]
        team = self.project.module_teams[module]
        if reviewer:
            # An independent review assignment uses the same plan-product shape.
            other, _ = self.project.ready("module_plan", "prices")
            self.conn.execute("UPDATE tasks SET independent_of_task_id=? WHERE task_id=?", (other["task_id"], task_id))
        if actor is None:
            actor = self.project.leads[module]
        else:
            self.project.manage("member", {"team_id": team, "subject": self.project.subject(actor), "action": "add"})
        self.project.manage("assign", {"team_id": team, "task_id": task_id, "subject": self.project.subject(actor)})
        return task_id, actor

    def callback(self, actor, tool, arguments):
        params = {"tool": tool, "arguments": arguments, "threadId": actor,
                  "turnId": self.authority.active_turns[actor], "callId": self.project.key()}
        reply = self.router.on_server_request(ReceivedMessage(self.authority.sequence + 1,
            {"method": "item/tool/call", "params": params}))
        return json.loads(reply["contentItems"][0]["text"])

    def acquire(self, task_id, actor):
        response = self.callback(actor, "gx_task_acquire", {"task_id": task_id})
        self.assertTrue(response["ok"], response)
        return response["data"]

    def count(self, table, task_id):
        return self.conn.execute(f"SELECT count(*) FROM {table} WHERE task_id=?", (task_id,)).fetchone()[0]


class NativeAcquire(FacadeFixture):
    def test_one_callback_binds_and_new_call_recovers_the_live_lease(self):
        task_id, actor = self.assign()
        first = self.acquire(task_id, actor)
        self.assertEqual(self.router.calls, 1)
        self.assertEqual(first["attempt"], 1)
        self.assertTrue(first["brief_ref"])
        self.assertTrue(first["lease_ref"])
        # Simulate loss of the response. The next call has its own wire call ID.
        again = self.acquire(task_id, actor)
        self.assertEqual(again["lease_ref"], first["lease_ref"])
        self.assertEqual(again["attempt"], first["attempt"])
        self.assertEqual(self.count("leases", task_id), 1)
        claim, = self.conn.execute("SELECT payload_json FROM events WHERE kind='host.native_claim_verified'"
                                   " AND subject_id=?", (task_id,))
        proof = json.loads(claim[0])
        self.assertEqual(proof["native_call"]["thread_id"], actor)
        self.assertEqual(proof["native_call"]["tool_name"], "gx_task_acquire")
        self.assertEqual(proof["lease_id"], first["lease_ref"])

    def test_model_activity_requires_the_observed_lease_owner(self):
        task_id, actor = self.assign()
        self.acquire(task_id, actor)
        foreign = self.project.leads["prices"]
        denied = self.callback(foreign, "gx_model_activity", {"task_id": task_id})
        self.assertEqual(denied["error"]["code"], "SCOPE_DENIED", denied)
        missing_profile = self.callback(actor, "gx_model_activity", {"task_id": task_id})
        self.assertEqual(missing_profile["error"]["code"], "SCHEMA_INVALID", missing_profile)
        self.assertFalse(self.conn.execute("SELECT 1 FROM idempotency WHERE scope='research.model_activity'").fetchone())
        self.assertNotIn("gx_model_activity", {entry["name"] for entry in dynamic_tools(profile="native_core")})

    def test_old_catalog_and_forged_worker_fields(self):
        self.assertNotIn("gx_task_acquire", {entry["name"] for entry in dynamic_tools(profile="native_core")})
        task_id, assigned = self.assign()
        wrong = self.project.leads["prices"]
        for args in ({"task_id": task_id}, {"task_id": task_id, "thread_id": assigned}):
            with self.subTest(args=args):
                reply = self.callback(wrong, "gx_task_acquire", args)
                self.assertFalse(reply["ok"], reply)
                self.assertEqual(self.count("leases", task_id), 0)

    def test_host_operation_id_conflict_does_not_move_second_task(self):
        first_task, actor = self.assign()
        other_task, _ = self.assign("prices", actor)
        acquire = TeamService.acquire_task

        def duplicate_operation(service, **arguments):
            return acquire(service, operation_id="reused-test-operation", **arguments)

        # Inject a reused host operation ID after authentic callback admission.
        # Neither the worker nor the tool JSON can override this field.
        with patch.object(TeamService, "acquire_task", duplicate_operation):
            self.acquire(first_task, actor)
            response = self.callback(actor, "gx_task_acquire", {"task_id": other_task})
        self.assertEqual(response["error"]["code"], "IDEMPOTENCY_CONFLICT", response)
        self.assertEqual(self.count("leases", first_task), 1)
        self.assertEqual(self.count("leases", other_task), 0)

    def test_pause_and_resume_require_a_new_handoff_before_first_acquire(self):
        task_id, actor = self.assign()
        run = self.project.run_id
        old_status = self.conn.execute("SELECT status FROM runs WHERE run_id=?", (run,)).fetchone()[0]
        self.conn.execute("UPDATE runs SET status='PAUSED' WHERE run_id=?", (run,))
        self.conn.execute("UPDATE runs SET status=? WHERE run_id=?", (old_status, run))
        response = self.callback(actor, "gx_task_acquire", {"task_id": task_id})
        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error"]["code"], "STALE_PLAN", response)
        self.assertEqual(self.count("leases", task_id), 0)

    def test_changed_observed_model_cannot_restore_eligibility_on_replay(self):
        task_id, actor = self.assign()
        claim = self.acquire(task_id, actor)
        self.authority.apply_settings(actor, {"model": "fixture-unpinned-model", "effort": "low"},
                                      sequence=self.authority.sequence + 1)
        response = self.callback(actor, "gx_task_acquire", {"task_id": task_id})
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["data"]["lease_ref"], claim["lease_ref"])
        self.assertFalse(response["data"]["eligible"], response)
        self.assertEqual(self.count("leases", task_id), 1)

    def test_expired_lease_cannot_be_reactivated_by_another_acquire(self):
        task_id, actor = self.assign()
        claim = self.acquire(task_id, actor)
        self.conn.execute("UPDATE leases SET expires_at='2001-01-01T00:00:00Z' WHERE lease_id=?", (claim["lease_ref"],))
        reply = self.callback(actor, "gx_task_acquire", {"task_id": task_id})
        self.assertTrue(not reply["ok"] or reply["data"].get("eligible") is False, reply)
        self.assertEqual(self.count("leases", task_id), 1)
        self.assertEqual(self.project.task(task_id)["attempt"], 1)

    def test_review_requires_explicit_fresh_context(self):
        thread = "10000000-0000-4000-8000-000000000998"
        effort = self.project.policy["task_classes"]["PLANNING"]["default_effort"]
        self.authority.register_thread({"id": thread, "parentThreadId": collaboration.ROOT,
            "sessionId": collaboration.ROOT, "cliVersion": self.authority.host_version,
            "forkedFromId": collaboration.ROOT, "model": self.project.policy["pinned_model"]["id"],
            "reasoningEffort": effort}, sequence=self.authority.sequence + 1)
        self.authority.start_turn(thread, "review", sequence=self.authority.sequence + 1)
        task_id, actor = self.assign(actor=thread, reviewer=True)
        result = self.callback(actor, "gx_task_acquire", {"task_id": task_id})
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "INDEPENDENCE_REQUIRED", result)
        self.assertEqual(self.count("leases", task_id), 0)

    def test_explicit_fresh_reviewer_can_acquire(self):
        task_id, actor = self.assign(reviewer=True)
        self.assertEqual(self.acquire(task_id, actor)["attempt"], 1)


class NativeFinish(FacadeFixture):
    plan = {"items": [{"key": "rows", "question": "How many rows?", "role": "executor"}]}
    raw_data = b"A,1\nA,1\nB,\nC,2\n"

    def prepared_work(self):
        from gx_core.artifacts.delivery import prepare_manifest
        from gx_core.artifacts.service import ArtifactService

        task_id, actor = self.assign()
        claim = self.acquire(task_id, actor)
        artifacts = ArtifactService(self.conn)
        inbox = artifacts.inbox_dir(self.project.run_id)
        (inbox / "plan.json").write_text(json.dumps(self.plan))
        (inbox / "raw.txt").write_bytes(self.raw_data)
        manifest = prepare_manifest(artifacts, run_id=self.project.run_id, task_id=task_id,
            attempt=claim["attempt"], entries=[
                {"inbox_path": "plan.json", "kind": "module_plan", "media_type": "application/json"},
                {"inbox_path": "raw.txt", "kind": "raw_note", "media_type": "text/plain"}],
            provenance={"method_version": "test_csv_plan/1", "input_artifacts": [self.project.source]})
        return actor, {"task_id": task_id, "manifest_ref": manifest}, claim

    def recorded_events(self, task_id):
        return self.conn.execute("SELECT count(*) FROM outbox WHERE topic='submission.recorded'"
             " AND json_extract(payload_json,'$.task_id')=?", (task_id,)).fetchone()[0]

    def test_assigned_secondary_profile_requires_its_observed_output(self):
        actor, request, claim = self.prepared_work()
        # A commissioned task has a secondary profile but no provider completion yet.
        self.conn.execute("UPDATE tasks SET input_json=json_set(input_json,'$.output_kind','science_assessment')"
                          " WHERE task_id=?", (request["task_id"],))
        self.conn.execute("UPDATE runs SET inputs_json=json_set(inputs_json,"
                          "'$.inputs.execution_profiles.science_assessment',json('{}')) WHERE run_id=?",
                          (claim["run_id"],))
        denied = self.callback(actor, "gx_task_finish", request)
        self.assertEqual(denied["error"]["code"], "SCHEMA_INVALID", denied)
        self.assertIn("configured model product", denied["error"]["message"])
        self.assertEqual(self.count("submissions", request["task_id"]), 0)

    def test_recording_and_validation_recover_separately_after_ack_loss(self):
        from gx_core.artifacts.service import ArtifactService
        actor, request, claim = self.prepared_work()
        task_id = request["task_id"]
        first = self.callback(actor, "gx_task_finish", request)
        self.assertTrue(first["ok"], first)
        receipt = first["data"]
        self.assertEqual(receipt["delivery_status"], "recorded")
        self.assertEqual(receipt["structure_status"], "manifest_verified")
        self.assertEqual(receipt["scientific_status"], "pending_review")
        self.assertEqual(self.router.calls, 2)
        self.assertEqual(self.count("submissions", task_id), 1)
        self.assertEqual(self.recorded_events(task_id), 1)
        self.assertNotEqual(self.project.task(task_id)["status"], "ACCEPTED")
        self.project.reconnect()
        self.router.conn = self.conn = self.project.conn
        artifacts = ArtifactService(self.conn)
        self.assertCountEqual([artifacts.read_bytes(ref) for ref in receipt["artifact_refs"]],
                              [json.dumps(self.plan).encode(), self.raw_data])
        # Pending validation is an independent deterministic action after restart.
        TeamService(self.conn).validate_delivery(submission_id=receipt["submission_id"])
        self.assertEqual(self.project.task(task_id)["status"], "ACCEPTED")
        # A completed receipt is still readable after execution eligibility ended.
        self.conn.execute("UPDATE runs SET status='PAUSED' WHERE run_id=?", (self.project.run_id,))
        replay = self.callback(actor, "gx_task_finish", request)
        self.assertTrue(replay["ok"], replay)
        self.assertEqual(replay["data"]["submission_id"], receipt["submission_id"])
        self.assertTrue(replay["data"]["replayed"])
        self.assertEqual(self.count("submissions", task_id), 1)
        self.assertEqual(self.recorded_events(task_id), 1)
        self.assertEqual(self.count("leases", task_id), 1)

    def test_precommit_failure_unwinds_artifact_rows_submission_and_outbox(self):
        actor, request, _ = self.prepared_work()
        task = request["task_id"]
        before = self.count("artifacts", task)
        # The failure occurs after artifact and submission insertion, still inside
        # the finish transaction. Production recovery opens the same durable store.
        pending_rows = []
        def observe_transient_state():
            pending_rows.append((self.count("artifacts", task), self.count("submissions", task)))
            return 1
        self.conn.create_function("observe_transient_state", 0, observe_transient_state)
        self.conn.execute("CREATE TEMP TRIGGER fail_recording BEFORE INSERT ON outbox"
                          " WHEN NEW.topic='submission.recorded' BEGIN SELECT observe_transient_state();"
                          " SELECT RAISE(ABORT, 'fixture: rollback before commit'); END")
        failure = self.callback(actor, "gx_task_finish", request)
        self.assertFalse(failure["ok"], failure)
        self.assertEqual(pending_rows, [(before + 2, 1)])
        self.project.reconnect()
        self.router.conn = self.conn = self.project.conn
        self.assertEqual(self.count("artifacts", task), before)
        self.assertEqual(self.count("submissions", task), 0)
        self.assertEqual(self.recorded_events(task), 0)
        retry = self.callback(actor, "gx_task_finish", request)
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(self.count("submissions", task), 1)
        self.assertEqual(self.recorded_events(task), 1)
        self.assertEqual(self.count("artifacts", task), before + 2)

    def test_conflicting_manifest_request_cannot_overwrite_first_delivery(self):
        actor, request, _ = self.prepared_work()
        finish = TeamService.finish_task

        def duplicate_operation(service, **arguments):
            return finish(service, operation_id="reused-finish-operation", **arguments)

        with patch.object(TeamService, "finish_task", duplicate_operation):
            first = self.callback(actor, "gx_task_finish", request)
            self.assertTrue(first["ok"], first)
            conflict = self.callback(actor, "gx_task_finish", {**request, "summary": "different contents"})
        self.assertFalse(conflict["ok"], conflict)
        self.assertEqual(conflict["error"]["code"], "IDEMPOTENCY_CONFLICT", conflict)
        self.assertEqual(self.count("submissions", request["task_id"]), 1)

    def test_cancel_committed_after_manifest_read_fences_finish(self):
        from gx_core.artifacts import delivery
        from gx_core.persistence import store
        actor, request, _ = self.prepared_work()
        original = delivery.load_manifest

        def cancel_after_prepare(artifacts, *args, **kwargs):
            prepared = original(artifacts, *args, **kwargs)
            self.assertFalse(artifacts.conn.in_transaction)
            writer = store.connect(self.project.state.db_path, timeout=1)
            try:
                writer.execute("UPDATE runs SET status='CANCELLED' WHERE run_id=?", (self.project.run_id,))
            finally:
                writer.close()
            return prepared

        with patch.object(delivery, "load_manifest", cancel_after_prepare):
            refused = self.callback(actor, "gx_task_finish", request)
        self.assertFalse(refused["ok"], refused)
        self.assertIn(refused["error"]["code"], ("STALE_PLAN", "PHASE_DENIED"))
        self.assertEqual(self.count("submissions", request["task_id"]), 0)
        self.assertEqual(self.recorded_events(request["task_id"]), 0)

    def test_lapsed_owner_cannot_finish_prepared_originals(self):
        actor, request, claim = self.prepared_work()
        self.conn.execute("UPDATE leases SET expires_at='2001-01-01T00:00:00Z' WHERE lease_id=?", (claim["lease_ref"],))
        response = self.callback(actor, "gx_task_finish", request)
        self.assertFalse(response["ok"], response)
        self.assertEqual(response["error"]["code"], "LEASE_EXPIRED", response)
        self.assertEqual(self.count("submissions", request["task_id"]), 0)
        self.assertEqual(self.recorded_events(request["task_id"]), 0)
