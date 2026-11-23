"""Deterministic loopback Responses fixture for official Codex protocol measurements.

Only derived fields are persisted. Request bodies and headers stay in memory.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import re
import threading
import time

BRIEF = "GX_NATIVE_BRIEF_5eda7"
STEER = "GX_NATIVE_STEER_9f87a"
CHILD = "native_probe_child"
DYNAMIC = "gx_probe_identity"


def tool(name, args, ident, namespace=None):
    item = dict(type="function_call", name=name, arguments=json.dumps(args), call_id=ident)
    if namespace:
        item["namespace"] = namespace
    return item


def names(value):
    if isinstance(value, list):
        return [name for v in value for name in names(v)]
    if isinstance(value, dict):
        return ([value["name"]] if "name" in value else []) + names(value.get("tools", []))
    return []


def wire_catalog(body):
    """The pinned host can declare tools directly or inside a Responses Lite input item."""
    specs = list(body.get("tools") or [])
    for item in body.get("input", ()):
        if item.get("type") == "additional_tools":
            specs.extend(item["tools"])
    exposed = set(names(specs))
    for group in specs:
        for tool in group.get("tools", ()):
            if group.get("name") == "functions" and tool.get("name") == "exec":
                exposed.update(re.findall(r"^### `([\w.-]+)`$", tool.get("description", ""), re.MULTILINE))
    return specs, exposed


def declared_calls(specs):
    """Flatten the official function/namespace schema while retaining each call's namespace."""
    for spec in specs:
        if spec.get("type") == "namespace":
            for leaf in spec["tools"]:
                yield spec["name"], leaf["name"]
        else:
            yield None, spec["name"]


class Peer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.rows = []
        self.lock = threading.Lock()
        self.gate = threading.Event()
        self.gate.set()
        self.plan = "plain"
        self.child_done = threading.Event()
        self.scenario_count = {}

    def scenario(self, plan):
        self.plan = plan
        self.scenario_count.clear()

    def record(self, body):
        items = body.get("input", [])
        child = any(i.get("recipient") == "/root/" + CHILD for i in items)
        text = json.dumps(items, ensure_ascii=False)
        tools = names(body.get("tools"))
        plan = self.plan
        with self.lock:
            key = (plan, child)
            n = self.scenario_count.get(key, 0) + 1
            self.scenario_count[key] = n
            ident = "response-" + str(len(self.rows) + 1)
            usage = {"input_tokens": 170 if child else 30, "output_tokens": 13 if child else 5}
            usage["total_tokens"] = sum(usage.values())
            row = {
                "id": ident,
                "t": time.monotonic(),
                "scenario": plan,
                "wire_model": body.get("model"),
                "wire_effort": (body.get("reasoning") or {}).get("effort"),
                "child": child,
                "brief_in_input": BRIEF in text,
                "brief_in_instructions": BRIEF in (body.get("instructions") or ""),
                "steer_in_input": STEER in text,
                "steer_samples": sorted({int(n) for n in re.findall(STEER + r" sample (\d+)",text)}),
                "dynamic_in_catalog": DYNAMIC in tools,
                "goal_tools": sorted(set(tools).intersection({"get_goal", "update_goal", "create_goal"})),
                "goal_mutator_declared": "create_goal" in json.dumps(body.get("tools")),
                "spawn_in_catalog": "spawn_agent" in tools,
                "tool_names": tools,
                "tool_outputs": [
                    str(i.get("output"))[:340] for i in items if i.get("type") == "function_call_output"
                ],
                "compact_trigger": any(i.get("type") == "compaction_trigger" for i in items),
                "encrypted_summary_in_context": "GX_ENCRYPTED" in text,
                "local_summary_in_context": "GX_LOCAL_SUMMARY" in text,
                "objective_change_in_input": "GX_GOAL_REPLACED" in text,
                "sandbox_instructions": (
                    "read-only" if "<sandbox_mode>" not in text and "read-only" in text else None
                ),
                "usage": usage,
            }
            self.rows.append(row)
        return row, n

    def item(self, row, n):
        plan, child, ident = row["scenario"], row["child"], row["id"]
        if not self.gate.wait(20):
            raise RuntimeError("test did not release inference barrier")
        if row["compact_trigger"]:
            return {"type": "compaction", "encrypted_content": "GX_ENCRYPTED"}
        if plan == "compact":
            text = "GX_LOCAL_SUMMARY"
        elif plan == "kill_shell" and n == 1:
            return tool(
                "exec_command",
                {"cmd": "echo started > started; sleep 2; echo finished > finished", "yield_time_ms": 10000},
                ident,
            )
        elif plan == "old_submit" and n == 1:
            return tool("gx_submit_product", {"product": {"probe": "old held attempt"}}, ident)
        elif plan == "model_edit" and n == 1:
            return tool("update_goal", {"objective": "GX_FORGED_OBJECTIVE", "status": "complete"}, ident)
        elif plan == "follow" and n == 1 and not child:
            return tool(
                "followup_task",
                {"target": "/root/" + CHILD, "message": "Resume the child sample."},
                ident,
                "collaboration",
            )
        elif plan == "follow" and n == 1 and child:
            return tool(DYNAMIC, {}, ident)
        elif plan == "follow":
            if child:
                self.child_done.set()
            else:
                self.child_done.wait(6)
            text = "Follow-up done"
        elif plan == "shell" and n == 1:
            return tool(
                "exec_command", {"cmd": "sleep 2; echo GX_SHELL_FINISHED", "yield_time_ms": 10000}, ident
            )
        elif plan == "dynamic" and n == 1:
            return tool(DYNAMIC, {}, ident)
        elif plan == "spawn" and n == 1 and not child:
            return tool(
                "spawn_agent",
                {
                    "task_name": CHILD,
                    "fork_turns": "none",
                    "message": "Perform the local protocol sample.",
                    "model": "gpt-6.1-sol",
                    "reasoning_effort": "xhigh",
                },
                ident,
                "collaboration",
            )
        elif plan == "spawn" and n == 1 and child:
            return tool(DYNAMIC, {}, ident)
        elif plan == "spawn" and child:
            self.child_done.set()
            text = "Child sample complete"
        elif plan == "spawn" and not child:
            if n == 2:
                self.child_done.wait(8)
            return (
                tool("update_goal", {"status": "complete"}, ident)
                if n == 2
                else self.message(ident, "Parent complete")
            )
        elif plan == "finish_goal" and n == 1:
            return tool("update_goal", {"status": "complete"}, ident)
        elif plan == "try_goal" and n == 1:
            return tool("create_goal", {"objective": "GX_DISABLED_MODEL_ATTEMPT"}, ident)
        else:
            text = "GX observed " + ident
        return self.message(ident, text)

    @staticmethod
    def message(ident, text):
        return {
            "id": "msg-" + ident,
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_error(404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if self.path.rstrip("/") != "/v1/responses":
            self.send_error(404)
            return
        row, n = self.server.record(json.loads(body))
        item = self.server.item(row, n)
        output = [
            {"type": "response.created", "response": {"id": row["id"]}},
            {"type": "response.output_item.done", "item": item},
            {"type": "response.completed", "response": {"id": row["id"], "usage": row["usage"]}},
        ]
        data = "".join("data: " + json.dumps(v) + "\n\n" for v in output).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass
