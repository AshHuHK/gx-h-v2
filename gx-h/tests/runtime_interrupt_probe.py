"""Exercise turn/interrupt on a real Codex runtime with a blocked loopback model."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import tempfile
import threading

from gx_core.hostobs.app_server_adapter import cancel_activity
from gx_core.hostobs.app_server_client import AppServerClient
from gx_core.hostobs.native_app_server import _proxy_preflight


class BlockedModel(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.server.started.set()
        self.server.release.wait(12)

    def log_message(self, *args):
        pass


def run(binary):
    _proxy_preflight()
    with tempfile.TemporaryDirectory(prefix="gx-interrupt-") as tmp:
        folder = Path(tmp)
        server = ThreadingHTTPServer(("127.0.0.1", 0), BlockedModel)
        server.started, server.release = threading.Event(), threading.Event()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        (folder / "config.toml").write_text(f'''model_provider="loopback"
chatgpt_base_url="{base}"
openai_base_url="{base}/v1"
check_for_update_on_startup=false
[model_providers.loopback]
name="Local interruption fixture"
base_url="{base}/v1"
wire_api="responses"
request_max_retries=0
stream_max_retries=0
[features]
apps=false
plugins=false
hooks=false
[analytics]
enabled=false
''')
        env = {k: v for k, v in os.environ.items() if k.lower().endswith("_proxy") or k in ("PATH", "LANG")}
        from gx_core.resource_profile import child_resource_environment
        env.update(child_resource_environment())
        env.update(CODEX_HOME=tmp, HOME=tmp)
        client = AppServerClient(argv=[str(binary), "--outcome-directory", str(folder / "outcomes")],
                                 cwd=tmp, env=env, default_timeout=15)
        try:
            with client:
                client.request("initialize", {"clientInfo": {"name": "gx", "version": "2"},
                                               "capabilities": {"experimentalApi": True}})
                client.notify("initialized", {})
                thread = client.request("thread/start", {"cwd": tmp, "model": "gpt-5.4",
                                        "approvalPolicy": "never", "sandbox": "read-only"})["thread"]["id"]
                turn = client.request("turn/start", {"threadId": thread,
                    "input": [{"type": "text", "text": "Wait for cancellation."}]})["turn"]["id"]
                if not server.started.wait(8):
                    raise RuntimeError("the model did not start")
                cancel_activity(client, thread, turn)
                event = client.run_until(lambda e: e.message.get("method") == "turn/completed"
                    and e.message["params"]["turn"]["id"] == turn, timeout=10)
                status = event.message["params"]["turn"]["status"]
                if status != "interrupted":
                    raise AssertionError(status)
                return {"mode": "integration_recorded", "backend": "codex_source_host",
                        "model": "loopback SSE peer held before response", "turn": turn, "thread": thread,
                        "interrupt_ack": True, "terminal_event_status": status}
        finally:
            server.release.set()
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("binary", type=Path)
    print(json.dumps(run(parser.parse_args().binary.resolve()), indent=2))
