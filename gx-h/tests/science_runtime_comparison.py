"""W01 pilot. Real Codex, native_v2 workers and GX CPU Jobs with a scripted model.

Run from gx-h with python3 -Bm tests.science_runtime_comparison [--source-host PATH].
The model fixture uses verified GX readbacks to generate its tool arguments and
stages the same synthetic judgment as science_cycle. Every mutation is dispatched
by an actual Codex callback, except manifest preparation and the GX reactor.
"""
import argparse
from collections import Counter
from contextlib import ExitStack, closing
import hashlib
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

from examples.science_cycle import PROJECT, read_inputs, snapshot
from examples.science_judgment import judge
from gx_core.artifacts import ArtifactService
from gx_core.artifacts.delivery import prepare_manifest
from gx_core.context import Ctx, Principal
from gx_core.hostobs import native_app_server
from gx_core.hostobs.app_server_client import AppServerClient
from gx_core.native_release import native_state
from gx_core.orchestration import reactor
from gx_core.persistence import store
from tests import runtime_child_probe as protocol


def action(s, actor, index, ident, model):
    call = lambda name, args: protocol.tool(ident, name, args)
    if s.phase == "proposal":
        return call("gx_run_start", s.proposal) if index == 1 else None
    if actor == "child":
        if index == 1:
            return call("gx_memory_search", {"query": "Grid measurement.", "project_scope": PROJECT})
        if index == 2:
            if not s.assigned.wait(15):
                raise RuntimeError("worker assignment missing")
            return call("gx_task_acquire", {"task_id": s.task["task_id"]})
        if index == 3:
            return call("gx_task_finish", {"task_id": s.task["task_id"], "manifest_ref": s.manifest})
        s.done.set()
        return None
    if index <= len(s.steps):
        step = s.steps[index - 1]
        if step == "spawn":
            return protocol.tool(ident, "spawn_agent", {"task_name": protocol.CHILD,
                "message": s.task_input["instruction"], "fork_turns": "none",
                "model": model, "reasoning_effort": "ultra"}, "collaboration")
        if step == "create":
            payload = {"intent": {"objective": "Evaluate and refine grid predictions."}}
        elif step == "transition":
            payload = {"team_id": s.team, "status": "ACTIVE"}
        else:
            if not s.seen.wait(15):
                raise RuntimeError("no admitted child callback")
            payload = {"team_id": s.team, "subject": "native:" + s.child}
            if step == "member":
                payload["action"] = "add"
            else:
                payload["task_id"] = s.task["task_id"]
        return call("gx_team_manage", {"run_id": s.run_id, "coordinator_lease_ref": s.coordinator,
                "expected_revision": s.revision, "idempotency_key": ident, "action": step, "payload": payload})
    s.done.wait(15)
    return None


def run(source_host):
    native_app_server._proxy_preflight()
    started = time.monotonic()
    with ExitStack() as stack:
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="gx-science-pilot-")))
        s = ThreadingHTTPServer(("127.0.0.1", 0), protocol.ResponsesPeer)
        s.lock, s.counts, s.trace, s.errors = threading.Lock(), Counter(), [], []
        s.team = s.run_id = None
        stack.callback(s.server_close)
        threading.Thread(target=s.serve_forever, daemon=True).start()
        stack.callback(s.shutdown)
        stack.enter_context(patch.object(protocol, "scripted_action", action))
        local = f"http://127.0.0.1:{s.server_port}"
        codex_dir = folder / "codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(f'''model_provider = "grid"
chatgpt_base_url = "{local}"
openai_base_url = "{local}/v1"
check_for_update_on_startup = false
[model_providers.grid]
name = "Grid scripted peer"
base_url = "{local}/v1"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
[analytics]
enabled = false
[feedback]
enabled = false
''')
        backend = "codex_source_host" if source_host else "codex_app_server"
        env = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy") or k in ("PATH","LANG")}
        from gx_core.resource_profile import child_resource_environment
        env.update(child_resource_environment())
        env.update(CODEX_HOME=str(codex_dir), HOME=str(folder))
        selection = {"CODEX_HOME": str(codex_dir)}
        if source_host:
            selection["GX_CODEX_HOST"] = str(source_host.resolve())
        stack.enter_context(patch.dict(os.environ, selection))
        state = stack.enter_context(native_state(folder / "state", env={}))
        conn = stack.enter_context(closing(store.open_store(state.db_path)))
        ctx = Ctx(conn=conn, principal=Principal(subject="grid-fixture-reactor", scope="operator"), tool="reactor")
        arts = ArtifactService(conn)
        objective, inputs = read_inputs(SimpleNamespace(instance="pilot", fail_route=None,
                                                        evaluator=None, execution_profiles=None))
        s.proposal = {"project_ref": PROJECT, "objective": objective, "budget_profile": "cpu_only",
            "workflow": {"id": "science_grid", "version": "1"}, "inputs": inputs, "idempotency_key": "grid-pilot"}
        calls, phases, judgments = [], [], []
        def factory(**kwargs):
            original = kwargs["on_server_request"]
            def observe(frame):
                args = frame.message["params"]
                reply = original(frame)
                result = json.loads(reply["contentItems"][0]["text"])
                name = args["tool"]
                calls.append({"phase": s.phase, "tool": name, "thread_id":args["threadId"], "ok": result.get("ok")})
                if not result.get("ok"):
                    s.errors.append([name, result.get("error")])
                    return reply
                data = result["data"]
                if name == "gx_run_start":
                    s.run_id, s.coordinator = data["run_id"], data["coordinator_lease_ref"]
                elif name == "gx_memory_search":
                    s.child = args["threadId"]
                    s.seen.set()
                elif name == "gx_team_manage":
                    if args["arguments"]["action"] == "create":
                        s.team = data["team"]["team_id"]
                    elif args["arguments"]["action"] == "assign":
                        s.assigned.set()
                elif name == "gx_task_acquire":
                    doc = judge(arts, s.task_input)
                    kind = s.task_input["output_kind"]
                    path = arts.inbox_dir(s.run_id) / f"{s.task['task_id']}.json"
                    path.write_text(json.dumps(doc))
                    s.manifest = prepare_manifest(arts, run_id=s.run_id, task_id=s.task["task_id"],
                        attempt=data["attempt"], entries=[{"kind":kind, "inbox_path":path.name,
                        "media_type":"application/json"}], provenance={"method":"grid-pilot/1"})
                    judgments.append({"phase":s.phase, "document":doc})
                s.revision = conn.execute("SELECT revision FROM runs WHERE run_id=?", (s.run_id,)).fetchone()[0]
                return reply
            return AppServerClient(**{**kwargs, "env": env, "on_server_request":observe})
        s.phase = "proposal"
        for cycle in range(6):
            s.seen, s.assigned, s.done = (threading.Event() for _ in range(3))
            if s.run_id:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    reactor.lazy(ctx, s.run_id)
                    reactor.drive_science(ctx, s.run_id)
                    saved = snapshot(conn, "pilot")
                    s.task = conn.execute("SELECT * FROM tasks WHERE run_id=? AND status='READY' ORDER BY rowid LIMIT 1",
                                          (s.run_id,)).fetchone()
                    if s.task or saved["status"] == "COMPLETED":
                        break
                    time.sleep(.05)
                if saved["status"] == "COMPLETED":
                    break
                if not s.task:
                    s.errors.append("no ready task")
                    break
                s.task_input = store.loads(s.task, "input_json")
                s.phase = f"{s.task['stage']}-{s.task_input['round']}"
                s.steps = ([] if s.team else ["create", "transition"]) + ["spawn", "member", "assign"]
                s.revision = conn.execute("SELECT revision FROM runs WHERE run_id=?", (s.run_id,)).fetchone()[0]
            with patch.object(protocol, "CHILD", "grid_worker_" + str(cycle)):
                begin = time.monotonic()
                result = native_app_server.run_session(objective + " Phase " + s.phase, state=state, conn=conn,
                    project_ref=PROJECT, max_wall_seconds=45, max_turns=14, max_workers=2, client_factory=factory,
                    runtime_backend=backend,
                    tool_policy={"tool_profile":"native_v2", "allow_mutations":True, "max_callbacks":30})
                phases.append({"phase":s.phase, "runtime_status": result["status"], "resumed":result["resumed"],
                    "root_thread_id":result["root_thread_id"], "elapsed_s":time.monotonic()-begin,
                    "failure":result.get("failure")})
            if s.errors or result["status"] != "COMPLETED":
                break
        saved = snapshot(conn, "pilot")
        accepted = len([t for t in saved.get("tasks",[]) if t["status"]=="ACCEPTED"])
        passed = (saved["status"]=="COMPLETED" and accepted==4 and len(saved["jobs"])==3 and len(judgments)==4
                  and judgments[1]["document"]["continue_paths"]==["path-a"] and not s.errors)
        return {"workload":"W01","mode":"integration_recorded","backend":backend,"pilot": True,
            "model_service":"deterministic local Responses peer with fixture-side evidence staging",
            "contract_sha256":state.contract_sha256,
            "catalog_sha256":hashlib.sha256(json.dumps(native_app_server.dynamic_tools(profile="native_v2"),
                sort_keys=True).encode()).hexdigest(),
            "input_sha256":hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest(),
            "elapsed_s":time.monotonic()-started, "model":sorted({r["model"] for r in s.trace}),
            "model_requests":len(s.trace), "management_calls":len(calls), "cost":None,
            "calls":calls,"phases":phases,"judgments":judgments,"state":saved,"errors":s.errors,"pass":passed}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source-host", type=Path)
    report = run(p.parse_args().source_host)
    print(json.dumps(report,indent=2))
    raise SystemExit(0 if report["pass"] else 1)
