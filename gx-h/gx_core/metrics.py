"""Optional append-only measurements. Recording never changes GX transaction outcomes."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
import json
import logging
import os
import time

_identity = ContextVar("gx_metrics_identity", default={})
_FIELDS = ("run_id", "task_id", "attempt", "thread_id", "turn_id", "operation_id")
_TOKENS = ("totalTokens", "inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")
_warned = set()


def enabled():
    return bool(os.environ.get("GX_METRICS_FILE"))


@contextmanager
def identity(**fields):
    token = _identity.set({**_identity.get(), **fields})
    try:
        yield
    finally:
        _identity.reset(token)


def record(kind, *, started=None, **fields):
    path = os.environ.get("GX_METRICS_FILE")
    if not path:
        return
    try:
        data = dict.fromkeys(_FIELDS)
        data.update(_identity.get())
        data.update(fields)
        if started is not None:
            data["duration_ms"] = (time.monotonic_ns() - started) / 1_000_000
        data.update(kind=kind, observed_ns=time.time_ns())
        line = (json.dumps(data, separators=(",", ":"), allow_nan=False) + "\n").encode()
        # One append per record across multiple project/Job processes.
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            if os.write(fd, line) != len(line):
                raise OSError("short metrics write")
        finally:
            os.close(fd)
    except (OSError, ValueError, TypeError) as exc:
        error = type(exc).__name__
        if error not in _warned:
            _warned.add(error)
            logging.getLogger(__name__).warning("GX metrics unavailable (%s)", error)


def codex_usage(params, sequence, adapter_instance_id):
    """Emit each observed snapshot separately. Unknown token fields stay null."""
    if not enabled():
        return
    usage = params.get("tokenUsage")
    if not isinstance(usage, Mapping):
        usage = {}
    parts = {}
    for key in ("total", "last"):
        values = usage.get(key)
        parts[key] = ({name: value if type(value := values.get(name)) is int else None
                       for name in _TOKENS} if isinstance(values, Mapping) else None)
    record("usage_report", thread_id=params.get("threadId"), turn_id=params.get("turnId"),
           report_id=f"{adapter_instance_id}:{sequence}", receive_seq=sequence,
           source="thread/tokenUsage/updated", semantics="total_cumulative_last_snapshot",
           model_context_window=usage.get("modelContextWindow"), **parts)
