"""Test-only local Responses service. All scientific inputs come from the Codex turn."""

from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading

GX = Path(__file__).resolve().parents[1] / "gx-h"
sys.path.insert(0, str(GX))
from examples.science_judgment import judge


class ReadArtifact(LookupError):
    pass


class Artifacts:
    def __init__(self, replies):
        self.replies = replies

    def read_json(self, ref):
        if ref not in self.replies:
            raise ReadArtifact(ref)
        return self.replies[ref]


def message_texts(item):
    content = item.get("content", [])
    if isinstance(content, str):
        return [content]
    return [part.get("text", "") for part in content if isinstance(part, dict)]


def brief_from(body):
    # Decode exactly the document delivered by the GX worker Root. The script
    # has no access to the SQLite state, product driver or artifact directory.
    for item in reversed(body["input"]):
        if item.get("role") not in ("user", "developer"):
            continue
        for text in reversed(message_texts(item)):
            marker = "GX task brief\n"
            if marker in text:
                return json.JSONDecoder().raw_decode(text.split(marker, 1)[1].lstrip())[0]
    raise ValueError("no GX task brief in model-visible input")


def output_json(value):
    if isinstance(value, list):
        value = "".join(item["text"] for item in value if "text" in item)
    return json.loads(value)


def tool_history(items):
    calls = {item["call_id"]: item for item in items if item.get("type") == "function_call" and item.get("call_id")}
    reads = {}
    submitted = False
    for item in items:
        if item.get("type") != "function_call_output":
            continue
        call = calls.get(item.get("call_id"))
        if not call or call["name"] not in {"gx_read_artifact", "gx_submit_product"}:
            continue
        reply = output_json(item["output"])
        if isinstance(reply, dict) and reply.get("error"):
            raise ValueError(f"{call['name']} failed: {reply['error']}")
        if call["name"] == "gx_read_artifact":
            ref = json.loads(call["arguments"])["ref"]
            reads[ref] = reply
        else:
            submitted = True
    return Artifacts(reads), submitted


def fixture_action(body, request_id):
    offered = [entry for group in body.get("tools", []) for entry in (group.get("tools") or [group])]
    if not any(tool.get("name") == "gx_submit_product" for tool in offered):
        return {"type": "message", "id": "m-" + request_id, "role": "assistant",
                "content": [{"type": "output_text", "text": "Received the synthetic experiment request."}]}, {
                    "actor": "main"}
    brief = brief_from(body)
    task = brief["input"]
    info = {"task_id": brief["task_id"], "output_kind": task["output_kind"], "round": task["round"]}
    artifacts, submitted = tool_history(body["input"])
    if submitted:
        return {"type": "message", "id": "m-" + request_id, "role": "assistant",
                "content": [{"type": "output_text", "text": "Submitted the evaluated GX product."}]}, info
    try:
        product = judge(artifacts, task)
        if task["output_kind"] == "science_decision" and task["round"] == 1 and product["continue_paths"]:
            source = product["continue_paths"][0]
            route, = [r for ref in task["evidence_refs"] for r in artifacts.read_json(ref)["routes"]
                      if r["path_id"] == source]
            measured = artifacts.read_json(route["observations_ref"])["observations"]
            best = max(measured, key=lambda point: point["score"])
            gap = min(abs(point["x"] - best["x"]) for point in measured if point["x"] != best["x"])
            grid = {**product["next_grids"][source], "candidates": [
                {"candidate_id": f"pivot-{label}", "x": best["x"] + sign * gap / 4}
                for label, sign in (("lower", -1), ("upper", 1))]}
            predictor = (f"At least one of the two quarter-step samples around x={best['x']:g}, "
                         f"whose observed score was {best['score']:g}, reaches the frozen threshold. "
                         "Two lower scores refute this finite-grid prediction.")
            product.update(continue_paths=[], next_grids={},
                           pivots=[{"from_path": source, "id": f"{source}-pivot",
                                    "prediction": predictor, "grid": grid}],
                           reason=product["reason"] + " Test the new finite-grid prediction.")
        name, args = "gx_submit_product", {"product": product}
    except ReadArtifact as required:
        ref = required.args[0]
        name, args = "gx_read_artifact", {"ref": ref}
        info["ref"] = ref
    info["tool"] = name
    return {"type": "function_call", "name": name, "arguments": json.dumps(args),
            "call_id": "call-" + request_id}, info


class Peer(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_error(404)

    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.request_count += 1
            ident = f"fixture-{self.server.request_count}"
        metadata = {"id": ident, "at_utc": datetime.now(timezone.utc).isoformat(), "model": body.get("model"),
                    "reasoning_effort": (body.get("reasoning") or {}).get("effort"),
                    "gx_tools": sorted(tool["name"] for group in body.get("tools", [])
                                       for tool in (group.get("tools") or [group])
                                       if (tool.get("name") or "").startswith("gx_")),
                    "peer": self.client_address[0]}
        try:
            item, detail = fixture_action(body, ident)
            metadata.update(detail)
        except Exception as exc:
            metadata["error"] = f"{type(exc).__name__}: {exc}"
            self.server.record(metadata)
            self.send_error(400, "Unexpected acceptance fixture input")
            return
        usage = {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35}
        metadata["scripted_provider_tokens"] = usage["total_tokens"]
        self.server.record(metadata)
        if (self.server.fault_enabled and metadata.get("tool") == "gx_submit_product"
                and metadata.get("round") == 0 and metadata["output_kind"] == "science_assessment"):
            self.server.fault_enabled = False
            self.server.fault_ready.set()
            self.server.fault_release.wait(timeout=20)
        events = [{"type": "response.created", "response": {"id": ident}},
                  {"type": "response.output_item.done", "item": item},
                  {"type": "response.completed", "response": {"id": ident, "usage": usage}}]
        raw = "".join("data: " + json.dumps(event) + "\n\n" for event in events).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass


class FixtureServer(ThreadingHTTPServer):
    def __init__(self, trace):
        super().__init__(("127.0.0.1", 0), Peer)
        self.lock = threading.Lock()
        self.rows = []
        self.request_count = 0
        self.trace = trace
        self.fault_enabled = False
        self.fault_ready = threading.Event()
        self.fault_release = threading.Event()

    def record(self, row):
        with self.lock:
            with self.trace.open("a") as log:
                log.write(json.dumps(row) + "\n")
            self.rows.append(row)


@contextmanager
def local_responses(trace):
    with FixtureServer(trace) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.shutdown()
            thread.join()
