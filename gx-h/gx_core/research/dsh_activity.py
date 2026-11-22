"""Optional task-scoped DSH SDK session. GX owns the task, lease, budget and artifacts."""
import http.client
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import urlsplit

from ..domain import ids
from ..errors import SchemaInvalid

RUNTIME = Path(__file__).resolve().parents[2] / "runtime" / "dsh-sdk"
VERSION = "0.2.2rc2"
CAPABILITIES = {
    "kind": "dsh_sdk", "profile": "sdk-minimal", "native_history": True, "sdk_resume": True,
    "automatic_resend": False, "max_model_calls_per_activity": 1,
    "tools": False, "child": False, "confirm_remote_stop": False,
    "usage": "deepseek_messages_wire_if_available",
}


def validated_profile(document):
    from .chat_provider import validated_profile as validate_limits
    data = dict(document)
    pins = {"sdk_version": VERSION, "patch_sha256": ids.sha256_of_file(RUNTIME / "science.yml")}
    for field, expected in pins.items():
        if data.pop(field, expected) != expected:
            raise SchemaInvalid(f"restore the pinned DSH {field}")
    if data.pop("runtime", None) != "dsh_sdk" or data.get("revision") != 1 or data.get("provider") != "local_messages":
        raise SchemaInvalid("DSH SDK revision 1 requires a local_messages profile")
    endpoint = data.get("endpoint")
    url = urlsplit(endpoint) if isinstance(endpoint, str) else None
    if not url or url.scheme != "http" or url.hostname not in {"127.0.0.1", "::1", "localhost"} or url.path != "/v1/messages" or url.query or url.fragment or url.username or url.password:
        raise SchemaInvalid("local_messages requires a loopback /v1/messages endpoint")
    # Both model activities use the same resource limits. The direct client's endpoint is never invoked here.
    validate_limits({**data, "provider": "local_chat", "endpoint": f"http://{url.netloc}/v1/chat/completions"})
    return {**data, "runtime": "dsh_sdk", **pins}


def session_for(operation):
    return "gx-" + ids.sha256_of_obj(operation)


def invoke(profile, request, state_dir, operation, cancelled):
    """Supervise the real SDK process tree so lease cancellation stops local execution."""
    session_id = session_for(operation)
    python = RUNTIME / ".venv/bin/python"
    command = [str(python if python.is_file() else sys.executable), "-B", "-m", __name__]
    payload = json.dumps({"profile": profile, "request": request, "state_dir": str(state_dir),
                          "session_id": session_id}).encode()
    process = subprocess.Popen(command, cwd=RUNTIME.parents[1], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + profile["timeout_seconds"]
    try:
        while time.monotonic() < deadline:
            try:
                output, _ = process.communicate(payload, timeout=.15)
                if process.returncode:
                    break
                return json.loads(output)
            except (ValueError, UnicodeError):
                break
            except subprocess.TimeoutExpired:
                payload = None
            if reason := cancelled():
                return {"status": "cancelled_locally", "reason": reason, "session_id": session_id}
        return {"status": "outcome_unknown", "reason": "DSH session interrupted", "session_id": session_id}
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.communicate()


def one_request_transport(profile, facts):
    """Forward one local Messages exchange, enforce its slot, observe provider metadata.

    The SDK reports the requested model on assistant events. The server's message_start
    identifies the actual response. The bridge only sends the GX-selected local request.
    """
    url = urlsplit(profile["endpoint"])

    class Messages(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/messages" or facts.get("forwarded"):
                self.send_error(409)
                return
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if message.get("model") != profile["model"] or message.get("max_tokens") != profile["max_output_tokens"]:
                self.send_error(409)
                return
            # This Activity supplies complete originals and asks for one structured result.
            message.pop("tools", None)
            message.pop("tool_choice", None)
            facts["forwarded"] = True
            upstream = http.client.HTTPConnection(url.hostname, url.port, timeout=profile["timeout_seconds"])
            try:
                upstream.request("POST", url.path, json.dumps(message), headers={
                    "Content-Type": "application/json", "Accept": "text/event-stream",
                    "anthropic-version": self.headers.get("anthropic-version", "2023-06-01")})
                response = upstream.getresponse()
                data = response.read(2_000_001)
                if len(data) > 2_000_000:
                    self.send_error(502)
                    return
                facts["http_status"] = response.status
                if response.status != 200:
                    self.send_error(502)
                    return
                for line in data.splitlines():
                    if not line.startswith(b"data:"):
                        continue
                    event = json.loads(line[5:])
                    if event.get("type") == "message_start":
                        meta = event["message"]
                        facts.update(id=meta.get("id"), model=meta.get("model"))
                        if meta.get("usage"):
                            facts["usage"] = dict(meta["usage"])
                    if event.get("type") == "message_delta" and event.get("usage"):
                        facts.setdefault("usage", {}).update(event["usage"])
                    if event.get("type") == "content_block_start" and event["content_block"].get("type") == "tool_use":
                        self.send_error(422)
                        return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            finally:
                upstream.close()

        def log_message(self, *_):
            pass

    return HTTPServer(("127.0.0.1", 0), Messages)


def run_session(payload):
    from deepseek_harness import DeepSeekHarness
    profile, request = payload["profile"], payload["request"]
    root = Path(payload["state_dir"])
    root.mkdir(parents=True, exist_ok=True)
    facts = {}
    with one_request_transport(profile, facts) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        try:
            with DeepSeekHarness(provider="deepseek-official", model=profile["model"], profile="sdk-minimal",
                max_tokens=profile["max_output_tokens"], patches=(str(RUNTIME / "science.yml"),),
                env={"DSH_TELEMETRY_DISABLED":"1", "NODE_USE_ENV_PROXY":"1", "DSH_RESOURCE_CACHE":str(root / "resources")},
                base_url=f"http://127.0.0.1:{server.server_port}", api_key="gx-local-messages",
                cwd=str(root), dsh_home=str(root / "sessions-home"),
                request_timeout_seconds=profile["timeout_seconds"]) as harness:
                prompt = "\n".join(message["content"] for message in request["messages"])
                result = harness.run(prompt, session_id=payload["session_id"])
        finally:
            server.shutdown()
            thread.join()
    if result.finish_reason != "completed" and not facts.get("model"):
        rejected = facts.get("http_status") in (400, 401, 403, 404, 413, 422, 429)
        return {"status": "provider_rejected" if rejected else "outcome_unknown",
                "reason": "DSH turn ended without a complete provider response",
                "provider_http_status": facts.get("http_status"), "session_id": result.session_id}
    body = {"session_id":result.session_id, "finish_reason":result.finish_reason, "final_response":result.final_response,
            "sdk_version":VERSION, "event_types":[event.get("type") for event in result.events], **facts}
    return {"status":"response_received", "body":body}


if __name__ == "__main__":
    payload = json.load(sys.stdin)
    from importlib.metadata import PackageNotFoundError, version

    try:
        installed = all(version(name) == VERSION for name in ("deepseek-harness-sdk", "deepseek-harness-runtime-bin"))
    except PackageNotFoundError:
        installed = False
    if installed:
        try:
            answer = run_session(payload)
        except Exception as exc:
            answer = {"status": "outcome_unknown", "reason": type(exc).__name__, "session_id": payload["session_id"]}
    else:
        answer = {"status": "runtime_unavailable", "reason": "install the pinned SDK/runtime wheels",
                  "session_id": payload["session_id"]}
    print(json.dumps(answer, ensure_ascii=False))
