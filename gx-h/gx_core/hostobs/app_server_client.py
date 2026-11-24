"""Synchronous stdlib client for Codex App Server's JSONL stdio protocol.

No handshake, thread, turn, retry, model selection or authority is implicit.
Callers own those policies. Callbacks run synchronously and must return promptly;
they may send but must not perform nested reads. A timeout does not cancel work.
Only the subprocess created by ``StdioTransport`` is signalled during cleanup.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import errno
import json
import math
import os
import selectors
import subprocess
import time
from typing import Any, Protocol


# A GX handler returns this only after a terminal dynamic call is committed.
# Codex then cancels its pending request as part of the exact Turn's interrupt.
# A normal None is still a JSON null response.
NO_TOOL_RESPONSE = object()


class AppServerError(Exception):
    """Base protocol/transport error; never a business outcome or retry grant."""

    cleanup_error: AppServerCleanupError | None = None


class AppServerCleanupError(AppServerError):
    """Owned resources remain uncertain; a subsequent close may retry reaping."""


class AppServerTimeout(AppServerError, TimeoutError):
    def __init__(self, message: str, *, request_id: int | None = None,
                 pending_ids: tuple[int, ...] = ()):
        super().__init__(message)
        self.request_id = request_id
        self.pending_ids = pending_ids


class AppServerDisconnected(AppServerError):
    def __init__(self, message: str, *, pending_ids: tuple[int, ...] = ()):
        super().__init__(message)
        self.pending_ids = pending_ids


class AppServerProtocolError(AppServerError):
    pass


class AppServerFrameTooLarge(AppServerProtocolError):
    """The stdio transport cannot buffer a complete JSON line."""


class AppServerCallbackError(AppServerError):
    """Callback failure with arrival metadata only; never retain its frame/body."""

    def __init__(self, message: str, *, callback_method: str | None = None,
                 receive_seq: int | None = None, callback_kind: str | None = None,
                 stage: str | None = None):
        super().__init__(message)
        self.callback_method = callback_method
        self.receive_seq = receive_seq
        self.callback_kind = callback_kind
        self.stage = stage


@dataclass(frozen=True)
class ReceivedMessage:
    """A wire object plus local arrival order; callers must not mutate message."""

    receive_seq: int
    message: dict[str, Any]


class AppServerDeliveryPaused(AppServerError):
    """Yield to the caller without closing, cancelling or resending a request."""

    def __init__(self, received: ReceivedMessage, *, pending_ids: tuple[int, ...] = ()):
        super().__init__("App Server callback delivery paused")
        self.received = received
        self.request_id = None
        self.pending_ids = pending_ids


class AppServerResponseError(AppServerError):
    def __init__(self, received: ReceivedMessage):
        error = received.message["error"]
        super().__init__(f"App Server error {error['code']}: {error['message']}")
        self.code = error["code"]
        self.message = error["message"]
        self.data = error.get("data")
        self.request_id = received.message["id"]
        self.received = received


class Transport(Protocol):
    """One complete line per operation, including its newline.

    timeout is a positive remaining duration, not a fresh per-chunk deadline.
    read_line raises AppServerDisconnected at EOF, AppServerTimeout on expiry.
    close must be idempotent and release only resources owned by this transport.
    The client takes ownership of an injected transport after start().
    """

    def write_line(self, line: bytes, timeout: float) -> None: ...
    def read_line(self, timeout: float) -> bytes: ...
    def close(self) -> None: ...


def _duration(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("timeout must be a positive finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError("timeout must be a positive finite number") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError("timeout must be a positive finite number")
    return result


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AppServerTimeout("App Server operation timed out")
    return remaining


class StdioTransport:
    """Own one explicitly supplied command, with bounded nonblocking pipe IO.

    stderr is drained concurrently with pipe IO and retained only as a bounded
    byte tail. It is never parsed as protocol or printed. No shell is used, no
    environment variables are rewritten, and no existing process is attached.
    close terminates/reaps the direct child; it does not claim descendant custody.
    """

    def __init__(self, argv: Sequence[str], *, cwd: str | os.PathLike | None = None,
                 env: Mapping[str, str] | None = None,
                 max_message_bytes: int = 16 * 1024 * 1024,
                 stderr_limit: int = 64 * 1024, cleanup_timeout: float = 1.0):
        if isinstance(argv, (str, bytes)) or not argv or any(
                not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
            raise ValueError("argv must be a nonempty sequence of nonempty strings")
        if type(max_message_bytes) is not int or max_message_bytes < 2:
            raise ValueError("max_message_bytes must be an integer >= 2")
        if type(stderr_limit) is not int or stderr_limit < 0:
            raise ValueError("stderr_limit must be a nonnegative integer")
        self.max_message_bytes = max_message_bytes
        self.stderr_limit = stderr_limit
        self.cleanup_timeout = _duration(cleanup_timeout)
        self._stderr = bytearray()
        self._stdout = bytearray()
        self._stdout_scan_from = 0
        self._eof = False
        self._closed = False
        self._cleanup_complete = False
        self.cleanup_error: AppServerCleanupError | None = None
        self._selector = selectors.DefaultSelector()
        try:
            self.process = subprocess.Popen(list(argv), cwd=cwd, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=0, close_fds=True, start_new_session=True)
            for stream, name in ((self.process.stdout, "stdout"), (self.process.stderr, "stderr")):
                os.set_blocking(stream.fileno(), False)
                self._selector.register(stream, selectors.EVENT_READ, name)
            os.set_blocking(self.process.stdin.fileno(), False)
        except BaseException as primary:
            try:
                self.close()
            except Exception as cleanup:
                primary.cleanup_error = cleanup
                primary.add_note("Owned App Server cleanup also failed")
            raise

    @property
    def stderr_tail(self) -> bytes:
        return bytes(self._stderr)

    @property
    def read_fds(self) -> tuple[int, ...]:
        """Expose readiness to a shared Driver. The transport still owns all pipe reads."""
        return tuple(key.fd for key in self._selector.get_map().values() if key.events & selectors.EVENT_READ)

    @property
    def cleanup_pending(self) -> bool:
        return self._closed and not self._cleanup_complete

    def _first_newline(self) -> int:
        # Bytes before this offset have already been searched, including on a
        # previous write or timed-out read. A complete frame includes its LF.
        index = self._stdout.find(b"\n", self._stdout_scan_from)
        self._stdout_scan_from = len(self._stdout) if index < 0 else index
        if self._stdout_scan_from >= self.max_message_bytes:
            raise AppServerFrameTooLarge("App Server message exceeded bound")
        return index

    def _io(self, deadline: float) -> bool:
        events = self._selector.select(_remaining(deadline))
        if not events:
            raise AppServerTimeout("App Server pipe IO timed out")
        writable = False
        for key, _ in events:
            if key.data == "stdin":
                writable = True
                continue
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if not chunk:
                self._selector.unregister(key.fileobj)
                if key.data == "stdout":
                    self._eof = True
                continue
            if key.data == "stderr":
                if self.stderr_limit:
                    self._stderr.extend(chunk)
                    del self._stderr[:-self.stderr_limit]
            else:
                self._stdout.extend(chunk)
                # Writing while the server floods stdout must also stay bounded.
                if len(self._stdout) > self.max_message_bytes * 2:
                    raise AppServerProtocolError("App Server stdout buffer exceeded bound")
                self._first_newline()
        return writable

    def write_line(self, line: bytes, timeout: float) -> None:
        if self._closed:
            raise AppServerDisconnected("App Server transport is closed")
        deadline = time.monotonic() + _duration(timeout)
        stream = self.process.stdin
        self._selector.register(stream, selectors.EVENT_WRITE, "stdin")
        try:
            offset = 0
            while offset < len(line):
                _remaining(deadline)
                try:
                    written = os.write(stream.fileno(), memoryview(line)[offset:])
                except BlockingIOError:
                    self._io(deadline)
                    continue
                except OSError as exc:
                    if exc.errno in (errno.EPIPE, errno.EBADF):
                        raise AppServerDisconnected("App Server stdin disconnected") from exc
                    raise
                if written <= 0:
                    raise AppServerDisconnected("App Server stdin disconnected")
                offset += written
        finally:
            self._selector.unregister(stream)

    def read_line(self, timeout: float) -> bytes:
        if self._closed:
            raise AppServerDisconnected("App Server transport is closed")
        deadline = time.monotonic() + _duration(timeout)
        while True:
            index = self._first_newline()
            if index >= 0:
                line = bytes(memoryview(self._stdout)[:index + 1])
                del self._stdout[:index + 1]
                self._stdout_scan_from = 0
                return line
            if self._eof:
                if self._stdout:
                    raise AppServerProtocolError("App Server disconnected with a partial JSON line")
                raise AppServerDisconnected("App Server stdout disconnected")
            self._io(deadline)

    def close(self) -> None:
        if self._cleanup_complete:
            return
        reap_only = self._closed
        self._closed = True
        proc = getattr(self, "process", None)
        failure = None
        try:
            if proc is not None:
                if reap_only:
                    # A previous bounded cleanup failed. Do not re-signal or
                    # discover another PID; wait only on the same owned Popen.
                    proc.wait(timeout=self.cleanup_timeout)
                elif proc.poll() is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass  # It exited between poll and signal; still reap it.
                    try:
                        proc.wait(timeout=self.cleanup_timeout)
                    except subprocess.TimeoutExpired:
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        proc.wait(timeout=self.cleanup_timeout)
                else:
                    proc.wait(timeout=self.cleanup_timeout)
        except Exception as exc:
            failure = exc
        finally:
            resources = [self._selector]
            if proc is not None:
                resources.extend((proc.stdin, proc.stdout, proc.stderr))
            for resource in resources:
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as exc:
                        if failure is None:
                            failure = exc
        if failure is not None:
            self.cleanup_error = AppServerCleanupError("Owned App Server cleanup incomplete")
            raise self.cleanup_error from failure
        self._cleanup_complete = True
        self.cleanup_error = None


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("nonfinite JSON number")
    return value


def _valid_id(value: Any) -> bool:
    return type(value) in (int, str)


def _envelope(message: Any) -> None:
    if not isinstance(message, dict) or message.get("jsonrpc", "2.0") != "2.0":
        raise AppServerProtocolError("expected an App Server JSON object")
    if "method" in message:
        if not isinstance(message["method"], str) or not message["method"] or (
                "result" in message or "error" in message):
            raise AppServerProtocolError("invalid App Server request/notification")
        if "id" in message and not _valid_id(message["id"]):
            raise AppServerProtocolError("invalid App Server request id")
    else:
        if not _valid_id(message.get("id")) or ("result" in message) == ("error" in message):
            raise AppServerProtocolError("invalid App Server response")
        if "error" in message:
            error = message["error"]
            if not isinstance(error, dict) or type(error.get("code")) is not int or not isinstance(
                    error.get("message"), str):
                raise AppServerProtocolError("invalid App Server error response")


class AppServerClient:
    """Single-threaded request/response multiplexing with a retained event queue.

    Supply argv OR an already-open Transport, then call start (or use ``with``).
    A supplied env is forwarded exactly; inheriting env preserves proxy settings.
    Every received object has a per-client receive_seq starting at 1. Callbacks
    fire once on delivery (normally receipt; explicit pause may defer delivery).
    run_until consumes queued observations without replaying their callbacks.
    Requests use monotonically increasing integer ids in their own direction.
    Timeout leaves a pending read retryable with wait(the_same_id), never resends.
    A write failure closes the connection because a partial frame is ambiguous.
    max_message_bytes bounds outbound messages, incoming server requests and
    total deferred callback bytes. Set max_inbound_message_bytes to permit larger
    notifications and responses. Both frame limits include the terminating LF.
    """

    def __init__(self, argv: Sequence[str] | None = None, *,
                 cwd: str | os.PathLike | None = None, env: Mapping[str, str] | None = None,
                 transport: Transport | None = None,
                 on_notification: Callable[[ReceivedMessage], None] | None = None,
                 on_server_request: Callable[[ReceivedMessage], Any] | None = None,
                 default_timeout: float = 30.0, max_message_bytes: int = 16 * 1024 * 1024,
                 max_inbound_message_bytes: int | None = None,
                 max_pending: int = 256, max_events: int = 4096):
        if (argv is None) == (transport is None):
            raise ValueError("supply exactly one of argv or transport")
        if isinstance(argv, (str, bytes)):
            raise ValueError("argv must be a sequence of arguments, not a string")
        if max_inbound_message_bytes is None:
            max_inbound_message_bytes = max_message_bytes
        for name, value in (("max_pending", max_pending), ("max_events", max_events),
                            ("max_message_bytes", max_message_bytes),
                            ("max_inbound_message_bytes", max_inbound_message_bytes)):
            if type(value) is not int or value < 2:
                raise ValueError(f"{name} must be an integer >= 2")
        self.argv = tuple(argv) if argv is not None else None
        self.cwd, self.env = cwd, env
        self._transport = transport
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self.default_timeout = _duration(default_timeout)
        self.max_message_bytes, self.max_pending, self.max_events = max_message_bytes, max_pending, max_events
        self.max_inbound_message_bytes = max_inbound_message_bytes
        self._started = self._closed = self._in_callback = False
        self._cleanup_complete = False
        self.cleanup_error: AppServerCleanupError | None = None
        self._next_id = 1
        self._pending: set[int] = set()
        self._responses: dict[int, ReceivedMessage] = {}
        self._history: dict[int, ReceivedMessage] = {}
        self._retired: dict[int, None] = {}
        self._events: deque[ReceivedMessage] = deque()
        self._callbacks_paused = False
        self._deferred_events: deque[tuple[ReceivedMessage, int]] = deque()
        self._deferred_bytes = self._last_frame_bytes = 0
        self.last_received: ReceivedMessage | None = None
        self._receive_seq = 0

    @property
    def pending_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._pending))

    @property
    def transport(self) -> Transport | None:
        return self._transport

    def start(self) -> AppServerClient:
        if self._closed:
            raise AppServerDisconnected("App Server client is closed", pending_ids=self.pending_ids)
        if not self._started:
            if self._transport is None:
                self._transport = StdioTransport(self.argv, cwd=self.cwd, env=self.env,
                                                 max_message_bytes=self.max_inbound_message_bytes)
            self._started = True
        return self

    def __enter__(self) -> AppServerClient:
        return self.start()

    def __exit__(self, exc_type: Any, exc: BaseException | None, traceback: Any) -> None:
        if exc is None:
            self.close()
        else:
            self._close_preserving(exc)

    def close(self) -> None:
        self._closed = True
        if self._cleanup_complete:
            return
        if self._started and self._transport is not None:
            try:
                self._transport.close()
            except AppServerCleanupError as exc:
                self.cleanup_error = exc
                raise
            except Exception as exc:
                self.cleanup_error = AppServerCleanupError("App Server transport cleanup incomplete")
                raise self.cleanup_error from exc
        self._cleanup_complete = True
        self.cleanup_error = None

    @property
    def cleanup_pending(self) -> bool:
        return self._closed and not self._cleanup_complete

    def _close_preserving(self, primary: BaseException) -> None:
        """Cleanup uncertainty must never replace the primary UNKNOWN error."""
        try:
            self.close()
        except Exception as cleanup:
            primary.cleanup_error = cleanup
            primary.add_note("App Server cleanup incomplete; close() may retry bounded reaping")

    def _ready(self, *, reading: bool = False) -> None:
        if not self._started or self._closed:
            raise AppServerDisconnected("App Server client is not open", pending_ids=self.pending_ids)
        if reading and self._in_callback:
            raise RuntimeError("nested App Server reads inside callbacks are unsupported")

    def _deadline(self, timeout: float | None) -> float:
        return time.monotonic() + _duration(self.default_timeout if timeout is None else timeout)

    def _write(self, message: dict[str, Any], deadline: float) -> None:
        # Encode before entering transport: an invalid local object was never sent.
        try:
            line = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8") + b"\n"
        except (ValueError, TypeError, RecursionError, UnicodeError) as exc:
            raise ValueError("outbound App Server message is not finite UTF-8 JSON") from exc
        if len(line) > self.max_message_bytes:
            raise ValueError("outbound App Server message exceeded bound")
        try:
            self._transport.write_line(line, _remaining(deadline))
        except AppServerError as exc:
            if isinstance(exc, (AppServerDisconnected, AppServerTimeout)):
                exc.pending_ids = self.pending_ids
            self._close_preserving(exc)
            raise
        except (OSError, ValueError) as exc:
            # A transport error can follow a partial write. It must not look
            # like the local JSON encoding ValueError handled by _send.
            primary = AppServerDisconnected("App Server write failed", pending_ids=self.pending_ids)
            self._close_preserving(primary)
            raise primary from exc
        except BaseException as exc:
            self._close_preserving(exc)
            raise

    @staticmethod
    def _method(method: str) -> None:
        if not isinstance(method, str) or not method:
            raise ValueError("method must be a nonempty string")

    def _send(self, method: str, params: Any, deadline: float) -> int:
        self._ready()
        self._method(method)
        if len(self._pending) >= self.max_pending:
            raise AppServerProtocolError("too many outstanding App Server requests")
        request_id = self._next_id
        self._next_id += 1
        self._pending.add(request_id)
        try:
            self._write({"id": request_id, "method": method, "params": {} if params is None else params}, deadline)
        except ValueError:
            self._pending.remove(request_id)
            raise
        except AppServerError as exc:
            exc.request_id = request_id
            exc.pending_ids = self.pending_ids
            raise
        return request_id

    def send(self, method: str, params: Any = None, *, timeout: float | None = None) -> int:
        """Send once and retain the id for a subsequent wait; never retry."""
        try:
            return self._send(method, params, self._deadline(timeout))
        except AppServerTimeout as exc:
            exc.pending_ids = self.pending_ids
            raise

    def notify(self, method: str, params: Any = None, *, timeout: float | None = None) -> None:
        self._ready()
        self._method(method)
        self._write({"method": method, "params": {} if params is None else params}, self._deadline(timeout))

    def response_received(self, request_id: int) -> ReceivedMessage | None:
        """Return an outstanding reply's arrival metadata or a recently consumed reply."""
        return self._responses.get(request_id) or self._history.get(request_id)

    def retire(self, request_id: int) -> bool:
        """Release a request after its owner has independently reconciled its effects.

        No frame is sent. An outstanding reply already read is consumed immediately.
        For a missing reply, retain its exact ID to absorb one eventual response.
        At most max_events missing replies are remembered. A still later unmatched
        frame closes the connection under the usual unsolicited-response rule.
        """
        self._ready()
        if type(request_id) is not int or request_id not in self._pending:
            return False
        self._pending.remove(request_id)
        if self._responses.pop(request_id, None) is None:
            self._retired[request_id] = None
            if len(self._retired) > self.max_events:
                del self._retired[next(iter(self._retired))]
        return True

    @property
    def callbacks_paused(self) -> bool:
        return self._callbacks_paused

    def pause_callbacks(self) -> None:
        """Finish the current callback, then yield and defer later callbacks.

        The current event is not replayed; its caller owns any staged facts.
        Explicit outer requests still receive responses while delivery is paused.
        No method, metadata lookup or server reply is automatically generated.
        """
        self._ready()
        self._callbacks_paused = True

    def resume_callbacks(self, *, timeout: float | None = None) -> None:
        """Deliver only already buffered events, once, in original wire order.

        Never reads the pipe or changes receive_seq/last_received. Deferred events
        are bounded by max_events and total original JSONL max_message_bytes.
        Business admission and the meaning of later response metadata are caller
        policy. An event handler may pause again; the remaining buffer stays put.
        """
        self._ready(reading=True)
        deadline = self._deadline(timeout)
        self._callbacks_paused = False
        try:
            while self._deferred_events:
                _remaining(deadline)
                received, size = self._deferred_events.popleft()
                self._deferred_bytes -= size
                self._deliver_callback(received, deadline)
                _remaining(deadline)
        except AppServerTimeout as exc:
            self._callbacks_paused = True
            exc.pending_ids = self.pending_ids
            raise

    def _receive(self, deadline: float) -> ReceivedMessage:
        try:
            line = self._transport.read_line(_remaining(deadline))
            if not isinstance(line, bytes) or not line.endswith(b"\n"):
                raise AppServerProtocolError("transport returned an incomplete JSON line")
            if len(line) > self.max_inbound_message_bytes:
                raise AppServerFrameTooLarge("App Server message exceeded bound")
            try:
                message = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs,
                    parse_float=_finite_float, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise AppServerProtocolError("invalid App Server UTF-8 JSON") from exc
            _envelope(message)
            if len(line) > self.max_message_bytes and "method" in message and "id" in message:
                raise AppServerProtocolError("incoming App Server request exceeded bound")
            self._last_frame_bytes = len(line)
            self._receive_seq += 1
            received = ReceivedMessage(self._receive_seq, message)
            self.last_received = received
            return received
        except AppServerTimeout as exc:
            exc.pending_ids = self.pending_ids
            raise
        except BaseException as exc:
            if isinstance(exc, (AppServerDisconnected, AppServerFrameTooLarge)):
                exc.pending_ids = self.pending_ids
            if isinstance(exc, AppServerFrameTooLarge):
                # The same transport also drains stdout while writing. Only a
                # rejection here precedes dispatch and has no ambiguous write.
                exc.failure_code = "INBOUND_FRAME_LIMIT_BEFORE_DISPATCH"
            self._close_preserving(exc)
            raise

    def _pump(self, deadline: float, *, poll_at: float | None = None) -> bool:
        # The optional wakeup bounds the read only. A callback arriving just
        # before it still has the caller's full deadline to return its result.
        read_deadline = deadline if poll_at is None else min(deadline, poll_at)
        try:
            received = self._receive(read_deadline)
        except AppServerTimeout:
            if read_deadline == deadline:
                raise
            return False
        message = received.message
        if "method" not in message:
            request_id = message["id"]
            # str and int ids are distinct; bool was rejected by _envelope.
            if type(request_id) is int and request_id in self._retired:
                del self._retired[request_id]
            elif type(request_id) is int and request_id in self._pending and request_id not in self._responses:
                self._responses[request_id] = received
            else:
                primary = AppServerProtocolError("unsolicited or duplicate App Server response")
                self._close_preserving(primary)
                raise primary
            self._history[request_id] = received
            if len(self._history) > self.max_pending:
                del self._history[next(iter(self._history))]
            return True
        if len(self._events) >= self.max_events:
            primary = AppServerProtocolError("App Server event queue exceeded bound")
            self._close_preserving(primary)
            raise primary
        self._events.append(received)
        if self._callbacks_paused:
            if (len(self._deferred_events) >= self.max_events or
                    self._deferred_bytes + self._last_frame_bytes > self.max_message_bytes):
                primary = AppServerProtocolError("deferred callback buffer exceeded bound")
                self._close_preserving(primary)
                raise primary
            self._deferred_events.append((received, self._last_frame_bytes))
            self._deferred_bytes += self._last_frame_bytes
            return True
        self._deliver_callback(received, deadline)
        return True

    def _deliver_callback(self, received: ReceivedMessage, deadline: float) -> None:
        message = received.message
        callback = self.on_server_request if "id" in message else self.on_notification
        metadata = {"callback_method": message["method"], "receive_seq": received.receive_seq,
                    "callback_kind": "request" if "id" in message else "notification"}
        result = None
        try:
            self._in_callback = True
            if callback is not None:
                result = callback(received)
        except Exception as exc:
            primary = AppServerCallbackError("App Server callback failed", **metadata, stage="handler")
            if "id" in message:
                try:
                    self._write({"id": message["id"], "error": {"code": -32603,
                        "message": "Client callback failed"}}, deadline)
                except Exception as reply_error:
                    # A failed error reply must not erase the original handler
                    # cause or imply that its business effects were undone.
                    primary.reply_error = reply_error
            self._close_preserving(primary)
            raise primary from exc
        finally:
            self._in_callback = False
        if ("id" in message and
                not (message["method"] == "item/tool/call" and result is NO_TOOL_RESPONSE)):
            response = {"id": message["id"], "result": result} if callback is not None else {
                "id": message["id"], "error": {"code": -32601, "message": "No client handler for server request"}}
            try:
                self._write(response, deadline)
            except ValueError as exc:
                primary = AppServerCallbackError("server callback returned invalid JSON",
                    **metadata, stage="result_encoding")
                self._close_preserving(primary)
                raise primary from exc
        if self._callbacks_paused:
            raise AppServerDeliveryPaused(received, pending_ids=self.pending_ids)

    def _wait(self, request_id: int, deadline: float) -> Any:
        self._ready(reading=True)
        if type(request_id) is not int or request_id not in self._pending:
            raise ValueError("request_id is not outstanding on this client")
        try:
            while request_id not in self._responses:
                self._pump(deadline)
        except AppServerError as exc:
            exc.request_id = request_id
            exc.pending_ids = self.pending_ids
            raise
        received = self._responses.pop(request_id)
        self._pending.remove(request_id)
        if "error" in received.message:
            raise AppServerResponseError(received)
        return received.message["result"]

    def wait(self, request_id: int, *, timeout: float | None = None) -> Any:
        return self._wait(request_id, self._deadline(timeout))

    def request(self, method: str, params: Any = None, *, timeout: float | None = None) -> Any:
        self._ready(reading=True)
        deadline = self._deadline(timeout)
        request_id = self._send(method, params, deadline)
        return self._wait(request_id, deadline)

    def run_until(self, predicate: Callable[[ReceivedMessage], bool], *,
                  timeout: float | None = None, poll_interval: float | None = None) -> ReceivedMessage | None:
        """Consume events in arrival order until predicate matches.

        Earlier nonmatching events are consumed, not silently retained forever.
        Responses are routed to wait(), not passed to the event predicate.
        With a poll interval, return None at the next wakeup, even on a quiet
        pipe. This leaves the connection, turn and pending requests untouched.
        """
        self._ready(reading=True)
        deadline = self._deadline(timeout)
        poll_at = self._deadline(poll_interval) if poll_interval is not None else None
        while True:
            while self._events:
                received = self._events.popleft()
                if predicate(received):
                    return received
            if not self._pump(deadline, poll_at=poll_at):
                return None
