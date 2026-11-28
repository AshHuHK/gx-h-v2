"""Two archive records under ordinary Goal admission, with controlled native actors."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from acceptance.harness_refactor.modules.research_study import ResearchStudy
from gx_core.errors import ScopeDenied, SchemaInvalid
from gx_core.p0 import research_topics


class ScopedLineage(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        inputs = []
        for name in ("observations", "coverage"):
            path = self.directory / (name + ".txt")
            path.write_text("Archive record of " + name)
            inputs.append({"path": str(path), "role": "material"})
        self.study = self.enterContext(ResearchStudy(self.directory, spec_updates={
            "research_effort":"deep", "capabilities":[], "research_modules":["ad_hoc@1"], "input_files":inputs}))
        self.first, self.second = self.study.materials
        self.seq = 0

    def open(self, name, refs, communication="shared"):
        s = self.study
        args = s.open_args(name, inputs=refs, caps=[], limits={"api_calls":3,"worker_tasks":2,"tokens":3000})
        args.update(module_id="ad_hoc", expected_delivery="Report the archive evidence.", communication=communication)
        opened = s.call(s.lead, "gx_open_module", args, name)
        return s.bind(opened["task_id"])

    def hint(self, actor, topic, action, **kwargs):
        self.seq += 1
        key = "exchange-" + str(self.seq)
        return self.study.call(actor, "gx_send_research_hint", dict(operation_key=key, topic=topic, action=action, **kwargs), key)

    def context(self, actor):
        self.seq += 1
        return self.study.call(actor, "gx_task_context", {}, "context-" + str(self.seq))

    def test_cross_topic_synthesis_replay_disclosure_and_source_correction(self):
        record = self.open("record", [self.first])
        coverage = self.open("coverage", [self.second])
        editor = self.open("editing", self.study.materials)
        reader = self.open("shared-reader", self.study.materials)
        restricted = self.open("restricted-reader", [self.first])
        blind = self.open("independent-reader", self.study.materials, "isolated")
        a = self.hint(record,"readings","publish",interpretation="Measured at noon.",source_refs=[self.first])
        b = self.hint(coverage,"coverage","publish",interpretation="Only the east station is represented.",source_refs=[self.second])
        for topic,parent in (("readings",a),("coverage",b)):
            self.assertEqual(self.hint(editor,topic,"subscribe")["entries"][0]["hint_id"],parent["hint_id"])
        for member in (reader,restricted):
            self.hint(member,"joint","subscribe")
        with self.assertRaises(ScopeDenied):
            self.hint(blind,"joint","subscribe")
        with self.assertRaises(ScopeDenied):
            self.hint(restricted,"joint","publish",question="Which other data exist?",parent_hint_ids=[b["hint_id"]])
        publish = {"topic":"joint","action":"publish","operation_key":"combined-reading",
                   "interpretation":"The noon observation covers only the east.","parent_hint_ids":[a["hint_id"],b["hint_id"]]}
        joined = self.study.call(editor,"gx_send_research_hint",publish,"combined-reading")
        with patch.object(research_topics,"visible_refs",return_value={self.first}):
            for call_id in ("combined-reading","reissue"):
                with self.assertRaises(ScopeDenied):
                    self.study.call(editor,"gx_send_research_hint",publish,call_id)
        self.assertEqual(set(joined["origin_hint_ids"]),{a["hint_id"],b["hint_id"]})
        self.assertEqual(len(self.study.rows("SELECT 1 FROM outbox WHERE topic=? AND json_extract(payload_json,'$.scope')=?",
                                             research_topics.NOTICE,restricted.scope)),0)
        shown = self.context(reader)["research_topics"][0]["entries"][0]
        self.assertEqual(shown["parent_hint_ids"],[a["hint_id"],b["hint_id"]])
        self.assertEqual(set(shown["original_source_refs"]),set(self.study.materials))
        hidden = self.context(restricted)
        self.assertEqual(hidden["research_topics"][0]["entries"],[])
        self.assertNotIn("joint",hidden["available_research_topics"])
        self.assertNotIn(b["hint_id"],json.dumps(hidden))
        self.assertNotIn(joined["hint_id"],json.dumps(hidden))
        self.assertFalse(self.context(blind)["research_topics"])
        # Replace the database connection and all native transport objects, retaining the leases.
        self.study.stack.close()
        with ResearchStudy(self.directory,reopen=True) as cold:
            self.study = cold
            for m in (editor,reader,restricted,coverage):
                cold.roots.active_turns[m.thread] = m.turn
            self.assertEqual(self.context(reader)["research_topics"][0]["entries"],[])
            revised = self.hint(coverage,"coverage","correct",parent_hint_ids=[b["hint_id"]],
                                interpretation="The archived window belongs to the west.",source_refs=[self.second])
            with self.assertRaises(ScopeDenied):
                self.hint(editor,"joint","forward",parent_hint_ids=[joined["hint_id"]])
            page = self.context(reader)["research_topics"][0]
            self.assertEqual([m["replaces"] for m in page["entries"]],[joined["hint_id"]])
            self.assertNotIn(revised["hint_id"],json.dumps(page))
            self.assertEqual(self.context(reader)["research_topics"][0]["entries"],[])
            self.assertEqual(self.context(restricted)["research_topics"][0]["entries"],[])
            self.assertEqual(self.hint(coverage,"joint","subscribe")["entries"],[])
            if out := os.environ.get("GX_TOPIC_SYNTHESIS_RECEIPT"):
                receipt = {"run":cold.run_id, "evidence_class":"SCRIPTED_CALLBACK", "parent_publications":[a,b],
                           "synthesis":joined,"delivered_entry":shown,"restricted_wakes":0,
                           "restricted_entries":hidden["research_topics"][0]["entries"],
                           "isolated_entries":[], "narrowed_grant_send_replay":"SCOPE_DENIED",
                           "correction":revised,"post_restart_withdrawal":page["entries"]}
                Path(out).write_text(json.dumps(receipt,indent=2)+"\n")

    def test_cross_topic_forward_carries_one_root_to_new_topic(self):
        a = self.open("first",self.study.materials)
        c = self.open("forwarder",self.study.materials)
        msg = self.hint(a,"source","publish",question="Does noon match on the two clocks?",source_refs=[self.first])
        self.hint(c,"source","subscribe")
        fwd = self.hint(c,"clock-notes","forward",parent_hint_ids=[msg["hint_id"]])
        self.assertEqual(fwd["origin_hint_ids"],[msg["hint_id"]])
        late = self.open("late",self.study.materials)
        self.assertIn("clock-notes",self.context(late)["available_research_topics"])
        shown, = self.hint(late,"clock-notes","subscribe")["entries"]
        self.assertEqual(shown["origin_hint_ids"],[msg["hint_id"]])
        self.assertEqual(shown["question"],"Does noon match on the two clocks?")
        self.hint(a,"source","retract",parent_hint_ids=[msg["hint_id"]])
        notice, = self.context(late)["research_topics"][0]["entries"]
        self.assertEqual(notice["replaces"],fwd["hint_id"])


class Reinterpretation(unittest.TestCase):
    def test_already_visible_uncited_record_reopens_only_its_route_as_reinterpretation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            inputs = []
            for name in ("observations","sampling frame"):
                p = path/(name+".txt")
                p.write_text(name)
                inputs.append({"path":str(p),"role":"material"})
            with ResearchStudy(path,spec_updates={"input_files":inputs,"capabilities":[]}) as s:
                def decide(actor, key, ops, reason, refs=()):
                    return s.call(actor,"gx_decide_revision",{"decision":{"ops":ops,"rationale":reason,
                                  "evidence_refs":list(refs)}},key)
                def graph(version):
                    return json.loads(s.rows("SELECT graph_json FROM v6_graph_versions WHERE run_id=? AND version=?",
                                             s.run_id,version)[0]["graph_json"])["intents"]
                a,b = s.materials
                opened = decide(s.lead,"open",[{"op":"open_intent","intent":"local:rank","question":"Can the stations be ranked?"},
                    {"op":"open_intent","intent":"local:units","question":"Which units were recorded?"}],"Check the archived claims.")
                rid,other = [opened["aliases"]["local:"+name] for name in ("rank","units")]
                actor = s.next_actor("run")
                frozen = s.brief(actor)
                self.assertIn(b,json.dumps(frozen["materials"]))
                concluded = decide(actor,"settled",[{"op":"converge_intent","intent":rid,"revision":1,"evidence_refs":[a]},
                    {"op":"resolve_intent","intent":other,"revision":1,"evidence_refs":[b]},
                    {"op":"open_intent","intent":"local:scope","question":"Does the sampling frame match?"}],"The central estimates support a ranking.",[a,b])
                before = graph(concluded["result_head"]["version"])
                actor = s.next_actor("run")
                with self.assertRaises(SchemaInvalid):
                    decide(actor,"unchanged",[{"op":"reopen_intent","intent":rid,"revision":2,"evidence_refs":[a]}],
                           "The central estimates support a ranking.",[a])
                reason = "The sampling frame omits the last day, undermining the station ranking."
                amendment = decide(actor,"review-window",[{"op":"reopen_intent","intent":rid,"revision":2,"evidence_refs":[b]}],reason,[b])
                revised = graph(amendment["result_head"]["version"])
                self.assertEqual((revised[rid]["revision"],revised[rid]["phase"]),(3,"OPEN"))
                self.assertEqual(revised[other],before[other])
                self.assertEqual(graph(concluded["result_head"]["version"]),before)
                basis = revised[rid]["reopen_basis"]
                self.assertEqual((basis["kind"],basis["change_reason"]),("reinterpretation",reason))
                self.assertEqual(basis["prior_decision_ref"],concluded["decision_ref"])
                self.assertEqual(basis["prior_evidence_refs"],[a])
                self.assertEqual(s.rows("SELECT job_id FROM jobs"),[])
                if out := os.environ.get("GX_ROUTE_REINTERPRETATION_RECEIPT"):
                    Path(out).write_text(json.dumps(dict(run=s.run_id, intent=rid, prior=before[rid],
                        revised=revised[rid], alternate_unchanged=True, job_count=0),indent=2)+"\n")


if __name__ == "__main__":
    unittest.main()
