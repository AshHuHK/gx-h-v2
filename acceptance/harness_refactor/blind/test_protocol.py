"""Protect paired evidence, logical-event injection and blind scientific scoring."""
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parent))
from common import HERE,PACKET,blind_text,read,sources,study_software
from score import contains_publisher_return,export,lead_delivery,scientific_timeline,summary
from trial import gx_checkpoint,official_read,terminal_before_fault
from native import final_answer

class TrialProtocol(unittest.TestCase):
    def test_source_denominators_and_freeze(self):
        registered=read(HERE/"preregistration.json")
        self.assertEqual(registered["inputs"],sources())
        self.assertEqual(registered["study_software"],study_software())
        early,late=[read(PACKET/name) for name in ("initial.json","late.json")]
        self.assertNotIn("verbatim_cells",early)
        self.assertEqual(early["linked_pubmed"],late["pmid"])
        self.assertEqual(late["verbatim_cells"]["α-pNPG"],"3.9 (54)")
        self.assertEqual(registered["blind_score_denominator"],len(read(HERE/"rubric.json")["criteria"]))
    def test_no_early_export_or_unmasking(self):
        with tempfile.TemporaryDirectory() as temp:
            work=Path(temp)
            with self.assertRaisesRegex(ValueError,"Every original arm"):
                export(work)
            for arm in ("stock","baseline","revised"):
                folder=work/arm;folder.mkdir()
                (folder/"outcome.json").write_text(json.dumps({"outcome":"finished" if arm=="stock" else "deadline"}))
            (work/"stock/second-terminal.json").write_text(json.dumps({"answer":"GX trace gxa_01abcdefabcdef. P0CW40, PMID 24649402, 54%."}))
            dest=export(work)
            packet=next(r for r in [read(p/"packet.json") for p in dest.iterdir()] if r["report"])
            self.assertIn("P0CW40, PMID 24649402, 54%",packet["report"])
            self.assertNotIn("gxa_",packet["report"]);self.assertNotIn("GX",packet["report"])
            self.assertNotIn("arm",packet)
            with self.assertRaisesRegex(ValueError,"Grading precedes"):
                summary(work)
    def test_earliest_adoption_clock_excludes_pre_intervention_and_drafts(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); (folder/"state").mkdir(); (folder/"sessions").mkdir()
            (folder/"late-delivery.json").write_text(json.dumps({"at":100}))
            (folder/"thread.json").write_text(json.dumps({"id":"scientist"}))
            prior="1970-01-01T00:01:39+00:00"
            after="1970-01-01T00:01:44+00:00"
            native=[]
            for when,phase,content in ((prior,"final_answer","Old belief"),(after,"commentary","Speculation"),(after,"final_answer","Primary source resolves unit.")):
                native.append(json.dumps({"type":"response_item","timestamp":when,"payload":{"type":"message",
                    "role":"assistant","phase":phase,"content":[{"type":"output_text","text":content}]}}))
            (folder/"sessions/rollout-scientist.jsonl").write_text("\n".join(native))
            wanted=[{"label":"D1","seconds_from_late":4.0,"statement":"Primary source resolves unit."}]
            self.assertEqual(scientific_timeline(folder,"stock"),wanted)
            db=sqlite3.connect(folder/"state/gx.db")
            try:
                db.executescript("CREATE TABLE v6_decisions(decision_ref,request_json);"
                                 "CREATE TABLE events(event_id,subject_id,kind,ts);")
                for i,when,scope,kind,reason in ((1,prior,"run","v6.decision.committed","Old belief"),
                        (2,after,"module:x","v6.decision.committed","Child hypothesis"),
                        (3,after,"run","refused","Refused draft"),
                        (4,after,"run","v6.decision.committed","Primary source resolves unit.")):
                    db.execute("INSERT INTO v6_decisions VALUES (?,?)",(i,json.dumps(
                        {"scope":scope,"decision":{"rationale":reason,"ops":[]}})))
                    db.execute("INSERT INTO events VALUES(?,?,?,?)",(i,i,kind,when))
                db.commit()
                self.assertEqual(scientific_timeline(folder,"revised"),wanted)
            finally: db.close()
    def test_submission_and_actual_lead_delivery_are_distinct(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); (folder/"sessions").mkdir()
            (folder/"late-delivery.json").write_text('{"at":100}')
            (folder/"thread.json").write_text('{"id":"lead"}')
            document=read(PACKET/"late.json")
            intro="Publisher response to your open question. Use E2 to finish the same curatorial decision.\n"
            note=intro+json.dumps(document)
            brief="GX research brief\n"+json.dumps({"pi_dialogue":[{"text":note}]})
            self.assertTrue(contains_publisher_return(brief,document))
            self.assertTrue(contains_publisher_return(json.dumps({"outer":{"source":note}}),document))
            self.assertFalse(contains_publisher_return(intro+'{"record":"E2"}',document))
            def event(thread):
                return {"type":"event_msg","timestamp":"1970-01-01T00:01:48Z","payload":{
                    "type":"item_completed","thread_id":thread,"turn_id":"q","item":{
                        "type":"UserMessage","content":[{"type":"text","text":brief}]}}}
            (folder/"sessions/rollout-router.jsonl").write_text(json.dumps(event("router")))
            self.assertIsNone(lead_delivery(folder,"stock"))
            (folder/"sessions/rollout-lead.jsonl").write_text(json.dumps(event("lead")))
            self.assertEqual(lead_delivery(folder,"stock")["at"],108)

    def test_final_output_and_terminal_fault_boundary(self):
        self.assertEqual(final_answer([{"type":"agentMessage","phase":"commentary","text":"working"}]),"")
        items=[{"type":"agentMessage","phase":"final_answer","text":x} for x in ["finding","limitation"]]
        self.assertEqual(final_answer(items),"finding\n\nlimitation")
        self.assertEqual(final_answer([{"type":"agentMessage","text":"legacy terminal"}]),"legacy terminal")
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"rollout.jsonl";point={"session":path.name,"turn_id":"original"}
            def terminal(t):return {"type":"event_msg","timestamp":"2026-11-27T12:00:00Z","payload":{"type":"task_complete","turn_id":t}}
            path.write_text(json.dumps(terminal("another")))
            self.assertIsNone(terminal_before_fault(path.parent,point))
            path.write_text(json.dumps(terminal("original")))
            self.assertEqual(terminal_before_fault(path.parent,point)["kind"],"task_complete")

    def test_official_host_must_acknowledge_the_same_complete_scientific_read(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); (folder/"sessions").mkdir()
            path=folder/"sessions/rollout-thread1.jsonl"
            callback={"thread_id":"thread1","turn_id":"turn1","call_id":"call1"}
            self.assertIsNone(official_read(folder,callback))
            item={"type":"DynamicToolCall","tool":"gx_read_artifact","id":"wrong-call",
                  "status":"completed","success":True,
                  "content_items":[{"type":"inputText","text":(PACKET/"initial.json").read_text()}]}
            def append():
                row={"timestamp":"2026-11-28T00:00:00Z","type":"event_msg","payload":{
                    **callback,"type":"item_completed","item":item}}
                with path.open("a") as dst: print(json.dumps(row),file=dst)
            append()
            self.assertIsNone(official_read(folder,callback))
            item["id"]="call1";item["success"]=False;append()
            self.assertIsNone(official_read(folder,callback))
            item["success"]=True;item["content_items"][0]["text"]='{"record":"E1"}';append()
            self.assertIsNone(official_read(folder,callback))
            item["content_items"][0]["text"]=(PACKET/"initial.json").read_text();append()
            self.assertEqual(official_read(folder,callback)["receipt_line"],4)
    def test_interruption_requires_original_delivered_read(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);(folder/"state").mkdir()
            db=sqlite3.connect(folder/"state/gx.db")
            try:
                db.executescript("CREATE TABLE p0_root_calls(thread_id,turn_id,call_id,result_json);"
                     "CREATE TABLE p0_v6_native_calls(thread_id,turn_id,call_id,request_json);")
                req=lambda name:json.dumps({"tool":name,"arguments":{"ref":"input"}})
                for i,tool,value in [(1,"gx_query_catalog",{"record":"E1"}),(2,"gx_read_artifact",{"error":"missing"})]:
                    db.execute("INSERT INTO p0_root_calls VALUES(?,?,?,?)",('t','u',i,json.dumps(value)))
                    db.execute("INSERT INTO p0_v6_native_calls VALUES(?,?,?,?)",('t','u',i,req(tool)))
                db.commit()
                self.assertEqual(gx_checkpoint(folder),[])
                db.execute("INSERT INTO p0_root_calls VALUES('t','u',3,?)",(json.dumps({"record":"E1"}),))
                db.execute("INSERT INTO p0_v6_native_calls VALUES('t','u',3,?)",(req("gx_read_artifact"),))
                db.commit()
                self.assertEqual(gx_checkpoint(folder)[0]["call_id"],3)
            finally:db.close()
