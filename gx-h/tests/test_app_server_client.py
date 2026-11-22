"""Protocol controls using inert injected transports and owned stdlib pipe peers.

No test launches Codex, authenticates, starts a model turn or uses the network.
The subprocess peers below are software fixtures, not observed Host sessions.
"""
from __future__ import annotations

from collections import deque
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from gx_core.hostobs import app_server_client as app


def line(message):
    return json.dumps(message, ensure_ascii=False).encode() + b"\n"


class MemoryTransport:
    """Declared software transport; EOF is explicit, never a fabricated response."""
    def __init__(self, messages=()):
        self.lines = deque(x if isinstance(x, bytes) else line(x) for x in messages)
        self.writes = []
        self.read_count = 0
        self.close_count = 0

    def write_line(self, data, timeout):
        self.writes.append(json.loads(data))

    def read_line(self, timeout):
        self.read_count += 1
        if not self.lines:
            raise app.AppServerDisconnected("fixture EOF")
        return self.lines.popleft()

    def close(self):
        self.close_count += 1


PEER = """
import json, os, sys, time
def read():
    return json.loads(sys.stdin.buffer.readline())
def emit(value):
    sys.stdout.buffer.write(json.dumps(value, ensure_ascii=False).encode() + b'\\n')
    sys.stdout.buffer.flush()
"""


class FakeOwnedProcess:
    """Deterministic cleanup schedule; no PID, process or OS signal exists."""
    def __init__(self, waits):
        self.waits = deque(waits)
        self.calls = []
        self.returncode = None
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()

    def poll(self):
        self.calls.append("poll")
        return self.returncode

    def terminate(self):
        self.calls.append("terminate")

    def kill(self):
        self.calls.append("kill")

    def wait(self, timeout=None):
        self.calls.append(("wait", timeout))
        result = self.waits.popleft()
        if isinstance(result, Exception):
            raise result
        self.returncode = result
        return result


def fake_owned_transport(*waits):
    """Exercise the real close method without constructing any subprocess."""
    transport = app.StdioTransport.__new__(app.StdioTransport)
    transport._closed = transport._cleanup_complete = False
    transport.cleanup_error = None
    transport.cleanup_timeout = 0.05
    transport.process = FakeOwnedProcess(waits)
    transport._selector = mock.Mock()
    return transport


class AppServerClientTests(unittest.TestCase):
    def client(self, messages=(), **kwargs):
        transport = MemoryTransport(messages)
        client = app.AppServerClient(transport=transport, **kwargs).start()
        self.addCleanup(client.close)
        return client, transport

    def test_polling_a_quiet_pipe_keeps_a_callback_its_full_reply_deadline(self):
        now = [100.0]
        def handle(_):
            now[0] += 3
            return {"done": True}
        client, transport = self.client([{"id": 77, "method": "item/tool/call", "params": {}}],
                                        on_server_request=handle)
        with mock.patch.object(app.time, "monotonic", side_effect=lambda: now[0]):
            with mock.patch.object(transport, "read_line", side_effect=app.AppServerTimeout("wake up")):
                self.assertIsNone(client.run_until(lambda _: True, timeout=10, poll_interval=2))
            event = client.run_until(lambda _: True, timeout=10, poll_interval=2)
        self.assertEqual(event.message["id"], 77)
        self.assertEqual(transport.writes, [{"id": 77, "result": {"done": True}}])
        self.assertEqual(transport.close_count, 0)

    def peer(self, source, **kwargs):
        client = app.AppServerClient([sys.executable, "-I", "-S", "-B", "-u", "-c", PEER + source], **kwargs)
        client.start()
        self.addCleanup(client.close)
        return client

    def test_explicit_start_and_context_have_no_implicit_protocol_or_process(self):
        transport = MemoryTransport()
        with mock.patch.object(app.subprocess, "Popen", side_effect=AssertionError("no implicit process")):
            client = app.AppServerClient(transport=transport)
            self.assertEqual(transport.writes, [])
            with self.assertRaises(app.AppServerDisconnected):
                client.request("initialize")
            with client as same:
                self.assertIs(same, client)
                self.assertIs(client.start(), client)
                self.assertEqual(transport.writes, [])
            client.close()
            self.assertEqual(transport.close_count, 1)
            with self.assertRaises(app.AppServerDisconnected):
                client.start()

    def test_never_started_injected_transport_remains_callers_resource(self):
        transport = MemoryTransport()
        app.AppServerClient(transport=transport).close()
        self.assertEqual(transport.close_count, 0)

    def test_official_omitted_jsonrpc_wire_and_explicit_notify_real_pipes(self):
        client = self.peer("""
request = read()
assert request == {'id': 1, 'method': 'initialize', 'params': {'clientInfo': {'name': 'software-test'}}}
emit({'id': request['id'], 'result': {'protocol': 'fixture'}})
notice = read()
assert notice == {'method': 'initialized', 'params': {}}
emit({'method': 'software/done', 'params': notice})
""")
        self.assertEqual(client.request("initialize", {"clientInfo": {"name": "software-test"}}), {"protocol": "fixture"})
        self.assertEqual(client.response_received(1).receive_seq, 1)
        client.notify("initialized")
        event = client.run_until(lambda row: row.message["method"] == "software/done")
        self.assertEqual(event.receive_seq, 2)

    def test_interleaved_notifications_and_bidirectional_request_real_pipes(self):
        notifications, calls = [], []
        def handler(received):
            calls.append(received)
            return {"contentItems": [{"type": "inputText", "text": "fixture-only"}], "success": True}
        client = self.peer("""
request = read()
emit({'method': 'thread/started', 'params': {'thread': {'id': 'software-thread'}}})
emit({'id': 1, 'method': 'item/tool/call', 'params': {'threadId': 'software-thread', 'turnId': 'software-turn', 'callId': 'software-call'}})
answer = read()
assert answer['id'] == 1 and answer['result']['success'] is True
emit({'method': 'turn/completed', 'params': {'turn': {'id': 'software-turn', 'status': 'completed'}}})
emit({'id': request['id'], 'result': {'answer': answer}})
""", on_notification=notifications.append, on_server_request=handler)
        result = client.request("software/start")
        self.assertEqual(result["answer"]["result"]["contentItems"][0]["text"], "fixture-only")
        self.assertEqual([x.receive_seq for x in notifications], [1, 3])
        self.assertEqual(calls[0].receive_seq, 2)
        self.assertEqual(calls[0].message["params"]["callId"], "software-call")
        self.assertEqual(client.last_received.receive_seq, 4)
        self.assertEqual(client.response_received(1).receive_seq, 4)
        events = [client.run_until(lambda _: True) for _ in range(3)]
        self.assertEqual([x.receive_seq for x in events], [1, 2, 3])
        self.assertEqual([x.receive_seq for x in notifications], [1, 3])
        self.assertEqual(len(calls), 1)

    def test_large_native_item_followed_by_tool_callback_real_pipes(self):
        notifications, calls = [], []
        def handler(row):
            calls.append(row)
            return {"success": True, "contentItems": [{"type": "inputText", "text": row.message["params"]["callId"]}]}
        client = self.peer("""
request = read()
item = {'id': 'previous', 'type': 'dynamicToolCall', 'namespace': None, 'tool': 'fixture',
        'arguments': {}, 'status': 'completed', 'success': True, 'durationMs': 1,
        'contentItems': [{'type': 'inputText', 'text': 'x' * (16 * 1024 * 1024)}]}
emit({'method': 'item/completed', 'params': {'threadId': 'thread', 'turnId': 'turn',
      'completedAtMs': 1, 'item': item}})
emit({'id': 17, 'method': 'item/tool/call', 'params': {'threadId': 'thread', 'turnId': 'turn',
      'callId': 'next-call', 'namespace': None, 'tool': 'fixture', 'arguments': {}}})
reply = read()
emit({'id': request['id'], 'result': reply})
""", max_inbound_message_bytes=64 * 1024 * 1024,
            on_notification=notifications.append, on_server_request=handler)
        reply = client.request("software/start")
        self.assertEqual(reply, {"id": 17, "result": {"success": True,
            "contentItems": [{"type": "inputText", "text": "next-call"}]}})
        self.assertEqual([row.receive_seq for row in notifications], [1])
        item = notifications[0].message["params"]["item"]
        self.assertEqual(len(item["contentItems"][0]["text"]), 16 * 1024 * 1024)
        self.assertEqual([row.receive_seq for row in calls], [2])
        self.assertEqual(client.response_received(1).receive_seq, 3)

    def test_out_of_order_responses_and_metadata_real_pipes(self):
        client = self.peer("""
first, second = read(), read()
emit({'id': second['id'], 'result': {'name': 'second'}})
emit({'method': 'software/progress', 'params': {}})
emit({'id': first['id'], 'result': {'name': 'first'}})
""")
        first, second = client.send("software/first"), client.send("software/second")
        self.assertEqual(client.wait(first), {"name": "first"})
        self.assertEqual(client.wait(second), {"name": "second"})
        self.assertEqual(client.response_received(first).receive_seq, 3)
        self.assertEqual(client.response_received(second).receive_seq, 1)
        self.assertEqual(client.pending_ids, ())
        self.assertEqual(client.run_until(lambda _: True).receive_seq, 2)

    def test_fragmented_unicode_and_crlf_real_pipes(self):
        client = self.peer("""
request = read()
raw = json.dumps({'id': request['id'], 'result': {'text': 'λ中文'}}, ensure_ascii=False).encode() + b'\\r\\n'
for byte in raw:
    os.write(1, bytes([byte]))
""")
        self.assertEqual(client.request("software/unicode"), {"text": "λ中文"})

    def test_rpc_error_preserves_details_and_connection(self):
        client, transport = self.client([
            {"id": 1, "error": {"code": -32001, "message": "fixture overload", "data": {"retry": "caller decides"}}},
            {"id": 2, "result": None},
        ])
        with self.assertRaises(app.AppServerResponseError) as caught:
            client.request("software/once")
        self.assertEqual(caught.exception.code, -32001)
        self.assertEqual(caught.exception.request_id, 1)
        self.assertEqual(caught.exception.data, {"retry": "caller decides"})
        self.assertEqual(caught.exception.received.receive_seq, 1)
        self.assertIsNone(client.request("software/explicit-next"))
        self.assertEqual([x["method"] for x in transport.writes], ["software/once", "software/explicit-next"])

    def test_unhandled_server_request_is_error_never_approval_real_pipes(self):
        client = self.peer("""
request = read()
emit({'id': 'server-approval', 'method': 'item/commandExecution/requestApproval', 'params': {}})
answer = read()
assert answer['id'] == 'server-approval' and answer['error']['code'] == -32601
assert 'result' not in answer
emit({'id': request['id'], 'result': {'denied': True}})
""")
        self.assertEqual(client.request("software/request"), {"denied": True})
        self.assertEqual(client.run_until(lambda _: True).message["id"], "server-approval")

    def test_timeout_keeps_id_and_wait_can_receive_without_resending_real_pipes(self):
        client = self.peer("""
request = read()
time.sleep(0.2)
emit({'id': request['id'], 'result': {'only_request': request}})
""")
        with self.assertRaises(app.AppServerTimeout) as caught:
            client.request("software/slow", timeout=0.05)
        self.assertEqual(caught.exception.request_id, 1)
        self.assertEqual(caught.exception.pending_ids, (1,))
        result = client.wait(1, timeout=2)
        self.assertEqual(result["only_request"]["id"], 1)
        self.assertEqual(client.pending_ids, ())
        with self.assertRaises(ValueError):
            client.wait(1)

    def test_one_deadline_covers_write_and_response(self):
        client, transport = self.client()
        clock = [100.0]
        def write(data, timeout):
            self.assertEqual(timeout, 10.0)
            clock[0] += 8
        def read(timeout):
            self.assertEqual(timeout, 2.0)
            raise app.AppServerTimeout("declared software deadline")
        transport.write_line, transport.read_line = write, read
        with mock.patch.object(app.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(app.AppServerTimeout) as caught:
                client.request("software/deadline", timeout=10)
        self.assertEqual(caught.exception.request_id, 1)
        self.assertEqual(transport.close_count, 0)

    def test_write_backpressure_is_bounded_and_connection_closed_real_pipes(self):
        client = self.peer("time.sleep(30)\n")
        proc = client.transport.process
        started = time.monotonic()
        with self.assertRaises(app.AppServerTimeout) as caught:
            client.request("software/large-write", {"text": "x" * (2 * 1024 * 1024)}, timeout=0.15)
        self.assertEqual(caught.exception.request_id, 1)
        self.assertEqual(caught.exception.pending_ids, (1,))
        self.assertLess(time.monotonic() - started, 3)
        self.assertIsNotNone(proc.poll())
        with self.assertRaises(app.AppServerDisconnected):
            client.wait(1)

    def test_stderr_backpressure_drained_and_tail_bounded_real_pipes(self):
        client = self.peer("""
request = read()
sys.stderr.buffer.write(b'X' * 250000 + b'LAST-FIXTURE-STDERR')
sys.stderr.buffer.flush()
emit({'id': request['id'], 'result': True})
""")
        self.assertTrue(client.request("software/stderr", timeout=3))
        tail = client.transport.stderr_tail
        self.assertEqual(len(tail), 65536)
        self.assertTrue(tail.endswith(b"LAST-FIXTURE-STDERR"))

    def test_disconnect_retains_pending_ids_and_reaps_child_real_pipes(self):
        client = self.peer("read()\nsys.exit(7)\n")
        proc = client.transport.process
        with self.assertRaises(app.AppServerDisconnected) as caught:
            client.request("software/disconnect")
        self.assertEqual(caught.exception.pending_ids, (1,))
        self.assertEqual(proc.returncode, 7)
        self.assertTrue(proc.stdin.closed and proc.stdout.closed and proc.stderr.closed)

    def test_partial_json_on_disconnect_is_fatal_real_pipes(self):
        client = self.peer("read()\nos.write(1, b'{\"id\":1,\"result\":true}')\n")
        with self.assertRaisesRegex(app.AppServerProtocolError, "partial"):
            client.request("software/partial")
        self.assertIsNotNone(client.transport.process.returncode)

    def test_close_escalates_only_own_direct_child_real_pipes(self):
        other = subprocess.Popen([sys.executable, "-I", "-S", "-B", "-c", "import time; time.sleep(30)"],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (other.kill(), other.wait()) if other.poll() is None else None)
        transport = app.StdioTransport([sys.executable, "-I", "-S", "-B", "-u", "-c", PEER + """
import signal
signal.signal(signal.SIGTERM, signal.SIG_IGN)
request = read()
emit({'id': request['id'], 'result': 'ready'})
time.sleep(30)
"""], cleanup_timeout=0.05)
        client = app.AppServerClient(transport=transport).start()
        self.addCleanup(client.close)
        self.assertEqual(client.request("software/ready"), "ready")
        try:
            client.close()
        except app.AppServerCleanupError as exc:
            # A scheduler may delay post-SIGKILL reaping past the unchanged
            # 50ms cleanup budget. It must be typed/visible and permit a later
            # owned wait; deterministic fakeProc controls cover that branch.
            self.assertTrue(client.cleanup_pending)
            self.assertIs(client.cleanup_error, exc)
            transport.process.wait(timeout=2)
            client.close()
        self.assertFalse(client.cleanup_pending)
        self.assertLess(transport.process.returncode, 0)
        self.assertIsNone(other.poll())

    def test_inherited_proxy_environment_and_cwd_are_unchanged_real_pipes(self):
        keys = ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy", "NO_PROXY", "no_proxy"]
        with tempfile.TemporaryDirectory(prefix="app-client-own-cwd-") as temp:
            client = self.peer("""
request = read()
emit({'id': request['id'], 'result': {'cwd': os.getcwd(), 'proxy': {key: os.environ.get(key) for key in request['params']['keys']}}})
""", cwd=temp)
            result = client.request("software/environment", {"keys": keys})
            self.assertEqual(result["cwd"], str(Path(temp).resolve()))
            self.assertEqual(result["proxy"], {key: os.environ.get(key) for key in keys})
            client.close()

    def test_malformed_frames_fail_closed_without_reply(self):
        frames = [b"[]\n", b"null\n", b"\n", b"not-json\n", b"\xff\n",
            b'{"id":1,"id":1,"result":true}\n', b'{"id":true,"result":1}\n',
            b'{"id":1,"result":1,"error":{"code":0,"message":"bad"}}\n',
            b'{"id":1,"error":{"code":true,"message":"bad"}}\n',
            b'{"id":1,"error":null}\n', b'{"id":1,"result":NaN}\n',
            b'{"id":1,"result":1e400}\n', b'{"id":1,"result":0,"jsonrpc":"1.0"}\n',
            b'{"method":"","params":{}}\n', b'{"method":"x","id":null}\n',
            b'{"method":"x","result":1}\n', b'{"id":1}\n',
            b'{"id":1,"result":0}', b'{"id":1,"result":0}\ntrailing\n',
        ]
        # The last buffer is not a single JSON object, even though it ends in LF.
        for raw in frames:
            with self.subTest(raw=raw):
                client, transport = self.client([raw])
                with self.assertRaises(app.AppServerProtocolError):
                    client.request("software/bad")
                self.assertEqual(transport.close_count, 1)
                self.assertEqual(len(transport.writes), 1)

    def test_unsolicited_string_and_duplicate_response_ids_are_fatal(self):
        for response in ({"id": 99, "result": 1}, {"id": "1", "result": 1}):
            client, transport = self.client([response])
            with self.assertRaises(app.AppServerProtocolError):
                client.request("software/unknown-id")
            self.assertEqual(transport.close_count, 1)
        client, _ = self.client([{"id": 1, "result": 1}, {"id": 1, "result": 2}])
        client.send("software/one")
        second = client.send("software/two")
        with self.assertRaises(app.AppServerProtocolError):
            client.wait(second)

    def test_oversize_without_newline_is_refused_before_eof_real_pipes(self):
        client = self.peer("read()\nos.write(1, b'x' * 128)\ntime.sleep(30)\n", max_message_bytes=64)
        with self.assertRaisesRegex(app.AppServerFrameTooLarge, "bound") as caught:
            client.request("x", timeout=2)
        self.assertEqual(caught.exception.failure_code, "INBOUND_FRAME_LIMIT_BEFORE_DISPATCH")
        self.assertEqual(caught.exception.pending_ids, (1,))
        self.assertIsNotNone(client.transport.process.returncode)

    def test_buffered_unterminated_oversize_after_complete_frame_fails_before_eof(self):
        notices = []
        client = self.peer("""
read()
os.write(1, b'{"method":"notice"}\\n' + b'x' * 64)
time.sleep(30)
""", max_inbound_message_bytes=64, on_notification=notices.append)
        with self.assertRaisesRegex(app.AppServerProtocolError, "bound"):
            client.request("x", timeout=2)
        self.assertEqual([row.message["method"] for row in notices], ["notice"])
        self.assertIsNotNone(client.transport.process.returncode)

    def test_complete_inbound_frame_cap_includes_lf_and_never_skips_oversize(self):
        notices, calls = [], []
        exact = line({"method": "notice"})
        client, transport = self.client([exact, exact[:-1] + b" \n", {"id": 7, "method": "item/tool/call"}],
            max_inbound_message_bytes=len(exact), on_notification=notices.append, on_server_request=calls.append)
        with self.assertRaises(app.AppServerFrameTooLarge) as caught:
            client.request("x")
        self.assertEqual(caught.exception.failure_code, "INBOUND_FRAME_LIMIT_BEFORE_DISPATCH")
        self.assertEqual([row.receive_seq for row in notices], [1])
        self.assertEqual(calls, [])
        self.assertEqual(transport.read_count, 2)
        self.assertEqual(transport.close_count, 1)

    def test_wider_inbound_limit_preserves_request_and_write_bounds(self):
        calls = []
        call = {"id": "server", "method": "item/tool/call", "params": {"text": "x" * 100}}
        self.assertTrue(64 < len(line(call)) < 256)
        client, transport = self.client([call, {"id": 2, "result": True}], max_message_bytes=64,
            max_inbound_message_bytes=256, on_server_request=calls.append)
        with self.assertRaisesRegex(ValueError, "outbound"):
            client.send("large", call["params"])
        self.assertEqual(transport.writes, [])
        self.assertEqual(client.pending_ids, ())
        with self.assertRaisesRegex(app.AppServerProtocolError, "request exceeded bound"):
            client.request("x")
        self.assertEqual(calls, [])
        self.assertEqual(transport.read_count, 1)
        self.assertEqual(transport.close_count, 1)
        self.assertEqual(transport.writes, [{"id": 2, "method": "x", "params": {}}])

    def test_invalid_local_objects_are_never_sent_or_left_pending(self):
        client, transport = self.client()
        for value in (float("nan"), float("inf"), {"set": {1, 2}}, "\ud800"):
            with self.subTest(type=type(value).__name__):
                with self.assertRaises(ValueError):
                    client.request("software/local", value)
        self.assertEqual(transport.writes, [])
        self.assertEqual(client.pending_ids, ())
        self.assertEqual(transport.close_count, 0)
        for bad in ("", 1, None, False):
            with self.assertRaises(ValueError):
                client.notify(bad)
        # A transport ValueError may occur after bytes escaped. Preserve that
        # request as unknown instead of treating it as unsent local JSON.
        other, output = self.client()
        def partial_write(data, timeout):
            output.writes.append(json.loads(data))
            raise ValueError("declared partial transport failure")
        output.write_line = partial_write
        with self.assertRaises(app.AppServerDisconnected) as caught:
            other.request("software/partial-write")
        self.assertEqual(caught.exception.pending_ids, (1,))
        self.assertEqual(other.pending_ids, (1,))
        self.assertEqual(output.close_count, 1)
        self.assertEqual(len(output.writes), 1)

    def test_event_and_outstanding_bounds_never_silently_drop(self):
        events = [{"method": "software/event", "params": {"index": i}} for i in range(3)]
        seen = []
        client, transport = self.client(events, max_events=2, on_notification=seen.append)
        with self.assertRaisesRegex(app.AppServerProtocolError, "queue"):
            client.request("software/overflow")
        self.assertEqual([x.receive_seq for x in seen], [1, 2])
        self.assertEqual(transport.close_count, 1)
        other, output = self.client(max_pending=2)
        other.send("software/one")
        other.send("software/two")
        with self.assertRaisesRegex(app.AppServerProtocolError, "outstanding"):
            other.send("software/three")
        self.assertEqual(len(output.writes), 2)

    def test_callback_exception_sends_error_and_closes_not_success(self):
        def handler(_):
            raise RuntimeError("local private fixture detail")
        client, transport = self.client([{"id": "server", "method": "software/call", "params": {}}],
                                        on_server_request=handler)
        with self.assertRaises(app.AppServerCallbackError) as caught:
            client.request("software/outer")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual((caught.exception.callback_method, caught.exception.receive_seq,
            caught.exception.callback_kind, caught.exception.stage), ("software/call", 1, "request", "handler"))
        self.assertFalse(hasattr(caught.exception, "received"))
        self.assertEqual(transport.writes[-1], {"id": "server", "error": {"code": -32603, "message": "Client callback failed"}})
        self.assertEqual(transport.close_count, 1)

    def test_invalid_callback_result_closes(self):
        client, transport = self.client([{"id": 7, "method": "software/call", "params": {}}],
                                       on_server_request=lambda _: float("nan"))
        with self.assertRaises(app.AppServerCallbackError) as caught:
            client.request("software/outer")
        self.assertEqual(caught.exception.stage, "result_encoding")
        self.assertEqual(caught.exception.callback_method, "software/call")
        self.assertEqual(transport.close_count, 1)
        self.assertEqual(len(transport.writes), 1)

    def test_callback_failure_reply_error_preserves_original_handler_cause(self):
        def handler(_):
            raise RuntimeError("original handler failure")
        client, transport = self.client([{"id": "server", "method": "software/call", "params": {}}],
            on_server_request=handler)
        write = transport.write_line
        def fail_reply(data, timeout):
            message = json.loads(data)
            if "error" in message:
                raise app.AppServerDisconnected("failed callback error reply")
            return write(data, timeout)
        transport.write_line = fail_reply
        with self.assertRaises(app.AppServerCallbackError) as caught:
            client.request("software/outer")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual(str(caught.exception.__cause__), "original handler failure")
        self.assertIsInstance(caught.exception.reply_error, app.AppServerDisconnected)
        self.assertEqual(caught.exception.callback_method, "software/call")

    def test_deferred_callback_failure_uses_failed_arrival_not_latest_response(self):
        def fail(_):
            raise RuntimeError("deferred callback failed")
        client, _ = self.client([{"method": "software/deferred", "params": {}},
            {"id": 1, "result": True}], on_notification=fail)
        client.pause_callbacks()
        self.assertTrue(client.request("software/lookup"))
        self.assertEqual(client.last_received.receive_seq, 2)
        with self.assertRaises(app.AppServerCallbackError) as caught:
            client.resume_callbacks()
        self.assertEqual((caught.exception.callback_method, caught.exception.receive_seq,
            caught.exception.callback_kind), ("software/deferred", 1, "notification"))

    def test_callback_nested_request_is_refused_before_second_send(self):
        client, transport = self.client([{"method": "software/event", "params": {}}])
        client.on_notification = lambda _: client.request("software/nested")
        with self.assertRaises(app.AppServerCallbackError) as caught:
            client.request("software/outer")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual([x["method"] for x in transport.writes], ["software/outer"])

    def test_timeout_arguments_and_transport_selection_are_strict(self):
        for value in (0, -1, True, float("nan"), float("inf"), 10 ** 400, "1"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    app.AppServerClient(transport=MemoryTransport(), default_timeout=value)
        for kwargs in ({}, {"argv": ["inert"], "transport": MemoryTransport()}):
            with self.assertRaises(ValueError):
                app.AppServerClient(**kwargs)
        for argv in ("inert", b"inert"):
            with self.assertRaises(ValueError):
                app.AppServerClient(argv)
        for invalid_limit in (1, -1, True, 2.0, "128"):
            with self.assertRaisesRegex(ValueError, "max_inbound_message_bytes"):
                app.AppServerClient(transport=MemoryTransport(), max_inbound_message_bytes=invalid_limit)
        client, transport = self.client()
        for value in (0, -1, True, float("inf")):
            with self.assertRaises(ValueError):
                client.request("software/invalid-timeout", timeout=value)
        self.assertEqual(transport.writes, [])


class AppServerPauseTests(unittest.TestCase):
    """Software-only pause/outer lookup ordering, never native admission proof."""
    client = AppServerClientTests.client
    peer = AppServerClientTests.peer

    def test_pause_preserves_pending_id_and_original_callback_order(self):
        notifications, requests = [], []
        client, transport = self.client([
            {"method": "software/unknown", "params": {}},
            {"method": "software/status", "params": {}},
            {"id": "server", "method": "software/call", "params": {}},
            {"id": 1, "result": {"old": True}},
            {"id": 2, "result": {"metadata": "software-only"}},
            {"method": "software/done", "params": {}},
        ])
        def notification(row):
            notifications.append(row.receive_seq)
            if row.receive_seq == 1:
                client.pause_callbacks()
        client.on_notification = notification
        client.on_server_request = lambda row: requests.append(row.receive_seq) or {"fixture": True}
        with self.assertRaises(app.AppServerDeliveryPaused) as caught:
            client.request("software/original")
        self.assertEqual(caught.exception.received.receive_seq, 1)
        self.assertEqual(caught.exception.request_id, 1)
        self.assertEqual(caught.exception.pending_ids, (1,))
        self.assertEqual(transport.close_count, 0)
        metadata = client.send("thread/read", {"threadId": "software-child", "includeTurns": False})
        self.assertEqual(client.wait(metadata), {"metadata": "software-only"})
        self.assertEqual(client.response_received(metadata).receive_seq, 5)
        self.assertEqual(client.pending_ids, (1,))
        self.assertEqual(notifications, [1])
        self.assertEqual(requests, [])
        self.assertEqual(len(transport.writes), 2)  # No deferred server reply.
        reads = transport.read_count
        client.resume_callbacks()
        self.assertEqual(transport.read_count, reads)  # Resume never reads.
        self.assertEqual(client.last_received.receive_seq, 5)
        self.assertEqual(notifications, [1, 2])
        self.assertEqual(requests, [3])
        self.assertEqual(transport.writes[-1], {"id": "server", "result": {"fixture": True}})
        self.assertEqual(client.wait(1), {"old": True})
        self.assertEqual(len(transport.writes), 3)  # Original request never resent.
        self.assertEqual(client.run_until(lambda row: row.message["method"] == "software/done").receive_seq, 6)
        self.assertEqual(notifications, [1, 2, 6])

    def test_early_business_callback_keeps_old_seq_and_is_refused_by_caller(self):
        client, transport = self.client([
            {"method": "software/unknown", "params": {}},
            {"id": "early", "method": "item/tool/call", "params": {}},
            {"id": 1, "result": {}}, {"id": 2, "result": {}},
        ])
        client.on_notification = lambda row: client.pause_callbacks()
        metadata_seq, dispatch = [None], []
        def caller_policy(row):
            if row.receive_seq <= metadata_seq[0]:
                raise RuntimeError("software policy: metadata cannot authorize an earlier callback")
            dispatch.append(row)
            return {"success": True}
        client.on_server_request = caller_policy
        with self.assertRaises(app.AppServerDeliveryPaused):
            client.request("software/original")
        meta = client.send("thread/read", {"threadId": "software-child", "includeTurns": False})
        client.wait(meta)
        metadata_seq[0] = client.response_received(meta).receive_seq
        self.assertEqual(metadata_seq[0], 4)
        with self.assertRaises(app.AppServerCallbackError):
            client.resume_callbacks()
        self.assertEqual(dispatch, [])
        self.assertEqual(transport.writes[-1]["id"], "early")
        self.assertEqual(transport.writes[-1]["error"]["code"], -32603)
        self.assertEqual(client.pending_ids, (1,))

    def test_pause_buffer_count_and_total_bytes_bounds_fail_closed(self):
        notices = [{"method": "software/event", "params": {"i": i}} for i in range(3)]
        seen = []
        client, transport = self.client(notices, max_events=2, on_notification=seen.append)
        client.pause_callbacks()
        original = client.send("software/pending")
        # Consuming the observer queue must not bypass the deferred queue bound.
        for expected in (1, 2):
            self.assertEqual(client.run_until(lambda _: True).receive_seq, expected)
        with self.assertRaises(app.AppServerProtocolError):
            client.run_until(lambda _: True)
        self.assertEqual(client.pending_ids, (original,))
        self.assertEqual(seen, [])
        self.assertEqual(transport.close_count, 1)
        large = {"id": "server", "method": "software/call", "params": {"text": "x" * 100}}
        self.assertLess(len(line(large)), 256)
        self.assertGreater(2 * len(line(large)), 256)
        other, output = self.client([large, large], max_message_bytes=256,
            max_inbound_message_bytes=1024, on_server_request=seen.append)
        other.pause_callbacks()
        with self.assertRaisesRegex(app.AppServerProtocolError, "buffer") as caught:
            other.request("software/pending")
        self.assertEqual(caught.exception.request_id, 1)
        self.assertEqual(caught.exception.pending_ids, (1,))
        self.assertEqual(seen, [])
        self.assertEqual(len(output.writes), 1)
        self.assertEqual(output.close_count, 1)

    def test_paused_lookup_timeout_keeps_ids_and_no_automatic_retry(self):
        client, transport = self.client()
        rows = deque([line({"method": "software/event", "params": {}}),
                      app.AppServerTimeout("software delayed metadata"), line({"id": 1, "result": {}})])
        def read(timeout):
            transport.read_count += 1
            value = rows.popleft()
            if isinstance(value, Exception):
                raise value
            return value
        transport.read_line = read
        callbacks = []
        client.on_notification = callbacks.append
        client.pause_callbacks()
        meta = client.send("thread/read", {"threadId": "software-child", "includeTurns": False})
        with self.assertRaises(app.AppServerTimeout) as caught:
            client.wait(meta)
        self.assertEqual(caught.exception.request_id, meta)
        self.assertEqual(caught.exception.pending_ids, (meta,))
        self.assertTrue(client.callbacks_paused)
        self.assertEqual(transport.close_count, 0)
        self.assertEqual(callbacks, [])
        self.assertEqual(client.wait(meta), {})
        client.resume_callbacks()
        self.assertEqual([row.receive_seq for row in callbacks], [1])
        self.assertEqual(len(transport.writes), 1)

    def test_wrong_or_duplicate_lookup_response_is_fatal_while_paused(self):
        for rows in ([{"id": 99, "result": {}}],
                     [{"id": 2, "result": {}}, {"id": 2, "result": {}}]):
            with self.subTest(rows=rows):
                client, transport = self.client(rows)
                client.pause_callbacks()
                client.send("software/original")
                client.send("thread/read", {"threadId": "software-child", "includeTurns": False})
                with self.assertRaises(app.AppServerProtocolError) as caught:
                    client.wait(1)
                self.assertEqual(caught.exception.pending_ids, (1, 2))
                self.assertEqual(transport.close_count, 1)
                self.assertEqual(len(transport.writes), 2)

    def test_second_pause_during_resume_preserves_remaining_buffer_position(self):
        seen = []
        client, transport = self.client([
            *[{"method": "software/event", "params": {}} for _ in range(4)],
            {"id": 1, "result": "original"}, {"id": 2, "result": {}}, {"id": 3, "result": {}},
        ])
        def handler(row):
            seen.append(row.receive_seq)
            if row.receive_seq in (1, 3):
                client.pause_callbacks()
        client.on_notification = handler
        with self.assertRaises(app.AppServerDeliveryPaused):
            client.request("software/original")
        client.request("thread/read", {"threadId": "software-child-one", "includeTurns": False})
        reads = transport.read_count
        with self.assertRaises(app.AppServerDeliveryPaused) as caught:
            client.resume_callbacks()
        self.assertEqual(caught.exception.received.receive_seq, 3)
        self.assertEqual(caught.exception.pending_ids, (1,))
        self.assertEqual(seen, [1, 2, 3])
        self.assertEqual(transport.read_count, reads)
        client.request("thread/read", {"threadId": "software-child-two", "includeTurns": False})
        reads = transport.read_count
        client.resume_callbacks()
        self.assertEqual(transport.read_count, reads)
        self.assertEqual(seen, [1, 2, 3, 4])
        self.assertEqual(client.last_received.receive_seq, 7)
        self.assertEqual(client.wait(1), "original")
        self.assertEqual(len(transport.writes), 3)

    def test_resume_timeout_keeps_remaining_events_paused_without_duplicate_delivery(self):
        client, transport = self.client([
            {"method": "software/event", "params": {}}, {"method": "software/event", "params": {}},
            {"id": 1, "result": {}},
        ])
        seen, clock = [], [100.0]
        def handler(row):
            seen.append(row.receive_seq)
            if row.receive_seq == 1:
                clock[0] += 2
        client.on_notification = handler
        client.pause_callbacks()
        client.request("software/explicit-lookup")
        reads = transport.read_count
        with mock.patch.object(app.time, "monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(app.AppServerTimeout):
                client.resume_callbacks(timeout=1)
            self.assertTrue(client.callbacks_paused)
            self.assertEqual(seen, [1])
            client.resume_callbacks(timeout=1)
        self.assertEqual(seen, [1, 2])
        self.assertFalse(client.callbacks_paused)
        self.assertEqual(transport.read_count, reads)
        self.assertEqual(client.last_received.receive_seq, 3)

    def test_resume_inside_callback_keeps_nested_read_guard(self):
        client, transport = self.client([{"method": "software/event", "params": {}}])
        def handler(row):
            client.pause_callbacks()
            client.resume_callbacks()
        client.on_notification = handler
        with self.assertRaises(app.AppServerCallbackError) as caught:
            client.request("software/original")
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertEqual(transport.read_count, 1)
        self.assertEqual(len(transport.writes), 1)

    def test_outer_lookup_and_future_callback_real_owned_pipes(self):
        notifications, calls, metadata_seq = [], [], [None]
        def notification(row):
            notifications.append(row.receive_seq)
            if row.receive_seq == 1:
                client.pause_callbacks()
        def server_request(row):
            self.assertGreater(row.receive_seq, metadata_seq[0])
            calls.append(row.receive_seq)
            return {"success": True, "fixture": "no product dispatch"}
        client = self.peer("""
original = read()
emit({'method': 'software/unknown', 'params': {}})
metadata = read()
assert metadata['method'] == 'thread/read' and metadata['params']['includeTurns'] is False
emit({'method': 'software/status', 'params': {}})
emit({'id': original['id'], 'result': 'original-once'})
emit({'id': metadata['id'], 'result': {'thread': {'id': 'software-child'}}})
emit({'id': 'after-metadata', 'method': 'item/tool/call', 'params': {}})
answer = read()
assert answer['id'] == 'after-metadata' and answer['result']['success'] is True
emit({'method': 'software/done', 'params': {}})
""", on_notification=notification, on_server_request=server_request)
        with self.assertRaises(app.AppServerDeliveryPaused) as caught:
            client.request("software/original")
        self.assertEqual(caught.exception.request_id, 1)
        meta = client.send("thread/read", {"threadId": "software-child", "includeTurns": False})
        self.assertEqual(client.wait(meta), {"thread": {"id": "software-child"}})
        metadata_seq[0] = client.response_received(meta).receive_seq
        self.assertEqual(metadata_seq[0], 4)
        client.resume_callbacks()
        self.assertEqual(notifications, [1, 2])
        self.assertEqual(client.last_received.receive_seq, 4)
        self.assertEqual(client.wait(1), "original-once")
        client.run_until(lambda row: row.message["method"] == "software/done")
        self.assertEqual(calls, [5])
        self.assertEqual(notifications, [1, 2, 6])


class AppServerCleanupTests(unittest.TestCase):
    """AS-CLEANUP-01 controls; fakeProc fixtures do not attest a live Host."""
    @staticmethod
    def expired():
        return subprocess.TimeoutExpired("owned-software-fixture", 0.05)

    def test_cleanup_failure_is_typed_and_next_close_only_reaps_owned_process(self):
        transport = fake_owned_transport(self.expired(), self.expired(), -9)
        try:
            transport.close()
        except Exception as exc:
            first = exc
        else:
            self.fail("the first cleanup must disclose uncertainty")
        self.assertIsInstance(first, app.AppServerError)
        self.assertEqual(type(first).__name__, "AppServerCleanupError")
        self.assertIsInstance(first.__cause__, subprocess.TimeoutExpired)
        self.assertIs(transport.cleanup_error, first)
        self.assertTrue(transport.cleanup_pending)
        self.assertTrue(all(stream.closed for stream in (
            transport.process.stdin, transport.process.stdout, transport.process.stderr)))
        transport.close()
        self.assertFalse(transport.cleanup_pending)
        self.assertIsNone(transport.cleanup_error)
        self.assertEqual(transport.process.calls.count("terminate"), 1)
        self.assertEqual(transport.process.calls.count("kill"), 1)
        self.assertEqual([x for x in transport.process.calls if isinstance(x, tuple)], [("wait", 0.05)] * 3)
        calls = list(transport.process.calls)
        transport.close()
        self.assertEqual(transport.process.calls, calls)

    def test_repeated_cleanup_is_bounded_without_repeated_signals(self):
        transport = fake_owned_transport(self.expired(), self.expired(), self.expired(), -9)
        failures = []
        for expected_waits in (2, 3):
            try:
                transport.close()
            except Exception as exc:
                failures.append(exc)
            self.assertTrue(transport.cleanup_pending)
            self.assertEqual(len([x for x in transport.process.calls if isinstance(x, tuple)]), expected_waits)
        transport.close()
        self.assertFalse(transport.cleanup_pending)
        self.assertEqual(len(failures), 2)
        self.assertTrue(all(isinstance(exc, app.AppServerError) for exc in failures))
        self.assertEqual(transport.process.calls.count("terminate"), 1)
        self.assertEqual(transport.process.calls.count("kill"), 1)

    def test_write_primary_object_ids_and_no_retry_survive_cleanup_failure(self):
        for error_class in (app.AppServerTimeout, app.AppServerDisconnected, app.AppServerProtocolError):
            with self.subTest(error_class=error_class.__name__):
                transport = fake_owned_transport(self.expired(), self.expired(), -9)
                primary = error_class("declared ambiguous write")
                writes = []
                def write(data, timeout):
                    writes.append(data)
                    raise primary
                transport.write_line = write
                client = app.AppServerClient(transport=transport).start()
                try:
                    client.request("software/once")
                except Exception as exc:
                    received = exc
                self.assertIs(received, primary)
                self.assertEqual(received.request_id, 1)
                self.assertEqual(received.pending_ids, (1,))
                self.assertEqual(client.pending_ids, (1,))
                self.assertEqual(len(writes), 1)
                self.assertIs(received.cleanup_error, client.cleanup_error)
                self.assertTrue(client.cleanup_pending)
                client.close()
                self.assertFalse(client.cleanup_pending)
                with self.assertRaises(app.AppServerDisconnected):
                    client.start()
                self.assertEqual(len(writes), 1)

    def test_read_primary_object_ids_survive_cleanup_failure(self):
        transport = fake_owned_transport(self.expired(), self.expired(), -9)
        primary = app.AppServerDisconnected("declared fixture EOF")
        writes = []
        transport.write_line = lambda data, timeout: writes.append(data)
        def read(timeout):
            raise primary
        transport.read_line = read
        client = app.AppServerClient(transport=transport).start()
        try:
            client.request("software/once")
        except Exception as exc:
            received = exc
        self.assertIs(received, primary)
        self.assertEqual(received.request_id, 1)
        self.assertEqual(received.pending_ids, (1,))
        self.assertEqual(len(writes), 1)
        self.assertIs(received.cleanup_error, client.cleanup_error)
        client.close()
        self.assertFalse(client.cleanup_pending)

    def test_context_exit_preserves_primary_even_when_cleanup_again_times_out(self):
        transport = fake_owned_transport(self.expired(), self.expired(), self.expired(), -9)
        primary = app.AppServerTimeout("declared ambiguous write")
        writes = []
        def write(data, timeout):
            writes.append(data)
            raise primary
        transport.write_line = write
        client = app.AppServerClient(transport=transport)
        try:
            with client:
                client.request("software/once")
        except Exception as exc:
            received = exc
        self.assertIs(received, primary)
        self.assertEqual(received.request_id, 1)
        self.assertEqual(received.pending_ids, (1,))
        self.assertTrue(client.cleanup_pending)
        self.assertIsInstance(client.cleanup_error, app.AppServerError)
        self.assertEqual(len(writes), 1)
        client.close()
        self.assertFalse(client.cleanup_pending)

    def test_explicit_client_close_reports_cleanup_failure_and_allows_bounded_reap(self):
        transport = fake_owned_transport(self.expired(), self.expired(), -9)
        client = app.AppServerClient(transport=transport).start()
        try:
            client.close()
        except Exception as exc:
            received = exc
        self.assertIsInstance(received, app.AppServerError)
        self.assertEqual(type(received).__name__, "AppServerCleanupError")
        self.assertTrue(client.cleanup_pending)
        self.assertIs(client.cleanup_error, received)
        client.close()
        self.assertFalse(client.cleanup_pending)


if __name__ == "__main__":
    unittest.main()
