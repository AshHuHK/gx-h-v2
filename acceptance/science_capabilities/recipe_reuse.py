"""Replay the controlled Goal callback, saved recipe and detached CPU Jobs on two original PDB deposits."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "gx-h"))

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.native_release import native_state
from gx_core.p0 import cli, conversation, conversation_delivery, manifest, v6_goal, v6_jobs, v623_exposure
from gx_core.p0.roots import Roots
from gx_core.persistence import store

CAP = "deposited-backbone-qc@1"
METHOD = "org.gx.deposited-structure-qc@1.0.0"
MATERIAL = Path(__file__).parent / "structure_controls/materials"


def lead(conn, run, workspace):
    task = conn.execute("SELECT task_id FROM tasks WHERE run_id=? AND kind='decide_revision' AND status='READY'",
                        (run,)).fetchone()[0]
    owner = Roots(conn, run_id=run, cwd=workspace)
    owner.client = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=os.getpid())))
    row = owner._claim(task)
    thread = owner._bind(row, {"id": "controlled-" + task, "cwd": row["cwd"], "source": "appServer"})
    turn = "controlled-turn-" + task
    owner.active_turns[thread] = turn
    conn.execute("INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,human,interrupt,"
                 "state,method,turn_id) VALUES(?,?,?,?,?,0,0,'acknowledged','turn/start',?)",
                 (run, turn, thread, "start-" + task, hashlib.sha256(row["brief"].encode()).hexdigest(), turn))

    delivery = conn.execute("SELECT * FROM p0_root_deliveries WHERE thread_id=? AND turn_id=?",
                            (thread, turn)).fetchone()
    with store.transaction(conn, transaction_owner="ControlledGoal", operation="ack_brief", run_id=run):
        v623_exposure.brief_ack(conn, owner._binding(thread), delivery)

    def call(tool, args):
        request = {"threadId": thread, "turnId": turn, "tool": tool,
                   "callId": ids.new_id("request"), "arguments": args}
        if tool in owner.mode.decision_tools:
            request["namespace"] = "gx"
        reply = owner._call(request)
        if tool == "gx_read_artifact":
            owner.delivery._arrived(thread, turn, {"type": "dynamicToolCall", "tool": tool,
                "id": request["callId"], "arguments": args, "status": "completed", "success": True,
                "contentItems": [{"type": "inputText", "text": ids.canonical_json(reply)}]})
        return reply
    return call


def run_pair(workspace):
    results = []
    with native_state(workspace), closing(store.open_store(workspace / "gx.db")) as conn:
        for entry, question in (("1QYS", "Which declared Top7 positions can support a backbone comparison?"),
                                ("6BEN", "How complete is the model-1 backbone of this deposited cyclic peptide?")):
            files = [{"role": "structure", "path": str(MATERIAL / (entry + ".cif"))}]
            if not results:
                run = cli.start(conn, workspace, {"mode": "goal", "project": "deposited-backbone-coverage",
                    "objective": question, "research_effort": "deep", "research_modules": [],
                    "capabilities": [CAP], "plugins": [METHOD], "input_files": files},
                    run_key="recipe-" + entry)["run_id"]
            else:
                parent = results[0]["run_id"]
                message = ids.new_id("request")
                conversation.send(conn, parent, question, message, successor={"input_files": files})
                options = json.loads(conn.execute("SELECT successor_json FROM p0_conversation_messages "
                                                  "WHERE message_id=?", (message,)).fetchone()[0])
                run = conversation_delivery.start_successor(conn, workspace, parent, message, question,
                                                            options, native_owner=True)["run_id"]
            source = json.loads(conn.execute("SELECT materials_json FROM v6_goals WHERE run_id=?", (run,)).fetchone()[0])[0]
            ref = source["artifact_ref"] if isinstance(source, dict) else source
            call = lead(conn, run, workspace)
            branch = {"op": "open_branch", "branch": "local:qc", "hypothesis": "All declared positions are modeled",
                      "method_family": "deposited backbone occupancy", "alternative": "Incomplete model coverage",
                      "discriminating_observation": "Count positions with four modeled backbone atoms",
                      "stop_condition": "Record the model-1 complete backbone numerator and declared denominator",
                      "budget_limits": {"job_submissions": 1, "cpu_seconds": 30}}
            if not results:
                jobs = [{"op": "add_node", "node": "local:qc-backbone", "branch": "local:qc",
                         "capability": CAP, "inputs": [ref], "question": "Which declared positions have a complete backbone?"}]
            else:
                lookup = call("gx_query_catalog", {"kind": "recipe", "reference": saved["reference"]})
                if saved["digest"] not in json.dumps(lookup):
                    raise AssertionError("The discovered recipe did not include its pinned digest")
                expansion = call("gx_instantiate_recipe", {"recipe": saved["reference"], "digest": saved["digest"],
                                 "prefix": "qc", "branch": "local:qc", "inputs": {"structure": ref}})
                jobs = expansion["ops"]
            decision = call("gx_decide_revision", {"decision": {"ops": [branch, *jobs],
                "rationale": "Count declared polymer positions and complete N/CA/C/O backbones before comparison."}})
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                v6_jobs.advance(conn, run)
                node = conn.execute("SELECT * FROM v6_nodes WHERE run_id=?", (run,)).fetchone()
                if node and node["status"] in v6_jobs.NODE_TERMINAL:
                    break
                time.sleep(.1)
            if not node or node["status"] != "SUCCEEDED":
                raise RuntimeError("The detached CPU measurement failed to settle")
            proof = manifest.read_manifest(conn, node["job_id"])
            output, = proof["completion"]["outputs"]
            measured = json.loads(ArtifactService(conn).read_bytes(output["artifact_ref"]))
            v6_goal.ensure_task(conn, run)
            follow = lead(conn, run, workspace)
            if not results:
                saved = follow("gx_save_recipe", {"recipe_id": "deposited-backbone-coverage", "version": "1",
                    "title": "Model-1 complete versus declared backbone positions",
                    "nodes": [{"slot": "backbone", "node_id": node["node_id"]}],
                    "inputs": [{"name": "structure", "artifact_ref": ref}],
                    "id_alignment": "Join entity and label sequence number in a single deposited chain.",
                    "missing_values": "Include declared positions with no observed backbone in the denominator.",
                    "units": "Counts in residues; radius of gyration in angstroms."})
            reading = output["artifact_ref"]
            rows = []
            offset = 0
            while offset is not None:
                page = follow("gx_read_artifact", {"ref": reading, "endpoint_offset": offset})
                rows.extend(page["endpoint_rows"])
                offset = page["next_endpoint_offset"]
            metrics = [reading + "#" + row["key"] for row in rows
                       if row["id"] in ("complete_backbones", "declared_residues")]
            if len(metrics) != 2:
                raise AssertionError(f"Expected two delivered endpoint rows, saw {rows}")
            reason = ("Limit structural comparisons to the modeled residue range." if
                      measured["complete_backbones"] < measured["declared_residues"] else
                      "The entire declared chain is available for a backbone comparison in this deposited model.")
            closed = follow("gx_decide_revision", {"decision": {"rationale": reason,
                "evidence_refs": [reading], "metric_refs": metrics,
                "ops": [{"op": "close_run", "reason": reason, "claim_refs": [],
                         "unresolved": ["Completeness alone does not establish conformational accuracy."]}]}})
            v6_jobs.advance(conn, run)
            results.append({"run_id": run, "closed_by": closed["decision_id"], "interpretation": reason, "job_id": node["job_id"], "node_id": node["node_id"],
                            "decision_id": decision["decision_id"], "input": proof["admission"]["inputs"][0],
                            "output_ref": output["artifact_ref"], "output_sha256": output["sha256"],
                            "method": proof["admission"]["capability"], "measured": measured})
        return {"mode": "controlled native callbacks, real public mmCIF originals and detached CPU Jobs",
                "recipe": saved, "runs": results}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        directory = args.workspace or Path(tmp)
        directory.mkdir(exist_ok=True, parents=True)
        body = json.dumps(run_pair(directory), indent=2, ensure_ascii=False) + "\n"
        if args.receipt:
            args.receipt.write_text(body)
        else:
            print(body)
