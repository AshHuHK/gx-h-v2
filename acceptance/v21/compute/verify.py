"""Run the official 0.169.0 Root tools against a local Responses fixture and actual Jobs."""
import argparse
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

PACKAGE = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(PACKAGE/"acceptance"), str(PACKAGE/"gx-h")]
from acc1_driver import project_fixture
from e2e_responses import brief_from, fixture_action, output_json
from native_probe import Probe
from native_probe_peer import Peer
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.p0 import compute, manifest, schema
from gx_core.p0.profiles import CODEX_VERSION, clash_env
from gx_core.p0.roots import Roots
from gx_core.persistence import store


class AnalysisPeer(Peer):
    def __init__(self):
        super().__init__()
        self.answers, self.actions, self.values = {}, [], {}

    def record(self, body):
        row, n = super().record(body)
        calls = {i["call_id"]: i for i in body["input"] if i.get("type") == "function_call"}
        for output in body["input"]:
            command = calls.get(output.get("call_id"))
            if output.get("type") == "function_call_output" and command:
                self.values[command["call_id"]] = output_json(output["output"])
        def past(i):
            return self.values["call-" + self.rows[i-1]["id"]]
        inputs = brief_from(body)["input"]
        names = {k: inputs[k] for k in ("left","right")}
        schema = {"type":"object", "properties":{"difference":{"type":"number"}, "n":{"type":"integer"}},
                  "required":["difference","n"],"additionalProperties":False}
        if n == 1:
            name, args = "gx_capability_inspect", {"capability_id":"artifact-analysis"}
        elif n == 2:
            assert past(1)["release_methods"][0]["capability_version"] == "artifact-analysis@1"
            name,args = "gx_job_submit",{"capability_version":"fixture-grid@1","inputs":{"grid":inputs["grid"]},
                                         "parameters":{"operation":"measure"},"idempotency_key":"cancel-grid"}
        elif n == 3:
            name,args = "gx_job_cancel",{"job_id":past(2)["job_id"],"idempotency_key":"stop-grid",
                                         "reason":"The requested comparison uses the JSON measurement artifacts."}
        elif n == 4:
            name,args = "gx_job_submit", {"capability_version":"artifact-analysis@1","inputs":names,
                    "idempotency_key":"compare-means", "script":"{'difference': mean(data['right']) - mean(data['left']), 'n': count(data['right'])}",
                    "output_schema":schema,"tighten":{"wall_seconds":8,"memory_mib":96}}
        elif n == 5:
            name,args = "gx_wait_job",{"job_id":past(4)["job_id"]}
        elif n == 6:
            assert past(5)["waiting"] is True
            name,args = None,None
            result = self.message(row["id"], "Parked this attempt while GX runs the admitted comparison.")
        elif n == 7:
            name,args = "gx_job_status",{"job_id":past(4)["job_id"]}
        elif n == 8:
            result_ref=next(o["artifact_ref"] for o in past(7)["outputs"] if o["kind"]=="artifact_analysis_result")
            name,args = "gx_read_artifact",{"ref":result_ref}
        else:
            assert past(8) == {"difference":12.5,"n":2}
            result,facts=fixture_action(body,row["id"])
            name,args=facts.get("tool"),None
        if args is not None:
            result = {"type":"function_call","name":name,"arguments":json.dumps(args),"call_id":"call-"+row["id"]}
        self.answers[row["id"]]=result
        self.actions.append(name or "message")
        row["usage"]={"input_tokens":1,"output_tokens":1,"total_tokens":2}
        return row,n

    def item(self,row,n):
        return self.answers[row["id"]]


def until(root, predicate, tick=lambda: None):
    stop = time.monotonic() + 35
    while time.monotonic() < stop:
        root.poll(0.05)
        tick()
        if predicate():
            return
    raise RuntimeError("official Root exceeded its 35-second acceptance deadline")


def run():
    binary = Path(os.environ.get("GX_P0_CODEX") or
                  PACKAGE/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex").resolve()
    with tempfile.TemporaryDirectory(prefix="gx-official-analysis-") as td, ExitStack() as stack:
        base=Path(td)
        peer=stack.enter_context(AnalysisPeer())
        thread=threading.Thread(target=peer.serve_forever,daemon=True)
        thread.start()
        stack.callback(thread.join)
        stack.callback(peer.shutdown)
        probe=Probe(base/"codex",binary,peer)
        stack.enter_context(patch.dict(os.environ,{**probe.env,"GX_P0_TEST":"1","GX_P0_TEST_PROVIDER":probe.provider,
                                    "GX_P0_MODEL":"gpt-6.2-astra","GX_P0_CODEX":str(binary)}))
        clash_env()
        version=subprocess.check_output([binary,"--version"],text=True).strip()
        assert version == "codex-cli "+CODEX_VERSION
        state=stack.enter_context(native_state(base/"workspace"))
        conn=store.open_store(state.db_path)
        schema.install(conn)
        run_id,budget,tasks=project_fixture(conn,10,False)
        task=tasks[0]
        assets=ArtifactService(conn)
        data={"left":[1,2,3,4],"right":[10,20],"grid":{"synthetic":True,"method_id":"fixture-grid","method_version":"1",
            "candidates":[{"candidate_id":"peak","x":2}],"delay_seconds":5}}
        refs={name:assets.publish_bytes(namespace=run_id+"/data",run_id=run_id,rel_path=name+".json",
              kind="input_measurements",media_type="application/json",data=ids.canonical_json(value)).artifact_ref
              for name,value in data.items()}
        existing=json.loads(conn.execute("SELECT input_json FROM tasks WHERE task_id=?", (task,)).fetchone()[0])
        conn.execute("UPDATE tasks SET input_json=? WHERE task_id=?", (ids.canonical_json({**existing,**refs}),task))
        first=Roots(conn,run_id=run_id,cwd=probe.cwd,token_budget=40)
        with first:
            receipt=first.dispatch(task)
            def drain_cancel_only():
                if len(peer.actions) < 4:
                    compute.drain(conn,run_id)
            until(first, lambda: bool(conn.execute("SELECT 1 FROM p0_root_turn_usage WHERE turn_id=? AND terminal_status IS NOT NULL",
                                                   (receipt["turn_id"],)).fetchone()), drain_cancel_only)
        queue=conn.execute("SELECT job_id,state,wait_turn FROM p0_job_proposals WHERE operation_key='compare-means'").fetchone()
        assert queue["state"] == "PROPOSED" and queue["wait_turn"] == receipt["turn_id"]
        comparison=queue["job_id"]
        cancelled=conn.execute("SELECT job_id FROM p0_job_proposals WHERE operation_key='cancel-grid'").fetchone()[0]
        assert not conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (comparison,)).fetchone()
        conn.close()
        with closing(store.open_store(state.db_path)) as cold:
            compute.drain(cold,run_id)
            jobs=JobService(cold)
            assert jobs.wait(cancelled,12)["status"] == "CANCELLED"
            assert jobs.wait(comparison,12)["status"] == "SUCCEEDED"
            m=manifest.read_manifest(cold,comparison)
            with Roots(cold,run_id=run_id,cwd=probe.cwd,token_budget=40) as second:
                wake=second.wake_job(task,comparison)
                until(second, lambda: bool(cold.execute("SELECT 1 FROM p0_root_turn_usage WHERE turn_id=? AND terminal_status IS NOT NULL",
                                                       (wake["turn_id"],)).fetchone()))
            assert peer.actions.count("gx_job_status")==1 and peer.actions.count("gx_wait_job")==1
            out=next(o for o in m["completion"]["outputs"] if o["kind"]=="artifact_analysis_result")
            proof=next(o for o in m["completion"]["logs"] if o["kind"]=="job_stdout")
            artifacts=ArtifactService(cold)
            product=cold.execute("SELECT status FROM tasks WHERE task_id=?", (task,)).fetchone()[0]
            assert product == "ACCEPTED", product
            return {"official_binary":version,"transport":"stdio with loopback Responses fixture",
                    "tools_offered":sorted(n for n in peer.rows[0]["tool_names"] if n.startswith("gx_")),
                    "official_tool_sequence":peer.actions,"attempt":m["admission"]["attempt_id"],"task_status":product,
                    "status_queries":1,"parked_before_admission":dict(queue),"cold_wake_turn":wake["turn_id"],
                    "cancelled_job_id":cancelled,"cancelled_job_state":"CANCELLED","job_id":comparison,
                    "result_ref":out["artifact_ref"],"result_sha256":out["sha256"],
                    "result":artifacts.read_json(out["artifact_ref"]),"kernel_and_limits":artifacts.read_json(proof["artifact_ref"]),
                    "pinned_inputs":m["admission"]["inputs"],"capability":m["admission"]["capability"],
                    "reservation_ref":m["admission"]["budget_reservation_ref"],
                    "manifest_admission_sha256":m["admission_hash"],"manifest_completion_sha256":m["manifest_hash"],
                    "execution_manifest":m, "total_job_rows":cold.execute("SELECT count(*) FROM jobs WHERE run_id=?", (run_id,)).fetchone()[0]}


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args=parser.parse_args()
    evidence=run()
    if args.output:
        args.output.write_text(json.dumps(evidence,indent=2,sort_keys=True)+"\n")
    print(json.dumps({k:evidence[k] for k in ("official_binary","result","task_status","total_job_rows","kernel_and_limits")}))
