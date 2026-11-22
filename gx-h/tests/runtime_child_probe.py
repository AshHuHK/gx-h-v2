"""Real Codex app-server/optional source-host, scripted loopback Responses peer.

Run with python3 -Bm tests.runtime_child_probe [--source-host /absolute/path].
Isolated CODEX_HOME, no model credentials. Only selected metadata is emitted.
"""
import argparse
from datetime import datetime, timezone
import hashlib
from collections import Counter
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch
import threading
import time

from gx_core.hostobs import native_app_server
from gx_core.hostobs.app_server_client import AppServerClient
from gx_core.native_release import native_state
from gx_core.persistence import store

PROJECT = "runtime-child-probe"
CHILD = "gx_child_probe"
READ = {"query": "Record the local protocol check.", "project_scope": PROJECT}


def tool(call_id, name, args, namespace=None):
    result = {"type": "function_call", "name": name, "call_id": call_id, "arguments": json.dumps(args)}
    if namespace:
        result["namespace"] = namespace
    return result


def scripted_action(s, actor, index, ident, model):
    if actor == "child":
        if index == 1 and s.phase == "create":
            return tool(ident, "gx_memory_search", READ)
        if s.phase == "create" and s.lose_ack and index >= 2:
            # No acquire until the original dispatch is reconciled on a new process.
            s.child_done.set()
            return None
        if index == (2 if s.phase == "create" else 1):
            if not s.assigned.wait(timeout=8):
                raise RuntimeError("the scripted worker did not receive its task assignment")
            return tool(ident, "gx_task_acquire", {"task_id": s.task_id})
        if index == 2 and s.phase == "resume":
            return tool(ident, "gx_task_finish", {"task_id": s.task_id, "manifest_ref": s.manifest})
        s.child_done.set()
        return None
    if s.phase == "resume":
        if index == 1:
            return tool(ident, "gx_run_status", {"run_id": s.run_id})
        if index == 2:
            return tool(ident, "followup_task", {"target": "/root/" + CHILD,
                         "message": "Finish the same assigned task."}, "collaboration")
        s.child_done.wait(timeout=8)
        return None
    if index == 1:
        return tool(ident, "gx_run_start", {"objective": "Check a measurement with an independent replicate.",
            "project_ref": PROJECT, "budget_profile": "cpu_only", "workflow": {"id": "module_collaboration",
            "version": "1"}, "inputs": {"modules": [{"key": "counts", "question": "Can the count be replicated?"}]},
            "idempotency_key": "probe-run"})
    if index == 2:
        return tool(ident, "gx_next_actions", {"run_id": s.run_id})
    if index == 3:
        action, payload = "create", {"intent": {"objective": "Repeat the count."}}
    elif index == 4:
        action, payload = "transition", {"team_id": s.team_id, "status": "ACTIVE"}
    elif index == 5:
        return tool(ident, "spawn_agent", {"task_name": CHILD, "message": "Accept the count task.",
            "fork_turns": "none", "model": model, "reasoning_effort": "ultra"}, "collaboration")
    elif index in (6, 7):
        if not s.child_seen.wait(timeout=8):
            raise RuntimeError("no admitted worker callback observed")
        payload = {"team_id": s.team_id, "subject": "native:" + s.child_id}
        if index == 6:
            action = "member"
            payload["action"] = "add"
        else:
            action = "assign"
            payload["task_id"] = s.task_id
    else:
        s.child_done.wait(timeout=8)
        return None
    return tool(ident, "gx_team_manage", {"run_id": s.run_id, "action": action, "payload": payload,
                "coordinator_lease_ref": s.coordinator, "expected_revision": s.revision, "idempotency_key": ident})


class ResponsesPeer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        payload = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.rstrip("/") != "/v1/responses":
            self.send_error(404)
            return
        body = json.loads(payload)
        child = any(entry.get("recipient") == "/root/" + CHILD for entry in body.get("input") or [])
        phase = self.server.phase
        actor = "child" if child else "root"
        with self.server.lock:
            self.server.counts[phase, actor] += 1
            index = self.server.counts[phase, actor]
            ident = f"{phase}-{actor}-{index}"
            self.server.trace.append({"response": ident, "model": body.get("model"), "path": self.path,
                "peer_address": self.client_address[0]})
        try:
            item = scripted_action(self.server, actor, index, ident, body["model"])
        except Exception as exc:
            self.server.errors.append(str(exc))
            item = None
        if item is None:
            item = {"id": "msg-" + ident, "type": "message", "role": "assistant", "content":
                    [{"type": "output_text", "text": "Child protocol evidence recorded."}]}
        events = [{"type": "response.created", "response": {"id": ident}},
                  {"type": "response.output_item.done", "item": item},
                  {"type": "response.completed", "response": {"id": ident, "usage":
                  {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35}}}]
        content = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)


def run(source_host=None, lose_ack=False):
    native_app_server._proxy_preflight()
    with ExitStack() as stack:
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="gx-child-contract-")))
        server = ThreadingHTTPServer(("127.0.0.1", 0), ResponsesPeer)
        server.lock, server.counts, server.trace = threading.Lock(), Counter(), []
        server.errors, server.acquire_receipts = [], []
        server.lose_ack, server.lost_receipts = lose_ack, []
        server.child_done, server.child_seen, server.assigned = (threading.Event() for _ in range(3))
        stack.callback(server.server_close)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        stack.callback(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}"
        codex_dir = folder / "codex"
        codex_dir.mkdir()
        (codex_dir / "config.toml").write_text(f'''model_provider = "gx_probe"
chatgpt_base_url = "{url}"
openai_base_url = "{url}/v1"
check_for_update_on_startup = false
[model_providers.gx_probe]
name = "GX loopback"
base_url = "{url}/v1"
wire_api = "responses"
requires_openai_auth = false
stream_max_retries = 0
request_max_retries = 0
[analytics]
enabled = false
[feedback]
enabled = false
''')
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LD_LIBRARY_PATH", "http_proxy", "https_proxy",
            "all_proxy", "no_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY") if key in os.environ}
        env.update(CODEX_HOME=str(codex_dir), HOME=str(folder), CODEX_APP_SERVER_CHATGPT_BASE_URL=url)
        if source_host:
            env["GX_CODEX_HOST"] = str(Path(source_host).resolve())
        backend = "codex_source_host" if source_host else "codex_app_server"
        stack.enter_context(patch.dict(os.environ, {"CODEX_HOME": str(codex_dir), **(
            {"GX_CODEX_HOST": str(Path(source_host).resolve())} if source_host else {})}))
        clients = []
        def factory(**kwargs):
            original = kwargs["on_server_request"]
            def collect(frame):
                response = original(frame)
                data = json.loads(response["contentItems"][0]["text"])
                args = frame.message["params"]
                if not data.get("ok"):
                    server.errors.append([args["tool"], data.get("error", {}).get("message")])
                    return response
                answer = data["data"]
                if data.get("revision") is not None:
                    server.revision = data["revision"]
                if args["tool"] == "gx_run_start":
                    server.run_id, server.coordinator = answer["run_id"], answer["coordinator_lease_ref"]
                if args["tool"] == "gx_next_actions":
                    server.task_id = answer["next_actions"][0]["task_id"]
                if args["tool"] == "gx_team_manage":
                    if args["arguments"]["action"] == "create":
                        server.team_id = answer["team"]["team_id"]
                    elif args["arguments"]["action"] == "assign":
                        server.assigned.set()
                if args["tool"] == "gx_task_acquire":
                    server.acquire_receipts.append({key: answer.get(key) for key in
                                                    ("attempt", "lease_ref", "eligible", "dispatch_operation_id")})
                if args["tool"] == "gx_memory_search":
                    server.child_id = args["threadId"]
                    server.child_seen.set()
                return response
            notify = kwargs["on_notification"]
            def notifications(frame):
                msg = frame.message
                item = msg.get("params", {}).get("item") or {}
                if (lose_ack and server.phase == "create" and msg["method"] == "item/completed"
                        and item.get("type") == "subAgentActivity" and item.get("kind") == "started"):
                    server.lost_receipts.append(item["id"])
                    return
                notify(frame)
            client = AppServerClient(**{**kwargs, "on_server_request": collect, "on_notification": notifications,
                                       "env": env})
            clients.append(client)
            return client

        state = stack.enter_context(native_state(folder / "state", env={}))
        conn = store.open_store(state.db_path)
        stack.callback(conn.close)
        report = {"mode": "integration_recorded", "backend": "source_host" if source_host else "codex_app_server",
                  "model_service": "scripted loopback HTTP", "model_usage_cost": None, "phases": [],
                  "started_utc": datetime.now(timezone.utc).isoformat(),
                  "tool_catalog_sha256": hashlib.sha256(json.dumps(native_app_server.dynamic_tools(profile="native_v2"),
                      sort_keys=True).encode()).hexdigest()}
        roots, child_ids = [], []
        for phase in ("create", "resume"):
            server.phase = phase
            server.child_done.clear()
            events = []
            start = time.monotonic()
            result = native_app_server.run_session("Record the " + phase + " protocol observations.", state=state,
                conn=conn, project_ref=PROJECT, max_wall_seconds=40, max_turns=12, max_workers=2,
                runtime_backend=backend, tool_policy={"tool_profile": "native_v2", "allow_mutations": True, "max_callbacks": 12},
                client_factory=factory, event_sink=events.append)
            elapsed = round((time.monotonic() - start) * 1000, 1)
            root = result.get("root_thread_id")
            callbacks = [{k: row.get(k) for k in ("thread_id", "turn_id", "call_id", "tool", "ok", "error_code")}
                         for row in events if row.get("kind") == "GX_call"]
            children = {row["thread_id"] for row in callbacks if row["thread_id"] != root and row["ok"]}
            roots.append(root)
            child_ids.append(children)
            diagnostics = [row for row in events if row.get("kind") in
                           ("metadata_lookup_refused", "native_host_error", "metadata_refusal_diagnostic")]
            adapter_ids = sorted({e["native_call"]["adapter_instance_id"] for e in events if e["kind"] == "GX_call"})
            stderr = clients[-1].transport.stderr_tail.decode(errors="replace").splitlines()
            report["phases"].append({"phase": phase, "run_session_ms": elapsed, "status": result["status"], "root": root, "resumed":
                result["resumed"], "callbacks": callbacks, "failure": result.get("failure"), "diagnostics": diagnostics,
                "stderr_tail": stderr[-6:] if result["status"] != "COMPLETED" else [],
                "adapter_instance_ids": adapter_ids, "event_counts": dict(Counter(e["kind"] for e in events))})
            if result["status"] != "COMPLETED" or server.errors:
                break
            if phase == "create":
                report["dispatch_before_resume"] = [row[0] for row in conn.execute(
                    "SELECT new_state FROM events WHERE kind='native.spawn_observed' ORDER BY event_id")]
                from gx_core.artifacts import ArtifactService
                from gx_core.artifacts.delivery import prepare_manifest
                artifacts = ArtifactService(conn)
                inbox = artifacts.inbox_dir(server.run_id)
                plan = {"items": [{"key": "counts", "question": "Can the count be reproduced?", "role": "executor"}]}
                (inbox / "plan.json").write_text(json.dumps(plan))
                server.manifest = prepare_manifest(artifacts, run_id=server.run_id, task_id=server.task_id,
                    attempt=1, entries=[{"kind": "module_plan", "inbox_path": "plan.json",
                                         "media_type": "application/json"}],
                    provenance={"method_version": "probe/1"})
        if server.assigned.is_set():
            report["leases"] = [dict(row) for row in conn.execute("SELECT lease_id,attempt,task_id,principal "
                "FROM leases WHERE task_id=?", (server.task_id,))]
            report["submissions"] = [dict(row) for row in conn.execute("SELECT submission_id,lease_id,task_id "
                "FROM submissions WHERE task_id=?", (server.task_id,))]
            report["dispatch_operation_id"] = native_app_server.native_spawn.dispatch_for_worker(
                conn, project_ref=PROJECT, session_id=roots[0], parent_thread_id=roots[0], thread_id=server.child_id)
        report["ended_utc"] = datetime.now(timezone.utc).isoformat()
        if source_host:
            report["outcomes"] = [json.loads(p.read_text()) for p in sorted((state.directory / "codex-outcomes").glob("*.json"))]
            rollouts = sorted(p for p in (codex_dir / "sessions").rglob("rollout-*") if p.is_file() and (
                p.name.endswith(".jsonl") or p.name.endswith(".jsonl.zst")))
            if rollouts:
                # Read each rollout twice. A repeated report cannot double the projected total.
                command = [str(Path(source_host).resolve()), "usage", *rollouts, *rollouts]
                projection = subprocess.run(command, capture_output=True, text=True, env=env, timeout=20, check=True)
                report["usage_from_doubled_rollouts"] = json.loads(projection.stdout)
        if server.assigned.is_set():
            report["registered_artifacts"] = [dict(row) for row in conn.execute(
                "SELECT artifact_ref,bytes,sha256 FROM artifacts WHERE task_id=? AND kind='module_plan'", (server.task_id,))]
        report["lost_receipts"] = server.lost_receipts
        report["acquire_receipts"] = server.acquire_receipts
        report["errors"] = server.errors
        report["model_requests"] = server.trace
        report["pass"] = (len(roots) == 2 and roots[0] is not None and roots[0] == roots[1]
            and len(child_ids[0]) == 1 and child_ids[0] == child_ids[1]
            and all(any(c["ok"] and c["thread_id"] == p["root"] for c in p["callbacks"]) for p in report["phases"])
            and all(any(c["tool"] == "gx_task_acquire" and c["thread_id"] in children and c["ok"]
                        for c in p["callbacks"]) for children, p in zip(child_ids, report["phases"]) if not lose_ack or p["phase"] == "resume")
            and any(c["tool"] == "gx_task_finish" and c["ok"] for c in report["phases"][1]["callbacks"])
            and len(report.get("leases", [])) == len(report.get("submissions", [])) == 1
            and len(server.acquire_receipts) == (1 if lose_ack else 2)
            and all(receipt["lease_ref"] == report["leases"][0]["lease_id"] and receipt["attempt"] == 1
                    and receipt["eligible"] for receipt in server.acquire_receipts)
            and bool(report["dispatch_operation_id"]) and len(report["registered_artifacts"]) == 1
            and all(r["dispatch_operation_id"] == report["dispatch_operation_id"] for r in server.acquire_receipts)
            and not report["errors"])
        if lose_ack:
            report["pass"] = report["pass"] and len(server.lost_receipts) == 1 and (
                report.get("dispatch_before_resume") == ["creation_pending"])
        if source_host:
            expected_turns = {(c["thread_id"], c["turn_id"]) for phase in report["phases"] for c in phase["callbacks"]}
            observed_turns = {(r["thread_id"], r["turn_id"]) for r in report["outcomes"] if r["status"] == "completed"}
            usage = report.get("usage_from_doubled_rollouts", {})
            ids = {entry["response_id"] for entry in usage.get("responses", [])}
            model_ids = {entry["response"] for entry in server.trace}
            report["outcome_coverage"] = expected_turns == observed_turns and bool(expected_turns)
            report["usage_coverage"] = (ids == model_ids and usage.get("observed_records") == 2 * len(model_ids)
                                        and usage.get("observed_total_tokens") == 35 * len(model_ids))
            report["pass"] = report["pass"] and report["outcome_coverage"] and report["usage_coverage"]
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-host")
    parser.add_argument("--lose-spawn-ack", action="store_true")
    args = parser.parse_args()
    result = run(args.source_host, args.lose_spawn_ack)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    sys.exit(0 if result["pass"] else 1)
