"""Exercise the official Root callback shape and real detached Job/Artifact owners."""
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.hostobs.app_server_client import ReceivedMessage
from gx_core.jobs.service import JobService
from gx_core.p0 import compute, manifest, waits
from gx_core.persistence import store
from tests import test_p0_roots


class ComputeTest(unittest.TestCase):
    def setUp(self):
        f = self.fixture = test_p0_roots.DurableRoots()
        f.setUp()
        self.addCleanup(f.doCleanups)
        self.db, self.run, self.task, self.roots = f.conn, f.run, f.task, f.roots
        budget = budgets.create_root(self.db,run_id=None,profile_name="p0",actor="test")
        self.db.execute("UPDATE runs SET budget_ref=? WHERE run_id=?", (budget,self.run))
        self.assets = ArtifactService(self.db)
        self.refs = {n: self.publish(n, v) for n,v in {"left":[1,2,3,4],"right":[10,20],"long":"x"*34000}.items()}
        self.unassigned = self.publish("unassigned", [9])
        self.db.execute("UPDATE tasks SET input_json=? WHERE task_id=?",(json.dumps(self.refs),self.task))
        self.roots._request = f.fake
        self.row = f.bind()
        self.i = 0
        self.observations = {}

    def publish(self, name, value):
        return self.assets.publish_bytes(namespace=self.run+"/inputs",run_id=self.run,kind="measurements",
            media_type="application/json",rel_path=name+".json",data=json.dumps(value)).artifact_ref

    def invoke(self, tool, args, ok=True, *, thread=None):
        self.i += 1
        frame = ReceivedMessage(1, {"method":"item/tool/call","params":{"threadId":thread or self.row["thread_id"],
               "turnId":"turn","callId":str(self.i),"tool":tool,"arguments":args}})
        with patch.object(self.roots,"_request",side_effect=AssertionError("nested callback RPC")):
            ans = self.roots._callback(frame)
        value = json.loads(ans["contentItems"][0]["text"])
        self.assertEqual(ans["success"], ok, value)
        if not ok:
            self.observations.setdefault("refusals", []).append({"tool":tool,"error":value["error"]})
        return value

    def proposal(self, key, **change):
        return {"capability_version":"artifact-analysis@1", "idempotency_key":key,
                "inputs":{k:self.refs[k] for k in ("left","right")}, "parameters":{"offset":2},
                "script":"{'gap': mean(data['right']) - mean(data['left']) + params['offset']}",
                "output_schema":{"type":"object","properties":{"gap":{"type":"number"}},
                                 "required":["gap"],"additionalProperties":False}, **change}

    def finish(self, job):
        compute.drain(self.db,self.run)
        return JobService(self.db).wait(job, 10)

    def test_original_inputs_result_and_wait_survive_cold_admission(self):
        listed = self.invoke("gx_capability_inspect",{"capability_id":"artifact-analysis"})
        self.assertEqual(listed["release_methods"][0]["capability_version"],"artifact-analysis@1")
        req=self.proposal("comparison", tighten={"wall_seconds":8,"memory_mib":96})
        jid=self.invoke("gx_job_submit",req)["job_id"]
        self.assertEqual(self.invoke("gx_job_submit",req)["job_id"],jid)
        self.assertTrue(self.invoke("gx_wait_job",{"job_id":jid})["waiting"])
        self.assertTrue(waits.deferred_turn(self.db,self.row["root_key"],"turn"))
        self.assertTrue(waits.is_waiting(self.db,self.row["root_key"]))
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],0)
        reopened = store.open_store(Path(self.fixture.tmp.name)/"test.db")
        self.addCleanup(reopened.close)
        compute.drain(reopened,self.run)
        compute.drain(reopened,self.run)
        self.assertEqual(JobService(reopened).wait(jid,10)["status"],"SUCCEEDED")
        view=self.invoke("gx_job_status",{"job_id":jid})
        result=next(o for o in view["outputs"] if o["kind"]=="artifact_analysis_result")
        self.assertEqual(self.invoke("gx_read_artifact",{"ref":result["artifact_ref"]}),{"gap":14.5})
        doc=manifest.read_manifest(reopened,jid)
        self.assertEqual({i["artifact_ref"] for i in doc["admission"]["inputs"] if i["role"]!="spec"},
                         {self.refs["left"],self.refs["right"]})
        self.assertEqual(doc["admission"]["attempt_id"],self.row["root_key"])
        self.assertEqual(doc["completion"]["outputs"][0]["sha256"],result["sha256"])
        stdout=next(i for i in doc["completion"]["logs"] if i["kind"]=="job_stdout")
        proof=json.loads(self.assets.read_bytes(stdout["artifact_ref"]))
        self.assertEqual((proof["socket_probe"],proof["open_probe"],proof["cpu_seconds"]),("EPERM","EPERM",8))
        self.assertEqual((proof["hard_fsize_bytes"],proof["hard_address_space_bytes"]),(0,96<<20))
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0],1)
        self.observations.update(job_id=jid,result_ref=result["artifact_ref"],result_sha256=result["sha256"],
                                 runtime=proof,manifest_sha256=doc["manifest_hash"],result={"gap":14.5})

    def test_unavailable_programs_paths_inputs_are_rejected_before_execution(self):
        for expr in ("{'v': open('/dev/null')}", "{'v': socket()}", "{'v': data.keys()}",
                     "{'v': [x for x in data]}", "import os"):
            self.assertIn("refused",self.invoke("gx_job_submit",self.proposal(expr,script=expr),False)["error"])
        for data in ({"left":self.unassigned},{"../outside":self.refs["left"]},{"data":"/dev/null"},{"data":"https://example.invalid/measurements"}):
            self.invoke("gx_job_submit",self.proposal("scope",inputs=data),False)
        self.invoke("gx_job_submit",self.proposal("child"),False,thread="unbound-child")
        self.assertEqual(self.db.execute("SELECT count(*) FROM p0_job_proposals").fetchone()[0],0)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0],0)

    def test_new_attempt_adopts_exact_operation_and_keeps_original_manifest(self):
        req = self.proposal("durable-comparison")
        jid = self.invoke("gx_job_submit",req)["job_id"]
        self.assertEqual(self.finish(jid)["status"],"SUCCEEDED")
        original = manifest.read_manifest(self.db,jid)
        old = self.row
        with store.transaction(self.db):
            store.release_lease(self.db,old["lease_id"])
            store.transition(self.db,machine="task",subject_id=self.task,to_state="READY",actor="test",
                             extra_updates={"owner":None,"lease_id":None})
        self.row = self.fixture.bind()
        completed = self.invoke("gx_job_submit",req)
        self.assertEqual(completed,{"job_id":jid,"status":"SUCCEEDED"})
        self.invoke("gx_job_submit",{**req,"parameters":{"offset":7}},False)
        self.assertFalse(self.invoke("gx_wait_job",{"job_id":jid})["waiting"])
        view = self.invoke("gx_job_status",{"job_id":jid})
        self.assertTrue(any(output["kind"] == "artifact_analysis_result" for output in view["outputs"]))
        self.invoke("gx_job_status",{"job_id":jid},False,thread=old["thread_id"])
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0],1)
        self.assertEqual(manifest.read_manifest(self.db,jid),original)
        self.observations.update(job_id=jid,original_attempt=old["root_key"],new_attempt=self.row["root_key"],
                                 original_manifest=original["manifest_hash"])

    def test_schema_encoded_output_and_resource_refusal(self):
        too_large=self.proposal("too_large",inputs={"text":self.refs["long"]},script="{'gap': data['text']}")
        wrong_type={**too_large,"idempotency_key":"wrong-type"}
        too_large["output_schema"]["properties"]["gap"]={"type":"string"}
        wrong_type["output_schema"]={"type":"object","properties":{"gap":{"type":"integer"}},
                                     "required":["gap"],"additionalProperties":False}
        for params, word in ((too_large,"32768"),(wrong_type,"output_schema")):
            jid=self.invoke("gx_job_submit",params)["job_id"]
            result=self.finish(jid)
            self.assertEqual(result["status"],"FAILED")
            doc=manifest.read_manifest(self.db,jid)["completion"]
            self.assertFalse(doc["outputs"])
            stderr=next(i for i in doc["logs"] if i["kind"]=="job_stderr")
            message=self.assets.read_bytes(stderr["artifact_ref"]).decode().strip()
            self.assertIn(word,message)
            self.observations.setdefault("failed_jobs", []).append({"job_id":jid,"status":result["status"],
                                         "result_count":len(doc["outputs"]),"diagnostic":message})
        jid=self.invoke("gx_job_submit",self.proposal("overbudget",tighten={"wall_seconds":11}))["job_id"]
        self.invoke("gx_wait_job",{"job_id":jid})
        compute.drain(self.db,self.run)
        refused=self.invoke("gx_job_status",{"job_id":jid})
        self.assertEqual((refused["status"],refused["failure"]["code"]),("REJECTED","SCHEMA_INVALID"))
        notices=self.db.execute("SELECT payload_json FROM outbox WHERE topic='p0.compute.notice'").fetchall()
        self.assertEqual(len(notices),1)
        with patch.object(self.roots,"deliver",return_value={"turn_id":"wake"}) as sent:
            compute.deliver_notice(self.roots,json.loads(notices[0][0]))
            compute.deliver_notice(self.roots,json.loads(notices[0][0]))
        self.assertEqual(sent.call_count,1)
        self.observations.update(refused=refused,notice_count=sent.call_count)
        self.assertIsNone(compute.pending_wait(self.db,self.row["root_key"]))

    def test_existing_job_limits_stop_bounded_compute_load(self):
        raw = self.publish("bounded-input", [1] * 310000)
        self.db.execute("INSERT INTO p0_root_artifact_reads VALUES(?,?)", (self.row["root_key"],raw))
        terms = ["sum(data['wide'])"] * 28
        request = self.proposal("limited-cpu",inputs={"wide":raw},script="{'gap': sum(["+",".join(terms)+"])}",
                                tighten={"wall_seconds":1})
        jid = self.invoke("gx_job_submit",request)["job_id"]
        view = self.finish(jid)
        self.assertEqual(view["status"],"FAILED")
        self.assertFalse(manifest.read_manifest(self.db,jid)["completion"]["outputs"])
        self.assertTrue(view["failure"].get("timed_out") or view["failure"]["observed"] == "signal")
        self.observations["wall_limit"] = {"job_id":jid, "failure":view["failure"], "measured":view["measured"]}
        small = self.proposal("limited-memory",tighten={"memory_mib":1})
        jid = self.invoke("gx_job_submit",small)["job_id"]
        view=self.finish(jid)
        self.assertEqual(view["status"],"FAILED")
        self.assertFalse(manifest.read_manifest(self.db,jid)["completion"]["outputs"])
        self.assertIn("rss_limit",view["failure"]["summary"])
        self.observations["memory_limit"] = {"job_id":jid, "failure":view["failure"], "measured":view["measured"]}

    def test_cancel_and_fence_before_scheduling(self):
        jid=self.invoke("gx_job_submit",self.proposal("cancel"))["job_id"]
        self.assertEqual(self.invoke("gx_job_cancel",{"job_id":jid,"reason":"different sampling plan",
                         "idempotency_key":"cancel"})["status"],"CANCELLED")
        compute.drain(self.db,self.run)
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0],0)
        self.invoke("gx_job_status",{"job_id":jid},False,thread="unbound-child")
        target=self.invoke("gx_job_submit",self.proposal("should-not-run"))["job_id"]
        with store.transaction(self.db):
            store.release_lease(self.db,self.row["lease_id"])
        compute.drain(self.db,self.run)
        rejected=json.loads(self.db.execute("SELECT error_json FROM p0_job_proposals WHERE job_id=?",(target,)).fetchone()[0])
        self.assertEqual(rejected["code"],"LEASE_EXPIRED")
        self.observations.update(cancelled=jid,revoked_job=target,revocation_reason=rejected)
        self.assertEqual(self.db.execute("SELECT state FROM p0_job_proposals WHERE job_id=?", (target,)).fetchone()[0],"REJECTED")
        self.assertEqual(self.db.execute("SELECT count(*) FROM jobs").fetchone()[0],0)
