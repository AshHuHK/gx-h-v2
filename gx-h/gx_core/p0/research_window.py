"""The registered wall-clock window for a timed Goal. The durable Job and turn owners enforce it."""
import json
import math

from ..domain import clock
from ..errors import HoldActive, SchemaInvalid


DEADLINES = ("start", "new_work_until", "drain_until", "reconcile_until")


def validate(window):
    if (not isinstance(window, dict) or set(window) != set(DEADLINES)
            or any(type(window[key]) not in (int, float) or not 0 < window[key] < math.inf
                   for key in DEADLINES)):
        raise SchemaInvalid("research_window requires four positive finite absolute Unix timestamps")
    if not all(window[left] < window[right] for left, right in zip(DEADLINES, DEADLINES[1:])):
        raise SchemaInvalid("research_window deadlines must strictly increase")


def for_run(conn, run_id):
    raw = conn.execute("SELECT json_extract(inputs_json,'$.inputs.p0_spec.research_window') "
                       "FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(raw[0]) if raw and raw[0] else None


def phase(window, *, now=None):
    if window is None:
        return "OPEN"
    current = clock.now().timestamp() if now is None else now
    for key, name in zip(DEADLINES, ("BEFORE_START", "OPEN", "DRAIN", "RECONCILE")):
        if current < window[key]:
            return name
    return "ENDED"


def require_open(window):
    stage = phase(window)
    if stage != "OPEN":
        raise HoldActive("The registered research window does not admit new work",
                         details={"reason": "research_window", "phase": stage})


def require_completion(window):
    stage = phase(window)
    if stage not in ("OPEN", "DRAIN"):
        raise HoldActive("The registered scientific observation and drain windows are closed",
                         details={"reason": "research_window", "phase": stage})
