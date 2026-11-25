"""D1/D2. Projection, authority, and host-confirmed delivery of named scientific outputs."""
import json
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from acceptance.v622 import test_reports as previous
from acceptance.v622.test_close import CloseStudy
from acceptance.v62a.interleavings_fixture import draft
from gx_core.domain import ids
from gx_core.p0 import v6_native, v623_endpoints, v623_exposure


class EndpointDelivery(unittest.TestCase):
    measure = previous.ReportEvidence.measured_output
    delivered = previous.ReportEvidence.delivered_report

    def work(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_report_with_unquantified_note_yields_no_reading(self):
        with CloseStudy(self.work()/"study") as s:
            src=s.materials[0]
            module=s.open("qualitative",inputs=[src], caps=[])
            worker=s.next_actor(module["scope"])
            s.call(worker,"gx_read_artifact",{"ref":src},"inspect")
            s.call(s.lead,v6_native.DECIDE,{"decision":draft()},"wait")
            receipt=s.report(worker,src,text="九成。 Estimated ninety percent.")
            _,_,delivered=self.delivered(s,receipt)
            self.assertEqual(delivered["endpoint_total"],0)
            self.assertFalse(delivered["pending_endpoints"])
            self.assertEqual(delivered["qualitative_notes"]["status"],"UNVERIFIED")
            self.assertEqual(delivered["findings"][0]["evidence_status"],"UNVERIFIED")
            self.assertEqual(v623_endpoints.registered(s.conn,s.run_id,receipt["report_ref"]),[])

    def test_read_content_is_confirmed_once_after_cold_attach(self):
        path=self.work()
        with patch.object(previous,"Scenario",CloseStudy):
            s,assets,_,node,output,worker=self.measure(path, module_policy={"report_bytes":512})
            receipt=s.report(worker,output,text="Only an estimate: 90%, 九成／ninety percent.")
            ref=receipt["report_ref"]
            actor,_,projected=self.delivered(s,receipt)
            key=projected["endpoint_rows"][0]["key"]
            self.assertLessEqual(v623_endpoints.size(projected),512)
            self.assertNotIn("ninety percent",json.dumps(projected))
            args={"ref":ref,"endpoint_offset":0}
            reading=s.call(actor,"gx_read_artifact",args,"read-result")
            metric,=reading["verified_metrics"]
            before=s.rows("SELECT event_id FROM events WHERE kind=? AND json_extract(payload_json,'$.delivery_kind')='artifact_read'",
                          v623_exposure.EVENT)
            self.assertEqual(before,[])
            self.assertEqual(v623_exposure.pending_reads(s.conn,s.run_id,actor.thread),{actor.turn})
            rid=s.run_id
            s.__exit__(None,None,None)
        with CloseStudy(path/"run",reopen=True) as s:
            item=dict(type="dynamicToolCall", tool="gx_read_artifact", id="read-result",arguments=args,
                      status="completed",success=True,contentItems=[{"type":"inputText","text":ids.canonical_json(reading)}])
            def recover_rpc(method,request):
                self.assertEqual(request["threadId"],actor.thread)
                if method=="thread/turns/list":
                    return {"data":[{"id":actor.turn,"status":"inProgress"}]}
                self.assertEqual(method,"thread/items/list")
                self.assertEqual(request["turnId"],actor.turn)
                return {"data":[{"turnId":actor.turn,"item":item}]}
            with patch.object(s.roots,"_request",side_effect=recover_rpc):
                s.roots.delivery.reconcile(actor.thread)
                s.roots.delivery.reconcile(actor.thread)
            event,=s.rows("SELECT event_id,payload_json FROM events WHERE kind=? AND json_extract(payload_json,'$.delivery_kind')='artifact_read'",
                           v623_exposure.EVENT)
            self.assertEqual(v623_exposure.pending_reads(s.conn,rid,actor.thread),set())
            received=v623_exposure.received(s.conn,run_id=rid,task_id=actor.task_id,lease_id=actor.lease_id,
                                            thread_id=actor.thread,turn_id=actor.turn)
            self.assertEqual(received[metric["metric_ref"]]["event_id"],event["event_id"])
            self.assertEqual(received[metric["metric_ref"]]["source_ref"],ref)
            self.assertTrue(metric["metric_ref"].removeprefix("metric_").startswith(key))
            self.assertEqual(len(s.rows("SELECT job_id FROM jobs")),1)

    def test_decision_endpoint_priority_resolvable_aliases_and_long_row(self):
        methods=[{"method":dict(capability="method@1",metric_id="reading",unit="mV",output_name="science.json",
                                  parameters={"mask":"selected"})}]
        def metric(name,value,ref,mask):
            return {"metric_ref":"metric_"+ref,"metric_id":name,"method":"method@1","display":value,"unit":"mV",
                    "output_name":"science.json","scope":{"mask":mask}}
        ordinary=metric("context","metadata", "abcdef1234569"+"f"*51,"other")
        primary=metric("reading","49 mV", "abcdef1234568"+"f"*51,"selected")
        long=metric("annotation","註"*100,"125"*21+"a","other")
        sorted_metrics=v623_endpoints.priority([ordinary,primary,long],methods)
        self.assertEqual(sorted_metrics[0],primary)
        a,b,c=v623_endpoints.compact_rows(sorted_metrics)
        self.assertNotEqual(a["key"],b["key"])
        self.assertLessEqual(v623_endpoints.size(a),80)
        packed={}
        v623_endpoints.fit(packed,[c,a,b],3,lambda:v623_endpoints.size(packed)<=190)
        self.assertIn(a,packed["endpoint_rows"])
        self.assertTrue(packed["pending_endpoints"])
        self.assertLessEqual(v623_endpoints.size(packed),190)
