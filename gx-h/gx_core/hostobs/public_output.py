"""Bounded, screened public display; never an identity or business evidence source.

Only whole completed public messages/command output are considered. Streaming
chunks and reasoning are deliberately excluded so a split credential is never
published before the complete text can be screened.
"""
from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from typing import Any

SCOPE = "PUBLIC_DISPLAY_NOT_AUTHORITY_OR_BUSINESS_VERDICT"
MAX_ITEMS = 256
MAX_ITEM_SCAN_BYTES = 256 * 1024
MAX_SESSION_SCAN_BYTES = 2 * 1024 * 1024
MAX_ITEM_PUBLISH_BYTES = 32 * 1024
MAX_SESSION_PUBLISH_BYTES = 1024 * 1024
PHASES = (None, "commentary", "partial_answer", "final_answer")


class PublicOutputCollector:
    def __init__(self, sink: Callable, *, screen: Callable):
        self._sink, self._screen = sink, screen
        self._seen: set[tuple[str, str, str, str]] = set()
        self._stats = {"enabled": True, "scope": SCOPE, "captured": 0,
            "withheld": 0, "truncated": 0, "scanned_bytes": 0,
            "published_bytes": 0, "duplicates": 0, "ignored": 0,
            "stopped": False, "error": None, "stop_reason": None}
        if not callable(sink) or not callable(screen):
            self._stop("INVALID_DISPLAY_CALLBACK", error=True)

    def _stop(self, reason: str, *, error=False) -> None:
        self._stats.update(stopped=True, stop_reason=reason)
        if error and self._stats["error"] is None:
            self._stats["error"] = reason

    @staticmethod
    def _identity(value) -> bool:
        if (not isinstance(value, str) or not value or len(value) > 1000
                or any(ord(char) < 32 or ord(char) == 127 for char in value)):
            return False
        try:
            value.encode("utf-8")
        except UnicodeError:
            return False
        return True

    def _publish(self, record: dict[str, Any]) -> None:
        try:
            self._sink(record)
        except Exception:
            self._stop("OUTPUT_IO_FAILED", error=True)
            return
        if record["capture_status"] == "CAPTURED":
            self._stats["captured"] += 1
            self._stats["published_bytes"] += record["published_bytes"]
            self._stats["truncated"] += int(record["truncated"])
        else:
            self._stats["withheld"] += 1

    def observe(self, received, *, root_thread_id=None) -> None:
        """Best-effort display observation. No failure escapes into dispatch."""
        if self._stats["stopped"]:
            return
        try:
            self._observe(received, root_thread_id)
        except Exception:
            self._stop("DISPLAY_OBSERVATION_FAILED", error=True)

    def _observe(self, received, root_thread_id) -> None:
        message = received.message
        if not isinstance(message, Mapping) or message.get("method") != "item/completed":
            self._stats["ignored"] += 1
            return
        params = message.get("params")
        item = params.get("item") if isinstance(params, Mapping) else None
        if not isinstance(item, Mapping) or item.get("type") not in ("agentMessage", "commandExecution"):
            self._stats["ignored"] += 1
            return
        thread, turn, identity = params.get("threadId"), params.get("turnId"), item.get("id")
        if (not all(self._identity(value) for value in (thread, turn, identity))
                or type(received.receive_seq) is not int or received.receive_seq < 1):
            self._stats["withheld"] += 1
            return
        key = (thread, turn, identity, item["type"])
        if key in self._seen:
            self._stats["duplicates"] += 1
            return
        if len(self._seen) >= MAX_ITEMS:
            self._stats["withheld"] += 1
            self._stop("MAX_ITEMS")
            return
        self._seen.add(key)
        agent = item["type"] == "agentMessage"
        phase = item.get("phase") if agent else None
        text = item.get("text") if agent else item.get("aggregatedOutput")
        exit_code = None if agent else item.get("exitCode")
        record = {"scope": SCOPE, "root_thread_id": root_thread_id if self._identity(root_thread_id) else None,
            "thread_id": thread, "turn_id": turn, "item_id": identity,
            "event_seq": received.receive_seq, "item_type": item["type"],
            "kind": "agent_message" if agent else "command_execution",
            "phase": phase if phase in PHASES else None,
            "exit_code": exit_code if type(exit_code) is int else None,
            "capture_status": "WITHHELD", "text": None, "truncated": False,
            "original_bytes": None, "published_bytes": 0, "content_sha256": None}
        if phase not in PHASES or exit_code is not None and type(exit_code) is not int or not isinstance(text, str):
            record["reason"] = "INVALID_OR_UNAVAILABLE_PUBLIC_TEXT"
            self._publish(record)
            return
        if len(text) > MAX_ITEM_SCAN_BYTES:
            record["reason"] = "ITEM_SCAN_LIMIT"
            self._publish(record)
            return
        try:
            raw = text.encode("utf-8")
        except UnicodeError:
            record["reason"] = "INVALID_UTF8"
            self._publish(record)
            return
        record["original_bytes"] = len(raw)
        if len(raw) > MAX_ITEM_SCAN_BYTES:
            record["reason"] = "ITEM_SCAN_LIMIT"
            self._publish(record)
            return
        if self._stats["scanned_bytes"] + len(raw) > MAX_SESSION_SCAN_BYTES:
            record["reason"] = "SESSION_SCAN_LIMIT"
            self._publish(record)
            self._stop("SESSION_SCAN_LIMIT")
            return
        self._stats["scanned_bytes"] += len(raw)
        try:
            safe, redactions, urls = self._screen(text)
            encoded = safe.encode("utf-8")
            if type(redactions) is not int or type(urls) is not int or redactions < 0 or urls < 0:
                raise ValueError("invalid screen counts")
        except Exception:
            record["reason"] = "SCREEN_FAILED"
            self._publish(record)
            self._stop("SCREEN_FAILED", error=True)
            return
        remaining = MAX_SESSION_PUBLISH_BYTES - self._stats["published_bytes"]
        if remaining <= 0 and encoded:
            record["reason"] = "SESSION_PUBLISH_LIMIT"
            self._publish(record)
            self._stop("SESSION_PUBLISH_LIMIT")
            return
        published = encoded[:min(MAX_ITEM_PUBLISH_BYTES, remaining)].decode("utf-8", errors="ignore")
        record.update(capture_status="CAPTURED", text=published,
            published_bytes=len(published.encode("utf-8")), truncated=len(published.encode("utf-8")) < len(encoded),
            redaction_count=redactions, withheld_url_count=urls,
            content_sha256=hashlib.sha256(published.encode("utf-8")).hexdigest())
        self._publish(record)

    def summary(self) -> dict[str, Any]:
        return dict(self._stats)
