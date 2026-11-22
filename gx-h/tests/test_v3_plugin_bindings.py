"""The public hook and tool contexts re-use the Run's authoritative GX owners."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from gx_core.artifacts import ArtifactService
from gx_core.errors import LeaseExpired, SchemaInvalid, ScopeDenied
from gx_core.orchestration.hooks import DEFAULT_HOOKS
from gx_core.p0.roots import Roots
from gx_core.persistence import store
from gx_core.plugins.author_api import RegistrationContext
from gx_core.plugins.bindings import bind_hooks
from tests import test_p0_roots


class BoundOperations(unittest.TestCase):
    def setUp(self):
        self.fixture = test_p0_roots.DurableRoots()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.db, self.run = self.fixture.conn, self.fixture.run
        self.seen = []

    def configure(self, handler, hook=None):
        reg = RegistrationContext()
        reg.tools.register(name="gx_sample", schema={"type": "object"}, handler=handler)
        reg.workflows.register(workflow_id="sample", version="1", hooks=hook)
        plugin = SimpleNamespace(contributions=reg.freeze())
        hooks = bind_hooks(DEFAULT_HOOKS, [plugin], workflow_id="sample", version="1")
        original = self.fixture.roots
        tools = hooks.build_worker_tools(self.db, self.run)
        root = Roots(self.db, run_id=self.run, cwd=original.cwd, worker_tools=tools.specs, worker_call=tools.call)
        root.client = original.client
        root._request = self.fixture.fake
        self.fixture.roots = root
        patches = patch("gx_core.p0.roots.hooks_for_run", return_value=hooks)
        patches.start()
        self.addCleanup(patches.stop)
        return hooks

    def call(self, row, args, *, call="sample", turn="turn"):
        return self.fixture.roots._call({"tool": "gx_sample", "threadId": row["thread_id"], "turnId": turn,
                                         "callId": call, "arguments": args})

    def source(self, document):
        return ArtifactService(self.db).put(run_id=self.run, kind="source", media_type="application/json",
            content=json.dumps(document), origin="HOST_PUT", put_by="fixture").artifact_ref

    def test_task_scope_nested_citations_and_replay_fence(self):
        nested = self.source({"observations": [1, 3]})
        source = self.source({"nested": nested})
        peer = self.source({"private_peer_measurement": 6})
        task = self.fixture.task
        self.db.execute("UPDATE tasks SET input_json=? WHERE task_id=?", (json.dumps({"source": source}),task))

        def brief(ctx, task_id, attempt):
            self.assertFalse(hasattr(ctx, "conn"))
            doc = ctx.tasks.read_context(task_id)
            doc["ungranted"] = peer
            return doc

        def measure(ctx, args):
            self.seen.append(ctx)
            document = ctx.artifacts.read(args["ref"])
            return ctx.artifacts.read(document["nested"]) if "nested" in document else document

        self.configure(measure, {"build_task_context": brief})
        row = self.fixture.bind()
        self.assertEqual(self.call(row, {"ref": source}), {"observations": [1, 3]})
        self.assertEqual(self.call(row, {"ref": source}), {"observations": [1, 3]})
        self.assertEqual(len(self.seen), 1)
        with self.assertRaises(ScopeDenied):
            self.seen[0].tasks.inputs()
        with self.assertRaises(ScopeDenied):
            self.call(row, {"ref": peer}, call="peer")
        with self.assertRaises(ScopeDenied):
            self.call(row, {"ref": source}, turn="different")
        store.release_lease(self.db, row["lease_id"])
        with self.assertRaises(LeaseExpired):
            self.call(row, {"ref": source})

    def test_job_proposal_and_rollback_share_callback_transaction(self):
        source = self.source({"values": [1, 3]})
        self.db.execute("UPDATE tasks SET input_json=? WHERE task_id=?",
                        (json.dumps({"source": source}), self.fixture.task))

        def propose(ctx, args):
            proposal = {"capability_version": "fixture-grid@1", "inputs": {"grid": ctx.tasks.inputs()["source"]}}
            result = ctx.jobs.propose(proposal, idempotency_key=args["key"])
            if args.get("fail"):
                raise ValueError("discard this callback")
            return result

        self.configure(propose)
        row = self.fixture.bind()
        statements = []
        self.db.set_trace_callback(statements.append)
        receipt = self.call(row, {"key": "one"})
        self.db.set_trace_callback(None)
        self.assertEqual(receipt["status"], "PROPOSED")
        self.assertFalse(any(sql.startswith("SAVEPOINT") for sql in statements), statements)
        self.assertEqual(self.call(row, {"key": "one"}, call="second"), receipt)
        with self.assertRaisesRegex(SchemaInvalid, "discard this callback"):
            self.call(row, {"key": "two", "fail": True}, call="fail")
        self.assertEqual(self.db.execute("SELECT count(*) FROM p0_job_proposals").fetchone()[0], 1)
        self.assertEqual(self.db.execute("SELECT count(*) FROM p0_root_calls").fetchone()[0], 2)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_research_services_and_context_expiry(self):
        def on_source(ctx, source_id):
            self.seen.append(ctx)
            self.assertEqual(ctx.run_id, self.run)
            self.assertIsNone(ctx.task_id)
            product = ctx.artifacts.publish({"kind": "source", "document": {"source_id": source_id}},
                                            idempotency_key=source_id)
            self.assertEqual(ctx.artifacts.read(product["artifact_ref"]), {"source_id": source_id})
            proposal = {"kind": "extract_evidence", "role": "researcher", "input": product,
                        "acceptance": {}, "bundle_kind": "evidence_bundle"}
            return ctx.tasks.propose(proposal, idempotency_key=source_id)

        hooks = self.configure(lambda ctx, args: args, {"reconcile_supplement": on_source})
        first = hooks.reconcile_supplement(self.db, self.run, "new-source")
        again = hooks.reconcile_supplement(self.db, self.run, "new-source")
        self.assertEqual(again["task_ids"], first["task_ids"])
        self.assertTrue(again["replayed"])
        with self.assertRaises(ScopeDenied):
            self.seen[0].intent.items()
        with store.transaction(self.db), self.assertRaises(ScopeDenied):
            hooks.reconcile_supplement(self.db, self.run, "writer")
