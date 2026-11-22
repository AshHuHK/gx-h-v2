"""Kill the router after SQLite commit and before the App Server learns the outcome."""
import json
import multiprocessing
import os
import signal
import sys
import unittest
from unittest.mock import patch

from gx_core import api
from gx_core.artifacts.delivery import prepare_manifest
from gx_core.artifacts.service import ArtifactService
from gx_core.hostobs import app_server_client as app, native_app_server as native
from gx_core.hostobs.session_receipts import closure_obstacle, committed_facades, native_sessions, recoverable_facade_crash
from gx_core.native_cli import _record_launch
from gx_core.persistence import store
from tests import test_module_collaboration as collaboration


class RouterFixtureAuthority(collaboration.native_context.NativeAuthority):
    """Use the router's default Root identity when creating the fixture project."""

    def __init__(self, *, project_ref, root_subject):
        super().__init__(project_ref=project_ref)


def wire_frames(policy, operations, resumed=False):
    root, turn = collaboration.ROOT, "root-resumed" if resumed else "root-first"
    model, effort = policy["pinned_model"]["id"], policy["native_coordinator_observation"]["default_effort"]
    thread = {"id": root, "sessionId": root, "parentThreadId": None, "forkedFromId": None,
              "cliVersion": "0.169.0", "model": model, "reasoningEffort": effort}
    frames = [
        {"id": 1, "result": {"userAgent": "fixture"}},
        {"id": 2, "result": {"config": {"mcp_servers": {}, "experimental_thread_store": {"type": "local"}},
                              "origins": {}}},
        {"id": 3, "result": {"thread": thread, "model": model, "reasoningEffort": effort}},
        {"method": "turn/started", "params": {"threadId": root, "turn": {"id": turn, "status": "inProgress"}}},
        {"id": 4, "result": {"turn": {"id": turn}}},
    ]
    for index, (tool, args) in enumerate(operations):
        frames.append({"id": 101 + index, "method": "item/tool/call", "params": {"threadId": root, "turnId": turn,
            "callId": "op-" + str(index), "tool": tool, "arguments": args}})
    frames.append({"method": "turn/completed", "params": {"threadId": root, "turn": {"id": turn, "status": "completed"}}})
    return frames


def peer(frames):
    # The parent owns the peer so it can verify the live-host barrier, then close and reap
    # it. Frames are scripted. The stdio child, router driver, SIGKILL and WAL are real.
    payload = "\n".join(json.dumps(frame) for frame in frames)
    return app.StdioTransport([sys.executable, "-c", "import sys;print(sys.argv[1],flush=True);sys.stdin.buffer.read()", payload])


def session(state, transport, event_sink=None):
    def factory(**options):
        return app.AppServerClient(transport=transport, on_notification=options["on_notification"],
                                   on_server_request=options["on_server_request"], default_timeout=2)
    conn = store.open_store(state.db_path)
    try:
        return native.run_session("Continue the same project", state=state, conn=conn,
            project_ref=collaboration.PROJECT, max_wall_seconds=8, max_turns=4, max_workers=2,
            tool_policy={"tool_profile": "native_v2", "allow_mutations": True, "max_callbacks": 5},
            client_factory=factory, event_sink=event_sink)
    finally:
        conn.close()


def killed_after(state, transport, target):
    dispatch = api.dispatch

    def terminate_on_commit(tool, *args, **kwargs):
        result = dispatch(tool, *args, **kwargs)
        if tool == target:
            if not result["ok"]:
                raise AssertionError(result)
            os.kill(os.getpid(), signal.SIGKILL)
        return result

    _record_launch(state, "crash", {"max_wall_seconds": 8}, b"Continue the same project", max_wall_seconds=8)
    with (patch.object(api, "dispatch", terminate_on_commit),
          (state.directory / "events-crash.jsonl").open("x", encoding="utf-8") as journal):
        def record(event):
            journal.write(json.dumps(event) + "\n")
            journal.flush()
        result = session(state, transport, record)
    raise AssertionError(result)


@unittest.skipUnless(sys.platform.startswith("linux"), "requires Linux process-group observation")
class NativeCrashReceipts(unittest.TestCase):
    def exercise(self, finish):
        project = collaboration.ModuleCollaboration()
        self.addCleanup(project.doCleanups)
        with patch.object(collaboration.native_context, "NativeAuthority", RouterFixtureAuthority):
            project.setUp()
        task = project.ready("module_plan", "keys")[0]["task_id"]
        team = project.module_teams["keys"]
        # Assign the registered root thread so this test isolates post-commit loss
        # from child discovery, which has separate adapter tests.
        subject = project.subject(collaboration.ROOT)
        project.manage("member", {"team_id": team, "subject": subject, "action": "add"})
        project.manage("assign", {"team_id": team, "task_id": task, "subject": subject})
        operations = [("gx_task_acquire", {"task_id": task})]
        if finish:
            artifacts = ArtifactService(project.conn)
            inbox = artifacts.inbox_dir(project.run_id)
            (inbox / "plan.json").write_text(json.dumps({"items": [{"key": "a", "role": "executor", "question": "What changed?"}]}))
            ref = prepare_manifest(artifacts, run_id=project.run_id, task_id=task, attempt=1,
                                  entries=[{"inbox_path": "plan.json", "kind": "module_plan",
                                            "media_type": "application/json"}], provenance={"method": "fixture/1"})
            operations.append(("gx_task_finish", {"task_id": task, "manifest_ref": ref}))
        transport = peer(wire_frames(project.policy, operations))
        self.addCleanup(transport.close)
        driver = multiprocessing.get_context("fork").Process(target=killed_after,
            args=(project.state, transport, operations[-1][0]))
        driver.start()
        driver.join(15)
        if driver.is_alive():
            driver.kill()
            driver.join()
            self.fail("driver did not reach post-commit failure window")
        self.assertEqual(driver.exitcode, -signal.SIGKILL)
        desc = json.loads((project.state.directory / native.SESSION_FILE).read_text())
        self.assertTrue(desc["cleanup_pending"])
        self.assertIsNone(desc["pending_effects"][0]["request_id"])
        self.assertFalse(recoverable_facade_crash(project.state.directory, desc))
        transport.close()
        self.assertTrue(recoverable_facade_crash(project.state.directory, desc))
        effect, = desc["pending_effects"]
        wrong_domain = {**desc, "host_process_group": {**desc["host_process_group"], "boot": "unrelated-boot"}}
        self.assertFalse(recoverable_facade_crash(project.state.directory, wrong_domain))
        for field in ("task_id", "native_call_sha256", "call_id", "thread_id", "arguments_sha256"):
            wrong = {**desc, "pending_effects": [{**effect, field: "bogus"}]}
            self.assertEqual(committed_facades(project.state.directory, wrong), {})
        # The project driver must see the committed original before allowing resume.
        launches = native_sessions(project.state.directory, latest_events_only=True)
        self.assertEqual(launches[-1]["root_thread_id"], collaboration.ROOT)
        self.assertIsNone(launches[-1]["status"])
        proofs = []
        self.assertIsNone(closure_obstacle(launches, desc, collaboration.PROJECT, state=project.state.directory,
                                           model=project.policy["pinned_model"]["id"], settled_effects=proofs))
        self.assertEqual(proofs[0]["task_id"], task)
        resumed = session(project.state, peer(wire_frames(project.policy, [], resumed=True)))
        self.assertEqual(resumed["status"], "COMPLETED", resumed)
        self.assertTrue(resumed["resumed"])
        self.assertFalse(resumed["recovery_only"])
        self.assertEqual(resumed["GX_calls"], 0)
        self.assertEqual(project.conn.execute("SELECT count(*) FROM leases WHERE task_id=?", (task,)).fetchone()[0], 1)
        self.assertEqual(project.conn.execute("SELECT count(*) FROM submissions WHERE task_id=?", (task,)).fetchone()[0], int(finish))

    def test_committed_acquire_survives_driver_sigkill(self):
        self.exercise(False)

    def test_committed_finish_survives_driver_sigkill(self):
        self.exercise(True)
