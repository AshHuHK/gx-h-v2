"""Ordinary Goal admission, module callbacks and claims with an offline Codex transport."""
import json
import os
import subprocess
import sys
import runpy
import shutil
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from acceptance.harness_refactor.closing.test_closeout import Study, insufficient
from acceptance.v62a.interleavings_fixture import PRODUCT, draft
from gx_core.artifacts import ArtifactService
from gx_core.errors import SchemaInvalid
from gx_core.persistence import store
from gx_core.p0 import manifest, metering


CONFIG = {"research_modules": ["data_inventory@1"], "capabilities": ["tl1a-coordinate-quality@1"],
          "plugins": ["org.gx.protein@0.2.0"], "closing_terms": {"core_materials": [{"role":"local_pair"}]}}
PARTIAL = {"operation_key":"deliver", "status":"PARTIAL", "findings":[], "artifact_refs":[],
           "gaps":["The local comparison is unfinished."], "next_steps":[]}


def completed_claim(instance, ref):
    return {"statement":"The assigned comparison report was delivered.", "scope":"This one commission",
            "criteria":"Sourced delivery by the assigned researcher", "limitations":"Only the assigned sites",
            "rationale":"The report identifies its author and evidence.", "level":"insufficient",
            "evidence_refs":[ref], "evidence_check":{"grade":"delivery_completed", "source_ref":ref,
                                                     "module_instance_id":instance}}


def save_evidence(name, data):
    if directory := os.environ.get("GX_MODULE_FACTS_RECEIPTS"):
        (Path(directory)/(name+".json")).write_text(json.dumps(data,indent=2,ensure_ascii=False)+"\n")


class ModuleFacts(unittest.TestCase):
    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_partial_report_released_before_one_assignment_advisory(self):
        with Study(self.work, spec_updates=CONFIG) as s:
            member = s.open("local", caps=[], inputs=s.materials)
            worker = s.bind(member["task_id"])
            s.call(s.lead, "gx_decide_revision", {"decision":draft()}, "wait")
            call = SimpleNamespace(message={"method":"item/tool/call", "params":{"threadId":worker.thread,
                "turnId":worker.turn, "namespace":"gx", "callId":"return","tool":"gx_submit_module_report",
                "arguments":PARTIAL}})
            reply = s.call(worker, "gx_submit_module_report", PARTIAL, "return")
            wire = s.roots._callback(call)
            self.assertTrue(wire["success"])
            self.assertEqual(json.loads(wire["contentItems"][0]["text"]),reply)
            self.assertTrue(s.roots.terminal_products.is_terminal(worker.thread,worker.turn,"return"))
            self.assertEqual(reply["author"]["kind"], "MODULE")
            self.assertEqual(reply["status"], "PARTIAL")
            lead = s.next_actor("run")
            self.assertIn(reply["report_ref"], s.brief(lead)["evidence_refs"])
            body = ArtifactService(s.conn).read_json(reply["report_ref"])
            self.assertEqual(body["gaps"], PARTIAL["gaps"])
            self.assertEqual(body["findings"], [])
            self.assertEqual(reply["delivery_advisory"]["materials"][0]["ref"], s.materials[0])
            self.assertEqual(reply["delivery_advisory"]["materials"][0]["state"], "acquired_unread")
            self.assertEqual(reply,s.call(worker, "gx_submit_module_report", PARTIAL, "return"))
            self.assertEqual(s.roots._callback(call),wire)
            self.assertEqual(len(s.rows("SELECT 1 FROM v62_module_reports")),1)
            self.assertEqual(len(s.rows("SELECT 1 FROM v62_module_wakes")),1)
            self.assertEqual(len(s.rows("SELECT 1 FROM events WHERE kind='research.close.module_advisory'")), 1)
            save_evidence("partial-callback", {"run_id":s.run_id,"transport":"scripted official tool envelope",
                "module_instance_id":member["module_instance_id"], "report_ref":reply["report_ref"],
                "publish_event":reply["event_id"], "wake_event":reply["wake_event_id"],
                "advisory":reply["delivery_advisory"], "terminal_recorded":True,
                "callback_identical_on_replay":True, "reports":1,"wakes":1,"advisories":1})
            with self.assertRaisesRegex(SchemaInvalid, "complete|COMPLETE"):
                s.call(lead,"gx_stage_claim",completed_claim(member["module_instance_id"], reply["report_ref"]), "overclaim")
        # Lose the connection between durable commit and receiving its tool
        # answer. Only the original bound callback can recover the same receipt.
        with Study(self.work,reopen=True) as cold:
            self.assertEqual(cold.roots._callback(call),wire)
            self.assertEqual(len(cold.rows("SELECT 1 FROM events WHERE kind='research.close.module_advisory'")),1)
            self.assertEqual(len(cold.rows("SELECT 1 FROM v62_module_reports")),1)

    def test_complete_sourced_report_matches_its_exact_commission(self):
        with Study(self.work, spec_updates=CONFIG) as s:
            member = s.open("return", caps=[], inputs=s.materials)
            worker = s.bind(member["task_id"])
            s.call(worker,"gx_read_artifact", {"ref":s.materials[0], "operation_key":"source"}, "read")
            s.call(s.lead,"gx_decide_revision", {"decision":draft()}, "wait")
            reply = s.report(worker,s.materials[0], text="The paired series records a positive signed error.")
            self.assertNotIn("delivery_advisory",reply)
            lead = s.next_actor("run")
            claim = completed_claim(member["module_instance_id"], reply["report_ref"])
            staged = s.call(lead,"gx_stage_claim",claim,"grounded")
            document = ArtifactService(s.conn).read_json(staged["claim_ref"])
            self.assertEqual(document["evidence_grade_receipt"]["report_sha256"],reply["report_sha256"])
            self.assertEqual(document["evidence_grade_receipt"]["grade"],"report_received")
            s.call(lead,"gx_watch_join",{"key":"prediction","question":"Does a separate forecast match the field?",
                "mode":"all","ports":[{"slot":"forecast","required":True,"alternatives":[
                    {"module_instance_id":member["module_instance_id"],"output_name":"prediction.json"}]}]}, "commit-product")
            with self.assertRaisesRegex(SchemaInvalid, "required named delivery"):
                s.call(lead,"gx_stage_claim",claim,"complete-csv-is-not-a-forecast")
            claim["evidence_check"]["module_instance_id"] = "different-commission"
            with self.assertRaises(SchemaInvalid):
                s.call(lead,"gx_stage_claim",claim,"wrong-commission")

    def test_query_scope_timeout_next_page_and_zero_survive_report_handoff(self):
        # All network responses are supplied here. The ordinary offline replay
        # method in the Goal executes real Jobs and validates the archived bytes.
        plugin = PRODUCT / "gx-h/plugins/installed"
        api = runpy.run_path(str(plugin/"org.gx.scholarly-sources/1.0.0/methods/scholarly.py"))
        pages = runpy.run_path(str(plugin/"org.gx.scholarly-pages/1.1.0/methods/windows.py"))
        query = 'DOI:"10.1234/example"'
        def responses(url):
            if "ebi.ac.uk" in url:
                return b'{"hitCount":0,"resultList":{}}'
            raise api["UpstreamUnavailable"]("controlled timeout")
        _, snapshot = pages["discover"](api, {"schema":api["REQUEST"]}, query, "first", responses)
        file = self.work/"snapshot.json"
        file.write_text(json.dumps(snapshot))
        # Same method on another archived query with a genuine next-page cursor.
        query2 = "field instrument comparison"
        def second(url):
            if "ebi.ac.uk" in url:
                return b'{"hitCount":0,"resultList":{}}'
            return b'{"message":{"items":[{"DOI":"10.1234/observed"}],"total-results":2,"next-cursor":"page_two"}}'
        _, other = pages["discover"](api, {"schema":api["REQUEST"]}, query2, "first", second)
        file2 = self.work/"snapshot2.json"
        file2.write_text(json.dumps(other))
        exact_query = 'DOI:"10.1234/no-record"'
        def both_empty(url):
            if "ebi.ac.uk" in url:
                return b'{"hitCount":0,"resultList":{}}'
            return b'{"message":{"items":[],"total-results":0}}'
        _, empty_snapshot = pages["discover"](api, {"schema":api["REQUEST"]}, exact_query, "first", both_empty)
        file3 = self.work/"exact-empty.json"
        file3.write_text(json.dumps(empty_snapshot))
        method = "scholarly-page-search@2"
        config = {"research_modules":["scholarly_page_investigation@2"], "capabilities":[method],
                  "plugins":["org.gx.scholarly-pages@1.1.0"], "input_files":[
                      {"role":"source","path":str(f)} for f in (file,file2,file3)]}
        with Study(self.work/"field", spec_updates=config) as s:
            args = s.open_args("search", caps=[method], inputs=s.materials, limits={"cpu_seconds":300,"job_submissions":3})
            args.update(module_id="scholarly_page_investigation", module_version="2", focus="literature",
                        expected_delivery="Report what each queried catalog returned for the requested scope.")
            member = s.call(s.lead,"gx_open_module",args,"assign")
            worker = s.bind(member["task_id"])
            s.call(s.lead,"gx_decide_revision",{"decision":draft()},"wait")
            results = []
            for i,(source,scope) in enumerate(zip(s.materials,(query,query2,exact_query))):
                label = "query"+str(i)
                node = s.submit_node(worker,label,source,capability=method,parameters={"query":scope,"action":"replay"})
                settled = s.settle(node["aliases"]["local:"+label])
                result = next(r["artifact_ref"] for r in manifest.read_manifest(s.conn,settled["job_id"])
                              ["completion"]["outputs"] if r["output_name"] == "candidates.json")
                results.append(result)
                worker = s.next_actor(member["scope"])
            report = s.report(worker,results[0],status="PARTIAL",artifact_refs=results,
                              text="The two named indexes returned different outcomes.",
                              gaps=["One provider timed out and one listing has a continuation."])
            bundle = ArtifactService(s.conn).read_json(report["report_ref"])
            by_query = {r["query"]: r for r in bundle["query_records"]}
            first, follow = by_query[query], by_query[query2]
            self.assertEqual((first["source_ref"],follow["source_ref"]),tuple(results[:2]))
            self.assertEqual(first["source_mode"],"snapshot_replay")
            zero, missing = first["sources"]
            self.assertEqual((zero["provider"], zero["returned"], zero["has_more"],zero["coverage"]),
                             ("europepmc",0,False,"exhausted"))
            self.assertEqual((missing["status"], missing["has_more"], missing["error"]),
                             ("unavailable",None,"controlled timeout"))
            self.assertEqual((follow["sources"][1]["has_more"],follow["sources"][1]["next_cursor"]),(True,"page_two"))
            exact = by_query[exact_query]
            self.assertTrue(all(w["status"] == "empty" and w["has_more"] is False and w["coverage"] == "exhausted"
                                for w in exact["sources"]))
            lead = s.next_actor("run")
            entry = s.brief(lead)["module_report_index"][0]
            self.assertEqual(entry["gx_facts"]["query_records"],bundle["query_records"])
            self.assertEqual(entry["gx_facts"]["status"], "PARTIAL")
            self.assertEqual(entry["gx_facts"]["commission"]["expected_delivery"], args["expected_delivery"])
            for i, ref in enumerate(results):
                s.call(lead,"gx_link_core_material", {"ref":ref, "question":"goal",
                    "reason":"Unresolved coverage can change the catalog comparison."}, "window-"+str(i))
                s.call(lead,"gx_read_artifact",{"ref":ref},"inspect-"+str(i))
            response = s.call(lead,"gx_decide_revision",insufficient(),"close-partial-windows")
            pending = [item for item in response["materials"] if item["state"] == "query_incomplete"]
            self.assertEqual({item["ref"] for item in pending},set(results[:2]))
            self.assertNotIn(results[2],{item["ref"] for item in response["materials"]})
            facts = [source for item in pending for source in item["query_record"]["sources"]]
            self.assertTrue(any(w.get("error") == "controlled timeout" for w in facts))
            self.assertTrue(any(w.get("next_cursor") == "page_two" for w in facts))
            final = s.call(lead,"gx_decide_revision",insufficient(),"close-with-limitations")
            self.assertIn("decision_ref",final)
            save_evidence("scoped-query-jobs",{"run_id":s.run_id,"module":member["module_instance_id"],
                "source_mode":"controlled snapshots, network disabled, actual GX replay Jobs",
                "author":report["author"],"report_ref":report["report_ref"],"readings":bundle["query_records"],
                "lead_task":lead.task_id,"close_advisory":response,"effective_close":final["decision_ref"]})

    def test_gx_partial_preserves_named_job_without_signing_a_researcher_conclusion(self):
        plugin = PRODUCT/"gx-h/plugins/installed"
        api = runpy.run_path(str(plugin/"org.gx.scholarly-sources/1.0.0/methods/scholarly.py"))
        pages = runpy.run_path(str(plugin/"org.gx.scholarly-pages/1.1.0/methods/windows.py"))
        query = "calibration"
        def respond(url):
            return b'{"hitCount":0,"resultList":{}}' if "ebi.ac.uk" in url else b'{"message":{"items":[],"total-results":0}}'
        _, snapshot = pages["discover"](api,{"schema":api["REQUEST"]},query,"first",respond)
        file = self.work/"archived.json"
        file.write_text(json.dumps(snapshot))
        method = "scholarly-page-search@2"
        config = {"capabilities":[method],"research_modules":["scholarly_page_investigation@2"],
                  "plugins":["org.gx.scholarly-pages@1.1.0"],"input_files":[{"role":"source","path":str(file)}]}
        with Study(self.work/"bounded",spec_updates=config) as s:
            args = s.open_args("bounded",caps=[method],inputs=s.materials,limits={"worker_tasks":1,"api_calls":1})
            args.update(module_id="scholarly_page_investigation",module_version="2",focus="literature",
                        expected_delivery="Find and independently interpret the available calibration evidence.")
            member = s.call(s.lead,"gx_open_module",args,"commission")
            worker = s.bind(member["task_id"])
            s.call(s.lead,"gx_decide_revision",{"decision":draft()},"wait")
            node = s.submit_node(worker,"lookup",s.materials[0],capability=method,
                                 parameters={"query":query,"action":"replay"})
            settled = s.settle(node["aliases"]["local:lookup"])
            hold = s.conn.execute("SELECT reservation_ref FROM p0_root_deliveries WHERE thread_id=? AND turn_id=?",
                                  (worker.thread,worker.turn)).fetchone()[0]
            s.roots.delivery.observe("turn/completed", {"threadId":worker.thread,"turn":{"id":worker.turn,"status":"completed"}})
            with store.transaction(s.conn,transaction_owner="HarnessAcceptance",operation="settle_controlled_turn",run_id=s.run_id):
                metering.confirm(s.conn,hold,10)
            s.advance()
            row = s.rows("SELECT report_ref FROM v62_module_reports WHERE module_instance_id=?",member["module_instance_id"])
            ref = row[0]["report_ref"]
            bundle = ArtifactService(s.conn).read_json(ref)
            self.assertEqual((bundle["author"]["kind"],bundle["status"]),("GX","PARTIAL"))
            self.assertTrue(bundle["gaps"])
            self.assertFalse(bundle["researcher_statement"])
            self.assertEqual(bundle["findings"],[])
            self.assertEqual(bundle["query_records"][0]["job_id"],settled["job_id"])
            self.assertIn(bundle["query_records"][0]["source_ref"],bundle["artifact_refs"])
            lead = s.next_actor("run")
            evidence = completed_claim(member["module_instance_id"],ref)
            with self.assertRaisesRegex(SchemaInvalid, "complete|COMPLETE"):
                s.call(lead,"gx_stage_claim",evidence,"cannot-claim-complete")
            save_evidence("gx-partial",{"run_id":s.run_id,"module":member["module_instance_id"],
                "report_ref":ref,"author":bundle["author"],"status":bundle["status"],
                "gaps":bundle["gaps"],"named_query":bundle["query_records"],"findings":bundle["findings"]})

            # A researcher can independently interpret the actual zero-row raw
            # result while citing its archived query, even though the first seat timed out.
            second_args = s.open_args("signed",caps=[method],inputs=s.materials,round=2)
            second_args.update(module_id="scholarly_page_investigation",module_version="2",focus="literature",
                               expected_delivery="Return the archive lookup and interpret its scope.")
            second = s.call(lead,"gx_open_module",second_args,"second-seat")
            s.call(lead,"gx_watch_join",{"key":"catalog","question":"What did the archive return?","mode":"all",
                "ports":[{"slot":"rows","required":True,"alternatives":[
                    {"module_instance_id":second["module_instance_id"],"output_name":"candidates.json"}]}]}, "typed-delivery")
            worker = s.bind(second["task_id"])
            s.call(lead,"gx_decide_revision",{"decision":draft()},"wait-signed")
            node = s.submit_node(worker,"check",s.materials[0],capability=method,parameters={"query":query,"action":"replay"})
            result = s.settle(node["aliases"]["local:check"])
            named = next(o["artifact_ref"] for o in manifest.read_manifest(s.conn,result["job_id"])["completion"]["outputs"]
                         if o["output_name"] == "candidates.json")
            worker = s.next_actor(second["scope"])
            read = s.call(worker,"gx_read_artifact",{"ref":named},"inspect-counts")
            self.assertEqual([w["returned"] for w in read["sources"]],[0,0])
            signed = s.report(worker,named,text="The recorded catalogue windows returned no records for this query.")
            lead = s.next_actor("run")
            claim = completed_claim(second["module_instance_id"],signed["report_ref"])
            claim["evidence_refs"].append(named)
            claim["evidence_check"]["output_ref"] = named
            accepted = s.call(lead,"gx_stage_claim",claim,"confirm-real-port")
            proof = ArtifactService(s.conn).read_json(accepted["claim_ref"])["evidence_grade_receipt"]
            self.assertEqual((proof["grade"],proof["scope"]),("delivery_completed","required_ports"))
            self.assertEqual(proof["satisfied_ports"][0]["artifact_ref"],named)
            save_evidence("signed-negative",{"run_id":s.run_id,"module":second["module_instance_id"],
                "report_ref":signed["report_ref"],"author":signed["author"],"factual_delivery_grade":proof,
                "interpretation":ArtifactService(s.conn).read_json(signed["report_ref"])["researcher_statement"]})

    def test_private_nomination_stays_in_original_worker_callback(self):
        from acceptance.harness_refactor.modules.research_study import ResearchStudy
        from acceptance.harness_refactor.late_material.test_directed_delivery import researcher, route
        from gx_core.p0 import v62_modules
        config = {"research_effort":"medium","research_modules":["ad_hoc@1"],"capabilities":[]}
        with ResearchStudy(self.work,spec_updates=config) as s:
            seat,_ = researcher(s,"archivist")
            _,peer = researcher(s,"independent")
            private,_ = route(s,seat["scope"],"sealed",{"name":"field.txt","text":"private location W7"})
            v62_modules.advance(s.conn,s.run_id)
            worker = s.next_actor(seat["scope"])
            s.call(worker,"gx_link_core_material",{"ref":private,"reason":"Confidential verification location."},"nominate")
            s.call(s.lead,"gx_decide_revision",{"decision":draft()},"wait")
            reply = s.call(worker,"gx_submit_module_report",PARTIAL,"partial")
            self.assertEqual([x["ref"] for x in reply["delivery_advisory"]["materials"]],[private])
            raw = ArtifactService(s.conn).read_json(reply["report_ref"])
            lead = s.next_actor("run")
            peer_context = s.call(peer,"gx_task_context",{},"check-audience")
            for audience in (raw,s.brief(lead),peer_context,s.rows("SELECT payload_json FROM events WHERE kind IN "
                     "('v62.module.reported','v62.module.wake','research.close.module_advisory')")):
                text = json.dumps(audience)
                self.assertNotIn(private,text)
                self.assertNotIn("Confidential verification location.",text)

    @unittest.skipUnless(os.getenv("GX_PREVIOUS_HARNESS_PRODUCT"), "point to a genuine previous release")
    def test_previous_release_run_keeps_its_original_callback_and_grade(self):
        previous = Path(os.environ["GX_PREVIOUS_HARNESS_PRODUCT"]).resolve()
        self.assertNotEqual(previous, PRODUCT)
        install = self.work/"install"
        state = self.work/"study"
        def deploy(product):
            # Stage just the release and Python fixtures in a private directory.
            # The actual release root recorded in the DB is stable through upgrade.
            for folder in ("gx-h", "bin"):
                target = install/folder
                if target.exists():
                    shutil.rmtree(target)
                shutil.copytree(product/folder,target,ignore=shutil.ignore_patterns("__pycache__", "var"))
            for file in (product/"acceptance").rglob("*.py"):
                target = install/file.relative_to(product)
                target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(file,target)
        paths = [install, install/"gx-h", install/"gx-h/tests", install/"acceptance/v62a"]
        env = {**os.environ,"PYTHONPATH":os.pathsep.join(map(str,paths))}
        deploy(previous)
        script = ("import json,sys; from pathlib import Path; "
                  "from acceptance.harness_refactor.closing.test_closeout import Study; "
                  "s=Study(Path(sys.argv[1]),spec_updates=json.loads(sys.argv[2])); "
                  "s.directory.mkdir(); s._start_goal()")
        subprocess.run([sys.executable, "-B", "-c",script,str(state),json.dumps(CONFIG)],check=True,
                       cwd=install,env=env)
        deploy(PRODUCT)
        subprocess.run([sys.executable,"-B",__file__,"--legacy-callback",str(state)],check=True,cwd=install,env=env)


def check_legacy(state):
    verify = unittest.TestCase()
    with Study(state, reopen=True) as s:
        lead = s.next_actor("run")
        verify.assertEqual(s.brief(lead)["harness_policy"],"gx.harness/2")
        member = s.open("old",caps=[],inputs=s.materials)
        worker = s.bind(member["task_id"])
        s.call(lead,"gx_decide_revision",{"decision":draft()},"wait")
        report = s.call(worker,"gx_submit_module_report",PARTIAL,"old-report")
        verify.assertNotIn("delivery_advisory",report)
        verify.assertNotIn("commission", ArtifactService(s.conn).read_json(report["report_ref"]))
        lead = s.next_actor("run")
        with verify.assertRaisesRegex(SchemaInvalid, "not one of"):
            s.call(lead, "gx_stage_claim", completed_claim(member["module_instance_id"],report["report_ref"]), "old-grade")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--legacy-callback"]:
        check_legacy(Path(sys.argv[2]))
    else:
        unittest.main()
