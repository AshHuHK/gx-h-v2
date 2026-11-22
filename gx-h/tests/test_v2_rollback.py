"""Deploy an upgrade and restore it at the same paths in a disposable checkout."""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest


BASELINE = "23d581c27f46e65324246dff269b6ce77c9b3e9a"


def snapshot(state_dir, destination):
    # Caller has quiesced GX and file-producing Jobs. SQLite backup sees WAL content.
    shutil.copytree(state_dir, destination,
                    ignore=shutil.ignore_patterns("gx.db", "gx.db-wal", "gx.db-shm", ".native.lock"))
    uri = (state_dir / "gx.db").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as source, closing(sqlite3.connect(destination / "gx.db")) as dest:
        source.backup(dest)
        assert dest.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def run_phase(phase, root):
    from gx_core import api, contracts
    from gx_core.artifacts.service import ArtifactService
    from gx_core.hostobs import host_policy, native_context
    from gx_core.native_release import native_state
    from gx_core.persistence import store
    from tests import test_native_router as fixture

    state_dir = root / "state"
    with native_state(state_dir, env={}) as state, closing(store.open_store(state.db_path)) as conn:
        peer = fixture.NativeRouterResume()
        peer.state, peer.conn = state, conn
        if phase != "candidate":
            calls, run_id, resumed = peer.run_session()
            operation = next(r for r in calls if r.get("method") in ("thread/start", "thread/resume"))
            assert resumed == (phase == "restored")
            if resumed:
                assert operation["params"]["threadId"] == fixture.ROOT
                assert "dynamicTools" not in operation["params"]
        else:
            run_id = conn.execute("SELECT run_id FROM runs").fetchone()[0]
            operation = None
        policy, _ = host_policy.native_codex_policy(state_dir)
        authority = native_context.NativeAuthority(project_ref=fixture.PROJECT)
        authority.register_thread({"id": fixture.ROOT, "sessionId": fixture.ROOT, "parentThreadId": None,
            "forkedFromId": None, "cliVersion": "0.169.0", "model": policy["pinned_model"]["id"],
            "reasoningEffort": policy["native_coordinator_observation"]["default_effort"]}, sequence=1, root=True)
        authority.start_turn(fixture.ROOT, "compat", sequence=2)

        def invoke(tool, args):
            call = authority.make_call({"threadId": fixture.ROOT, "turnId": "compat", "callId": tool,
                "tool": tool, "arguments": args}, sequence=authority.sequence + 1)
            try:
                return api.dispatch(tool, args, principal=call.principal, conn=conn,
                                    transport=native_context.TRANSPORT, native_call=call)
            finally:
                authority.complete(call)

        read = invoke("gx_run_status", {"run_id": run_id})
        assert read["ok"], read
        resume = None
        if phase == "candidate":
            run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            resume = invoke("gx_run_resume", {"run_id": run_id, "interrupt_ref": run["interrupt_ref"],
                "coordinator_lease_ref": conn.execute("SELECT lease_id FROM coordinator_leases WHERE run_id=?",
                    (run_id,)).fetchone()[0], "submission_refs": [], "expected_revision": run["revision"]})
            assert not resume["ok"], resume
            assert resume["error"]["code"] == "CONTENT_HASH_MISMATCH", resume
        artifact = ArtifactService(conn)
        refs = {}
        for ref, digest in conn.execute("SELECT artifact_ref,sha256 FROM artifacts WHERE published=1 ORDER BY artifact_ref"):
            assert artifact.verify(ref)["ok"]
            refs[ref] = digest
        assert refs
        report = {"phase": phase, "run_id": run_id, "actual_store_pin": store.read_meta(conn)["contract_sha256"],
            "original_run_pin": conn.execute("SELECT contract_sha256 FROM runs WHERE run_id=?", (run_id,)).fetchone()[0],
            "installed_pin": contracts.contract_digest()["__combined__"], "state": read["data"]["status"],
            "artifact_digests": refs, "task_ids": [r[0] for r in conn.execute("SELECT task_id FROM tasks ORDER BY task_id")],
            "jobs": conn.execute("SELECT count(*) FROM jobs").fetchone()[0],
            "run_count": conn.execute("SELECT count(*) FROM runs").fetchone()[0],
            "native_protocol_method": operation["method"] if operation else None,
            "candidate_resume_error": resume["error"]["code"] if resume else None}
        if phase == "baseline":
            assert (state.db_path.parent / "gx.db-wal").stat().st_size > 0
            snapshot(state_dir, root / "backup")
        print(json.dumps(report))


class Rollback(unittest.TestCase):
    def test_wal_snapshot_restores_threads_tasks_and_artifacts_after_upgrade(self):
        checkout = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="gx-migrate-restore-") as tmp:
            root = Path(tmp)
            source = root / "source"
            subprocess.run(["git", "clone", "--quiet", "--local", "--no-hardlinks", "--no-checkout", str(checkout), str(source)], check=True)
            subprocess.run(["git", "checkout", "--quiet", BASELINE], cwd=source, check=True)

            def run(phase):
                # -c keeps the scratch deployment first on sys.path in every interpreter.
                command = "import runpy,sys; runpy.run_path(sys.argv[1])['run_phase'](sys.argv[2], __import__('pathlib').Path(sys.argv[3]))"
                output = subprocess.check_output([sys.executable, "-B", "-c", command, __file__, phase, str(root)],
                                                 cwd=source, text=True)
                return json.loads(output)

            old = run("baseline")
            candidate = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=checkout, text=True).strip()
            subprocess.run(["git", "checkout", "--quiet", candidate], cwd=source, check=True)
            upgraded = run("candidate")
            self.assertNotEqual(old["actual_store_pin"], upgraded["actual_store_pin"])
            self.assertEqual(upgraded["actual_store_pin"], upgraded["installed_pin"])
            self.assertEqual(upgraded["candidate_resume_error"], "CONTENT_HASH_MISMATCH")
            (root / "state").rename(root / "upgraded-state")
            shutil.copytree(root / "backup", root / "state")
            subprocess.run(["git", "checkout", "--quiet", BASELINE], cwd=source, check=True)
            restored = run("restored")
            self.assertEqual(old["actual_store_pin"], restored["actual_store_pin"])
            self.assertEqual(restored["native_protocol_method"], "thread/resume")
            self.assertEqual(restored["run_count"], 1)
            for key in ("run_id", "original_run_pin", "state", "artifact_digests", "task_ids", "jobs"):
                self.assertEqual(upgraded[key], old[key], key)
                self.assertEqual(restored[key], old[key], key)
            if output := os.environ.get("GX_MIGRATION_TEST_REPORT"):
                Path(output).write_text(json.dumps({
                    "scope_task_id": "P02", "scenario_id": "SC24", "mode": "offline_fake",
                    "boundaries": "real SQLite WAL backup, files and two checkouts, scripted App Server peer",
                    "baseline_commit": BASELINE, "candidate_commit": candidate,
                    "phases": [old, upgraded, restored]
                }, indent=2) + "\n")
