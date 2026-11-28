"""The exact method in a job-settlement subscription can identify a newly submitted local node."""
import unittest

from acceptance.harness_refactor.probes.test_probes import CAP, probe, start
from acceptance.v62a.interleavings_fixture import branch, draft
from gx_core.errors import SchemaInvalid


def watch(event="job_settled"):
    return [{"event": event, "subject_ref": CAP}]


def researcher(study, name):
    args = study.open_args(name, inputs=study.materials, caps=[CAP])
    args.update(module_id="ad_hoc", focus="Gauge measurements", communication="isolated",
                expected_delivery="Report the instrument reading with its source.")
    return study.bind(study.call(study.lead, "gx_open_module", args, name)["task_id"])


class MethodWake(unittest.TestCase):
    def test_new_measurement_wakes_its_own_module_after_real_cpu_settlement(self):
        s = start(self, delegated=True)
        actor = researcher(s, "reader")
        op = dict(probe(s.materials), branch=s.brief(actor)["module"]["branch_id"])
        args = {"decision": draft(op, watches=watch())}
        accepted = s.call(actor, "gx_decide_revision", args, "bind-wake")
        node, = accepted["aliases"].values()
        self.assertEqual(accepted["reconsider_when"], [{"event": "job_settled", "subject_ref": node}])
        job = s.settle(node)
        self.assertEqual(job["status"], "SUCCEEDED")
        resumed = s.next_actor(actor.scope)
        self.assertNotEqual(resumed.task_id, actor.task_id)
        self.assertIn(node, [r["node_id"] for r in s.brief(resumed)["nodes"]])
        self.assertEqual(s.call(actor, "gx_decide_revision", args, "bind-wake"), accepted)
        self.assertEqual(s.rows("SELECT job_id FROM jobs"), [{"job_id": job["job_id"]}])

    def test_ambiguity_offers_only_this_drafts_labels_and_cannot_watch_a_blind_peer(self):
        s = start(self, delegated=True)
        left, right = researcher(s, "reader-A"), researcher(s, "reader-B")
        private = s.submit_node(left, "private-label", capability=CAP)["aliases"]["local:private-label"]
        before = s.head()
        for ops, event in (([], "job_settled"), ([probe(s.materials)], "evidence_added")):
            with self.subTest(event=event), self.assertRaisesRegex(SchemaInvalid, "outside the current graph") as absent:
                s.call(right, "gx_decide_revision", {"decision": draft(*ops, watches=watch(event))}, "no-" + event)
            self.assertNotIn("options", absent.exception.details)
            self.assertNotIn(private, str(absent.exception))
        pair = [dict(probe(s.materials), node=alias) for alias in ("local:direct", "local:repeat")]
        with self.assertRaises(SchemaInvalid) as ambiguous:
            s.call(right, "gx_decide_revision", {"decision": draft(*pair, watches=watch())}, "two")
        self.assertEqual(ambiguous.exception.details["field"], "reconsider_when[0].subject_ref")
        self.assertEqual(set(ambiguous.exception.details["options"]), {"local:direct", "local:repeat"})
        self.assertEqual(s.head(), before)
        self.assertNotIn(private, str(ambiguous.exception.details))

    def test_historical_wire_leaves_method_names_as_literal_subjects(self):
        s = start(self, old=True)
        route = dict(branch("old"), budget_limits={"job_submissions": 1, "cpu_seconds": 15})
        node = dict(probe(s.materials), node="local:measure", branch=route["branch"], parameters={}, depends_on=[])
        with self.assertRaisesRegex(SchemaInvalid, "outside the current graph"):
            s.call(s.lead, "gx_decide_revision", {"decision": draft(route, node, watches=watch())}, "old")
        self.assertIsNone(s.head())
