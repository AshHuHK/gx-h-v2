"""Real optional DSH runtime, persistent session and recorded loopback model stream."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from importlib.util import find_spec
import hashlib
import json
import os
from pathlib import Path
import tempfile
from threading import Event, Thread
import unittest


@contextmanager
def recorded_model(replies=None, *, respond=None, response_model=None):
    """Yield base URL, captured message requests and replies. respond(body) may supply dynamic text."""
    requests = []
    if replies is None:
        replies = (
            []
            if respond
            else [
                "The recorded grid evidence is GX-SDK-RESULT-17.",
                "Carry GX-SDK-RESULT-17 to the next round.",
            ]
        )

    class MessagesHandler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/messages":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["content-length"])))
            requests.append(body)
            if respond:
                replies.append(respond(body))
            if len(requests) > len(replies):
                self.send_error(400, "Unexpected replay of a recorded model turn")
                return
            text = replies[len(requests) - 1]
            frames = [
                {
                    "type": "message_start",
                    "message": {
                        "id": f"msg-{len(requests)}",
                        "model": response_model or body["model"],
                        "usage": {"input_tokens": 14, "output_tokens": 0},
                    },
                },
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn"},
                    "usage": {"output_tokens": 10},
                },
                {"type": "message_stop"},
            ]
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            try:
                self.end_headers()
                for frame in frames:
                    self.wfile.write(f"event: {frame['type']}\ndata: {json.dumps(frame)}\n\n".encode())
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                # An intentional stop may disconnect a stream whose answer has not arrived.
                pass

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), MessagesHandler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", requests, replies
        finally:
            server.shutdown()
            thread.join(timeout=3)


def sdk_config(directory, base_url, workspace, home):
    patch = Path(__file__).resolve().parents[1] / "runtime/dsh-sdk/science.yml"
    if not patch.is_file():
        raise FileNotFoundError("the GX DSH science profile patch must be installed")
    return dict(
        patches=(str(patch),),
        profile="sdk-minimal",
        provider="deepseek-official",
        model="deepseek-v4-flash",
        base_url=base_url,
        api_key="dummy-loopback-fixture",
        cwd=str(workspace),
        dsh_home=str(home),
        env={
            "DSH_TELEMETRY_DISABLED": "1",
            "DSH_RESOURCE_CACHE": str(directory / "resources"),
            "HTTP_PROXY": "http://127.0.0.1:7890",
            "HTTPS_PROXY": "http://127.0.0.1:7890",
            "NO_PROXY": "localhost,127.0.0.1,::1",
            "NODE_USE_ENV_PROXY": "1",
        },
        initialize_timeout_seconds=30,
        request_timeout_seconds=30,
        shutdown_timeout_seconds=2,
    )


@unittest.skipUnless(
    find_spec("deepseek_harness") and find_spec("deepseek_harness_runtime"),
    "install the optional DSH Python SDK/runtime wheel pair",
)
class DeepSeekRuntimeProcess(unittest.TestCase):
    def test_close_while_recorded_response_is_pending(self):
        from deepseek_harness import DeepSeekHarness

        admitted, release = Event(), Event()

        def withheld(_request):
            admitted.set()
            release.wait(15)
            return "This is a late recorded response."

        with tempfile.TemporaryDirectory() as temporary, recorded_model(respond=withheld) as (
            url,
            requests,
            _,
        ):
            directory = Path(temporary)
            client = DeepSeekHarness(**sdk_config(directory, url, directory, directory / "home"))
            results = []

            def run():
                try:
                    results.append(
                        client.run("Hold this fixture turn until stopped.", session_id="gx-sdk-stop")
                    )
                except Exception as exc:
                    results.append(type(exc).__name__)

            try:
                client.start()
                process = client.client._proc
                worker = Thread(target=run, daemon=True)
                worker.start()
                self.assertTrue(admitted.wait(12), "the DSH turn never reached the fixture endpoint")
                client.close()
                self.assertIsNotNone(process.poll())
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive(), "the SDK caller was still waiting after runtime shutdown")
                self.assertEqual(len(results), 1)
                self.assertNotEqual(getattr(results[0], "finish_reason", None), "completed")
                self.assertEqual(len(requests), 1)
            finally:
                release.set()
                client.close()

    def test_close_cold_resume_and_observe_prior_evidence(self):
        from deepseek_harness import DeepSeekHarness

        first_prompt = "Read the recorded GX experiment observation GX-OBS-93."
        followup = "What conclusion did the first turn record?"
        session_id = "gx-science-sdk-resume"
        with tempfile.TemporaryDirectory() as temporary, recorded_model() as (base_url, requests, replies):
            directory = Path(temporary)
            workspace = directory / "work"
            workspace.mkdir()
            home = directory / "harness"
            config = sdk_config(directory, base_url, workspace, home)
            with DeepSeekHarness(**config) as first:
                process = first.client._proc
                answer = first.run(first_prompt, session_id=session_id)
                self.assertEqual(answer.final_response, replies[0], answer)
                self.assertEqual(answer.session_id, session_id)
            self.assertIsNotNone(process.poll())
            self.assertEqual(len(requests), 1)

            with DeepSeekHarness(**config) as restarted:
                next_process = restarted.client._proc
                session = restarted.resume_session(session_id)
                self.assertNotEqual(process.pid, next_process.pid)
                self.assertEqual(session.id, session_id)
                self.assertEqual(len(requests), 1)
                continuation = session.run(followup)
                self.assertEqual(continuation.final_response, replies[1], continuation)
            self.assertIsNotNone(next_process.poll())
            self.assertEqual(len(requests), 2)
            for request in requests:
                self.assertEqual(request["model"], config["model"])
                self.assertLessEqual(
                    {tool["name"] for tool in request.get("tools", [])}, {"working_directory"}
                )
            event = next(e for e in continuation.events if e.get("type") == "assistant/message")
            self.assertEqual(
                event["data"]["usage"], {"inputTokens": 14, "outputTokens": 10, "totalTokens": 24}
            )
            conversation = [(m["role"], json.dumps(m["content"])) for m in requests[-1]["messages"]]
            for role, text in [("user", first_prompt), ("assistant", replies[0]), ("user", followup)]:
                self.assertTrue(any(r == role and text in c for r, c in conversation), (role, text))
            if output := os.environ.get("GX_DSH_TEST_EVIDENCE"):
                Path(output).write_text(
                    json.dumps(
                        {
                            "mode": "real_dsh_runtime_recorded_loopback_model",
                            "session_id": session_id,
                            "gx_profile_patch_sha256": hashlib.sha256(
                                Path(config["patches"][0]).read_bytes()
                            ).hexdigest(),
                            "sdk_version": version("deepseek-harness-sdk"),
                            "runtime_version": version("deepseek-harness-runtime-bin"),
                            "pids": [process.pid, next_process.pid],
                            "process_exit_codes": [process.returncode, next_process.returncode],
                            "provider_requests": len(requests),
                            "resume_provider_requests": 0,
                            "cold_history_matching_roles": ["user", "assistant", "user"],
                            "first_finish": answer.finish_reason,
                            "second_finish": continuation.finish_reason,
                            "raw_responses": replies,
                            "request_keys": sorted(requests[0]),
                            "message_keys": [sorted(m) for m in requests[-1]["messages"]],
                            "advertised_tools": [t.get("name") for t in requests[0].get("tools", [])],
                            "requested_model": requests[0]["model"],
                            "usage_source": "fixture SSE frames",
                            "fixture_usage_per_turn": {"input_tokens": 14, "output_tokens": 10},
                            "result_event_types": [event.get("type") for event in continuation.events],
                            "model_events": [
                                event
                                for event in continuation.events
                                if event.get("type")
                                in ("model/selection", "assistant/message", "step/end", "turn/end")
                            ],
                        },
                        indent=2,
                    )
                    + "\n"
                )


if __name__ == "__main__":
    unittest.main()
