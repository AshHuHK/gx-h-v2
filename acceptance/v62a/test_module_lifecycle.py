"""Delegation fencing on the public Goal and its immutable method/scope grants."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

if __package__:
    from .interleavings_fixture import Scenario
else:
    from interleavings_fixture import Scenario
from gx_core.errors import IdempotencyConflict, SchemaInvalid, ScopeDenied, StaleRevision
from gx_core.p0 import v62_modules


class ModuleLifetime(unittest.TestCase):
    def setUp(self):
        where = self.enterContext(TemporaryDirectory(prefix="gx62-lifecycle-"))
        self.session = self.enterContext(
            Scenario(Path(where), spec_updates={"module_policy": {"catalog_limit": 1}})
        )

    def test_compact_catalog_narrowed_tool_grants_and_key_replay(self):
        s = self.session
        self.assertEqual(s.brief(s.lead)["catalog_view"]["mode"], "categories")
        p = s.open_args("narrow", inputs=[s.materials[0]])
        p["tools"] = ["gx_read_artifact", "gx_submit_module_report"]
        answer = s.call(s.lead, "gx_open_module", p, "first-native-call")
        twin = s.call(s.lead, "gx_open_module", p, "second-native-call")
        self.assertEqual(answer, twin)
        actor = s.next_actor(answer["scope"])
        self.assertEqual(set(s.brief(actor)["allowed_tools"]), set(p["tools"]))
        self.assertEqual(
            v62_modules.visible_refs(s.conn, v62_modules.require_active(s.conn, s.run_id, actor.scope)),
            [s.materials[0]],
        )
        with self.assertRaises(IdempotencyConflict):
            s.call(s.lead, "gx_open_module", {**p, "focus": "different purpose"}, "changed-key")
        with self.assertRaises(ScopeDenied):
            s.call(
                s.lead,
                "gx_open_module",
                {**p, "operation_key": "different", "tools": ["gx_open_module"]},
                "self-escalation",
            )
        with self.assertRaises(SchemaInvalid):
            s.call(
                s.lead,
                "gx_open_module",
                {**p, "operation_key": "different", "tools": ["gx_read_artifact"]},
                "no-report",
            )
        self.assertEqual(s.conn.execute("SELECT count(*) FROM v62_modules").fetchone()[0], 1)

    def test_material_removed_between_prepare_and_commit_rolls_back_all_admission(self):
        s = self.session
        actor = s.lead
        initial = s.conn.execute("SELECT count(*) FROM budgets").fetchone()[0]
        plan = v62_modules.prepare_open(
            s.conn,
            run_id=s.run_id,
            task_id=actor.task_id,
            lease_id=actor.lease_id,
            thread_id=actor.thread,
            turn_id=actor.turn,
            call_id="interleave",
            payload=s.open_args("snapshot", inputs=[s.materials[0]]),
        )
        s.conn.execute(
            "UPDATE v6_goals SET materials_json=? WHERE run_id=?", (json.dumps(s.materials[1:]), s.run_id)
        )
        with self.assertRaises(StaleRevision):
            v62_modules.commit_open(s.conn, plan)
        self.assertEqual(s.conn.execute("SELECT count(*) FROM v62_modules").fetchone()[0], 0)
        self.assertEqual(s.conn.execute("SELECT count(*) FROM budgets").fetchone()[0], initial)

    def test_accepted_job_survives_unrelated_changes_and_proof_is_bound_to_the_new_task(self):
        s = self.session
        opened = s.open("job", inputs=[s.materials[0]])
        actor = s.next_actor(opened["scope"])
        first = s.submit_node(actor, "cover")
        node = s.settle(first["resolved_ops"][0]["spec"]["node"])
        follow = s.next_actor(opened["scope"])
        (proof,) = s.brief(follow)["verified_jobs"]
        self.assertNotEqual(follow.task_id, actor.task_id)
        self.assertEqual(proof["exec_key"], node["exec_key"])
        self.assertEqual(proof["artifact_ref"], node["result_ref"])
        self.assertEqual(len(proof["admission_sha256"]), 64)
        self.assertEqual(s.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        s.conn.execute(
            "UPDATE v6_goals SET materials_json=? WHERE run_id=?", (json.dumps(s.materials[:1]), s.run_id)
        )
        v62_modules.require_active(s.conn, s.run_id, opened["scope"])
        s.conn.execute("UPDATE v6_goals SET materials_json='[]' WHERE run_id=?", (s.run_id,))
        self.assertTrue(v62_modules.advance(s.conn, s.run_id))
        self.assertEqual(s.conn.execute("SELECT status FROM v62_modules").fetchone()[0], "STALE")
        self.assertEqual(
            s.conn.execute("SELECT result_ref FROM v6_nodes").fetchone()[0], node["result_ref"]
        )
        self.assertEqual(s.conn.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
