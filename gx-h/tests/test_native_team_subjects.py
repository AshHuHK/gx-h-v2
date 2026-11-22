"""Native team membership uses observed worker identities across an adapter restart."""

from gx_core.hostobs import native_context, native_spawn
from tests.test_native_facades import FacadeFixture
from tests.test_module_collaboration import PROJECT, ROOT

PATH = "/root/grid_reviewer"


class NativeTeamSubjects(FacadeFixture):
    def setUp(self):
        super().setUp()
        self.team = self.project.module_teams["keys"]
        self.task_id = self.project.ready("module_plan", "keys")[0]["task_id"]
        self.actor = self.project.actor(ROOT, "PLANNING")
        self.subject = self.project.subject(self.actor)

    def observe(self, path, thread):
        authority = self.project.authority
        return native_spawn.observe(
            self.conn,
            project_ref=PROJECT,
            session_id=ROOT,
            parent_thread_id=ROOT,
            turn_id=authority.active_turns[ROOT],
            phase="completed",
            item={
                "type": "subAgentActivity",
                "kind": "started",
                "id": self.project.key(),
                "agentPath": path,
                "agentThreadId": thread,
            },
        )

    def arguments(self, operation, **payload):
        return {
            "run_id": self.project.run_id,
            "coordinator_lease_ref": self.project.started["coordinator_lease_ref"],
            "action": operation,
            "payload": {"team_id": self.team, **payload},
            "expected_revision": self.project.status()["revision"],
            "idempotency_key": self.project.key(),
        }

    def manage(self, operation, **payload):
        return self.project.ok("gx_team_manage", self.arguments(operation, **payload))

    def assignment(self):
        return self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (self.task_id,)).fetchone()

    def member(self, subject):
        return self.conn.execute(
            "SELECT * FROM team_members WHERE subject=? AND team_id=?", (subject, self.team)
        ).fetchone()

    def test_observed_path_replays_original_request_and_child_can_acquire(self):
        self.observe(PATH, self.actor)
        add = self.arguments("member", **{"action": "add", "subject": PATH})
        reply = self.callback(ROOT, "gx_team_manage", add)
        self.assertTrue(reply["ok"], reply)
        worker, = (w for w in reply["native_context"]["registered_workers"] if PATH in w["agent_paths"])
        self.assertEqual(worker["backend_subject"], self.subject)
        self.assertIsNone(self.member(PATH))
        self.assertEqual(self.member(self.subject)["status"], "ACTIVE")
        self.project.ok("gx_team_manage", add)
        assignment = self.arguments("assign", subject=PATH, task_id=self.task_id)
        self.project.ok("gx_team_manage", assignment)
        generation = self.assignment()["assignment_generation"]
        self.assertEqual(self.assignment()["subject"], self.subject)
        self.assertTrue(self.acquire(self.task_id, self.actor)["lease_ref"])
        self.project.ok("gx_team_manage", assignment)
        self.assertEqual(self.assignment()["assignment_generation"], generation)

    def test_reopened_store_uses_spawn_receipt_from_the_same_session(self):
        self.observe(PATH, self.actor)
        self.project.authority.close()
        self.project.reconnect()
        self.conn = self.router.conn = self.project.conn
        fresh = native_context.NativeAuthority(project_ref=PROJECT, root_subject="test-coordinator")
        self.addCleanup(fresh.close)
        self.project.authority = self.authority = self.router.authority = fresh
        self.project.register_thread(
            ROOT, parent=None, effort=self.project.policy["native_coordinator_observation"]["default_effort"]
        )
        self.manage("member", **{"action": "add", "subject": PATH})
        self.manage("assign", task_id=self.task_id, subject=PATH)
        self.assertEqual(self.assignment()["subject"], self.subject)
        self.project.register_thread(
            self.actor, parent=ROOT, effort=self.project.policy["task_classes"]["PLANNING"]["default_effort"]
        )
        self.assertTrue(self.acquire(self.task_id, self.actor)["lease_ref"])

    def test_unknown_and_ambiguous_paths_cannot_create_membership(self):
        other = self.project.actor(ROOT, "PLANNING")
        self.observe(PATH, self.actor)
        self.observe(PATH, other)
        for path in (PATH, "/root/absent"):
            with self.subTest(path=path):
                before = self.project.status()["revision"]
                args = self.arguments("member", **{"action": "add", "subject": path})
                result = self.project.invoke("gx_team_manage", args)
                self.assertFalse(result["ok"], result)
                self.assertIn("registered_workers", result["error"]["details"])
                self.assertIsNone(self.member(path))
                self.assertIsNone(self.member(self.subject))
                self.assertIsNone(self.member(self.project.subject(other)))
                self.assertEqual(self.project.status()["revision"], before)

    def test_old_spawn_receipt_recovers_only_matching_thread_metadata(self):
        self.manage("member", **{"action": "add", "subject": self.subject})
        self.manage("assign", subject=self.subject, task_id=self.task_id)
        self.conn.execute("UPDATE task_assignments SET subject=? WHERE task_id=?", (PATH, self.task_id))
        # An older persisted spawn receipt recorded the UUID before this version
        # retained the optional path from the same collaboration item.
        self.observe(None, self.actor)
        source = {"subAgent": {"thread_spawn": {"agent_path": PATH, "parent_thread_id": ROOT}}}
        thread = {"id":self.actor, "sessionId":ROOT, "parentThreadId":ROOT,
                  "cliVersion":self.authority.host_version, "source":source}
        def read(worker, session):
            return {"thread": {**thread, "id":worker, "sessionId":session,
                               "source":source if worker==self.actor else {}}}

        native_spawn.recover_legacy_paths(self.conn, self.authority, lambda w: read(w, "other-session"))
        self.assertFalse(any(PATH in w["agent_paths"] for w in native_spawn.worker_directory(self.conn,self.authority)))
        native_spawn.recover_legacy_paths(self.conn, self.authority, lambda w: read(w, ROOT))
        self.manage("handoff", subject=PATH, task_id=self.task_id, recipient_team_id=self.team)
        self.assertEqual(self.assignment()["subject"], self.subject)

    def test_existing_unclaimed_path_assignment_can_be_handed_to_real_worker(self):
        # Recreate the old persisted subject shape before the native-domain resolution fix.
        self.manage("member", **{"action": "add", "subject": self.subject})
        self.manage("assign", subject=self.subject, task_id=self.task_id)
        previous = self.assignment()["assignment_generation"]
        self.conn.execute(
            "UPDATE team_members SET subject=? WHERE team_id=? AND subject=?", (PATH, self.team, self.subject)
        )
        self.conn.execute("UPDATE task_assignments SET subject=? WHERE task_id=?", (PATH, self.task_id))
        self.observe(PATH, self.actor)
        self.manage("member", **{"action": "add", "subject": self.subject})
        self.manage("handoff", subject=self.subject, task_id=self.task_id, recipient_team_id=self.team)
        self.manage("member", **{"action": "remove", "subject": PATH})
        self.assertEqual(self.member(PATH)["status"], "LEFT")
        self.assertEqual(self.member(self.subject)["status"], "ACTIVE")
        self.assertEqual(self.assignment()["subject"], self.subject)
        self.assertEqual(self.assignment()["assignment_generation"], previous + 1)
        self.assertEqual(self.acquire(self.task_id, self.actor)["attempt"], 1)
