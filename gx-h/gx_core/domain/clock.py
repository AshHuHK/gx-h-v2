"""Single source of time.

Everything that records a timestamp goes through here, for two reasons: timestamps in
design and provenance records are evidence that a decision preceded a result, so they
must come from a clock rather than an estimate; and lease-expiry tests need to move
time without sleeping.
"""

from __future__ import annotations

import datetime as _dt
from typing import Callable

UTC = _dt.timezone.utc

_offset_seconds: float = 0.0
_source: Callable[[], _dt.datetime] = lambda: _dt.datetime.now(tz=UTC)


def now() -> _dt.datetime:
    return _source() + _dt.timedelta(seconds=_offset_seconds)


def now_iso() -> str:
    """ISO-8601 in UTC with a ``Z`` suffix, millisecond precision, fixed width."""
    return now().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse(value: str) -> _dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = _dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso_after(seconds: float) -> str:
    return (now() + _dt.timedelta(seconds=seconds)).astimezone(UTC).strftime(
        "%Y-%m-%dT%H:%M:%S.%f"
    )[:-3] + "Z"


def is_past(value: str) -> bool:
    return parse(value) <= now()


def age_seconds(value: str) -> float:
    return (now() - parse(value)).total_seconds()


# ---------------------------------------------------------------- test-only controls

def advance(seconds: float) -> None:
    """Move the clock forward. Tests only — never called by production code."""
    global _offset_seconds
    _offset_seconds += seconds


def reset() -> None:
    global _offset_seconds
    _offset_seconds = 0.0
