"""What a host's own record of one worker amounts to, in host-neutral terms.

A reader (``claude_code`` today) turns the file a host wrote into a :class:`Record`: the
turns the worker took, each with the model that served it and the effort it was sent at, and
the refusals and fallbacks the host noted. :func:`summarise` then answers one question about
a period of that record - did every turn run on the required model at an allowed effort - in
the shape ``policy.judge`` takes.

A period spent off the policy is reported as **stretches**: runs of consecutive turns on one
model that the policy does not allow, each with its first and last turn, whether a later turn
ended it, and what the record shows about how it began. The first build's session was on the
pin, off it for 988 turns and back on it; a summary that only counted would say the deviation
was still going on.

Nothing here holds message text. A turn is a timestamp, identifiers, a model name, an effort
level and four token counts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Mapping

DEVIATION_FALLBACK = "fallback"
DEVIATION_REFUSAL = "refusal"

#: How a stretch off the policy began, as far as the record shows.
#: The host wrote that it moved the session to another model (a fallback row) before the stretch.
CAUSE_HOST_FALLBACK = "host_fallback"
#: The host announced the new model at a prompt boundary before its first turn and recorded no
#: fallback: the session's model setting was changed between turns, which is what a switch made
#: with the host's model command leaves. The record does not say who made it.
CAUSE_MODEL_SET = "model_set_between_turns"
#: The worker's first turn is already off the policy: it was started that way.
CAUSE_BEGAN = "began_off_policy"
#: The model is the required one and the effort is below what the task allows.
CAUSE_EFFORT = "effort_below_requirement"
#: None of the above is in the record.
CAUSE_UNRECORDED = "unrecorded"


@dataclass(frozen=True)
class Turn:
    """One row the host wrote for one piece of a model's response."""

    at: str
    model: str
    requested: str | None = None
    effort: str | None = None
    per_turn_effort: str | None = None
    message_id: str | None = None
    request_id: str | None = None
    stop_reason: str | None = None
    #: input, output, cache-creation and cache-read tokens of the response this row belongs to
    tokens: tuple[int, int, int, int] | None = None


@dataclass(frozen=True)
class Deviation:
    """A refusal or a model fallback, as the host recorded it."""

    kind: str
    at: str
    from_model: str | None = None
    to_model: str | None = None
    category: str | None = None
    request_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Announcement:
    """The host telling a session, at a prompt boundary, which model it now runs on."""

    at: str
    model: str


@dataclass
class Record:
    """The host's record of one worker: the main session or one of its agents."""

    host: str
    path: str
    sha256: str
    size: int
    session_ref: str | None = None
    agent_ref: str | None = None
    #: False when the reader could not vouch for the format; ``reason`` says why
    readable: bool = False
    reason: str | None = None
    versions: dict[str, int] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    turns: list[Turn] = field(default_factory=list)
    deviations: list[Deviation] = field(default_factory=list)
    #: empty when the reader was not allowed to read announcements (the policy lists no field for them)
    announcements: list[Announcement] = field(default_factory=list)
    #: rows of the kinds the reader interprets, and rows it found but could not use
    rows_read: int = 0
    rows_unusable: int = 0

    @property
    def host_version(self) -> str | None:
        """The single host version every interpreted row carries, or None if there is not one."""
        return next(iter(self.versions)) if len(self.versions) == 1 else None

    @property
    def worker_name(self) -> str | None:
        return self.meta.get("name") or self.meta.get("agentType")


def _in_period(at: str, since: str | None, until: str | None) -> bool:
    if since is not None and at < since:
        return False
    if until is not None and at > until:
        return False
    return True


def _readings(turn: Turn) -> list[str]:
    return [level for level in (turn.effort, turn.per_turn_effort) if level is not None]


#: A required model: one name (the run view: what the turn's task requires under the policy
#: its run pinned), or ``at -> name`` (the record view: the pin the user had set when a turn was
#: served, ``policy.model_at``). In the record view the callable may also say which models the
#: user allowed beside the pin at that time (``.allowed(at)``); a turn on one of them is not
#: off the policy.
RequiredModel = "str | Callable[[str], str]"


def required_at(required_model: Any, at: str) -> str:
    """The model required of a turn served at ``at``: the one name, or the pin in force then."""
    return required_model(at) if callable(required_model) else required_model


def allowed_at(required_model: Any, at: str) -> tuple[str, ...]:
    """Every model a turn served at ``at`` may have run on without being off the policy.

    The run view allows the one model the work requires. The record view allows the pin in
    force at that time and the models the user allowed beside it from then on.
    """
    if callable(required_model):
        allowed = getattr(required_model, "allowed", None)
        return tuple(allowed(at)) if callable(allowed) else (required_model(at),)
    return (required_model,)


def _off_policy(turn: Turn, required_model: Any, allowed: set[str]) -> tuple[bool, bool]:
    """``(off the model, below the effort)`` for one turn.

    A host may write the effort of a turn in two fields; a turn passes only if every reading
    it carries is allowed, which needs no ranking of levels and errs toward holding.
    """
    readings = _readings(turn)
    return (turn.model not in allowed_at(required_model, turn.at),
            bool(readings) and not all(level in allowed for level in readings))


def stretches(record: Record, *, required_model: Any, allowed_efforts: Iterable[str]) -> list[dict[str, Any]]:
    """Every run of consecutive off-policy turns on one model, over the whole record, in order.

    Each stretch says when it began and when its last turn was, whether a later turn ended it
    (``ended`` and ``ended_at``, the first turn after it; ``back_on_policy`` when that turn was
    on the policy rather than on yet another model), and its ``cause``: what the record
    shows happened between the turn before it and its first turn. A fallback explains a
    stretch only when no turn lies between the two; one from the day before, with a return to
    the pin since, explains nothing about a stretch that began today.
    """
    allowed = {str(level) for level in allowed_efforts}
    found: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    previous_at: str | None = None
    fallbacks = [item for item in record.deviations if item.kind == DEVIATION_FALLBACK]
    for index, turn in enumerate(record.turns):
        off_model, below = _off_policy(turn, required_model, allowed)
        if not (off_model or below):
            if current is not None:
                current["ended"], current["ended_at"], current["back_on_policy"] = True, turn.at, True
                current = None
            previous_at = turn.at
            continue
        if current is None or current["model"] != turn.model:
            if current is not None:
                current["ended"], current["ended_at"] = True, turn.at
            current = {
                "model": turn.model, "turns": 0, "turns_off_model": 0, "turns_below_effort": 0, "efforts": {},
                "first_at": turn.at, "last_at": turn.at, "request_id": turn.request_id,
                "required_model": required_at(required_model, turn.at),
                "ended": False, "ended_at": None, "back_on_policy": False,
                "cause": CAUSE_UNRECORDED, "fallback": None, "announced_at": None,
                "first_index": index,
            }
            if not off_model:
                current["cause"] = CAUSE_EFFORT
            else:
                between = [item for item in fallbacks
                           if item.at <= turn.at and (previous_at is None or item.at > previous_at)]
                # What the host announced between the two turns, latest last. The model was "set
                # between turns" only if the latest announcement names the model that then served;
                # the time is that of the first of the unbroken run of announcements naming it.
                said = [item for item in record.announcements
                        if item.at <= turn.at and (previous_at is None or item.at > previous_at)]
                run_of_this_model: list[Announcement] = []
                for item in reversed(said):
                    if item.model != turn.model:
                        break
                    run_of_this_model.append(item)
                if between:
                    current["cause"], current["fallback"] = CAUSE_HOST_FALLBACK, between[-1].to_dict()
                elif index == 0:
                    current["cause"] = CAUSE_BEGAN
                elif run_of_this_model:
                    current["cause"], current["announced_at"] = CAUSE_MODEL_SET, run_of_this_model[-1].at
            found.append(current)
        current["turns"] += 1
        current["turns_off_model"] += int(off_model)
        current["turns_below_effort"] += int(below)
        label = "/".join(dict.fromkeys(_readings(turn))) or "unknown"
        current["efforts"][label] = current["efforts"].get(label, 0) + 1
        current["last_at"] = turn.at
        previous_at = turn.at
    return found


def summarise(
    record: Record,
    *,
    required_model: Any,
    allowed_efforts: Iterable[str],
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Count, over one period of a record, what ``policy.judge`` asks about.

    ``required_model`` is one name, or ``at -> name`` when each turn is judged against the pin
    in force when it was served (the record view; a turn on a model the user allowed beside
    that pin is then on the policy too, see :func:`allowed_at`).
    ``turns_off_model`` counts turns served by any model other than ``required_model``, and
    ``turns_below_effort`` turns whose effort is known and is not one of ``allowed_efforts``;
    ``turns_off_policy`` counts each turn that is either, once.
    A host may write the effort of a turn in two fields; a turn passes only if every reading
    it carries is allowed, which needs no ranking of levels and errs toward holding.

    Timestamps are compared as text: a reader hands over every ``at`` in the one fixed-width
    UTC form the backend's clock writes, and ``since`` and ``until`` come from that clock.

    ``stretches`` are the off-policy stretches of the record that have a turn in the period,
    each with ``turns_in_period``; their own counts and times are those of the whole stretch,
    so a period that began on a fallback model still says when the fallback happened.
    ``off_policy_ongoing`` is whether the record's last turn is off the policy: a deviation
    that ended and one that had not at the time of the read are different findings.

    ``preceding_fallback`` is the fallback that began the stretch the period's first off-policy
    turn belongs to, when the host recorded it before the period: a period that ran entirely
    on a fallback model can still be reported with when the switch happened, from which model
    to which, its category and its request id. A fallback the session has since returned from
    is not one.
    """
    allowed = {str(level) for level in allowed_efforts}
    summary: dict[str, Any] = {
        "readable": bool(record.readable),
        "reason": record.reason,
        "host": record.host,
        "host_version": record.host_version,
        "versions": dict(record.versions),
        "session_ref": record.session_ref,
        "agent_ref": record.agent_ref,
        "record_sha256": record.sha256,
        "since": since,
        "until": until,
        "turns": 0,
        "responses": 0,
        "turns_off_model": 0,
        "turns_below_effort": 0,
        "turns_off_policy": 0,
        "turns_effort_unknown": 0,
        "turns_served_differs_from_requested": 0,
        "models": {},
        "efforts": {},
        "tokens": {"input": 0, "output": 0, "cache_creation": 0, "cache_read": 0},
        "first_turn_at": None,
        "last_turn_at": None,
        "first_off_policy": None,
        "last_off_policy_at": None,
        "off_policy_ongoing": False,
        "stretches": [],
        "deviations": [],
        "preceding_fallback": None,
        "record_turns": len(record.turns),
    }
    if not record.readable:
        return summary
    whole = stretches(record, required_model=required_model, allowed_efforts=allowed)
    in_period: dict[int, int] = {}

    responses: dict[str, tuple[int, int, int, int] | None] = {}
    position = 0
    for index, turn in enumerate(record.turns):
        while position + 1 < len(whole) and whole[position + 1]["first_index"] <= index:
            position += 1
        if not _in_period(turn.at, since, until):
            continue
        summary["turns"] += 1
        summary["models"][turn.model] = summary["models"].get(turn.model, 0) + 1
        summary["first_turn_at"] = summary["first_turn_at"] or turn.at
        summary["last_turn_at"] = turn.at
        key = turn.message_id or f"row:{summary['turns']}"
        if turn.tokens is not None or key not in responses:
            responses[key] = turn.tokens

        off_model, below = _off_policy(turn, required_model, allowed)
        readings = _readings(turn)
        effort_label = "/".join(dict.fromkeys(readings)) if readings else "unknown"
        summary["efforts"][effort_label] = summary["efforts"].get(effort_label, 0) + 1
        if off_model:
            summary["turns_off_model"] += 1
        if not readings:
            summary["turns_effort_unknown"] += 1
        elif below:
            summary["turns_below_effort"] += 1
        if turn.requested is not None and turn.requested != turn.model:
            summary["turns_served_differs_from_requested"] += 1
        if off_model or below:
            summary["turns_off_policy"] += 1
            summary["last_off_policy_at"] = turn.at
            in_period[position] = in_period.get(position, 0) + 1
        if (off_model or below) and summary["first_off_policy"] is None:
            summary["first_off_policy"] = {
                "at": turn.at, "model": turn.model, "requested": turn.requested,
                "effort": effort_label, "request_id": turn.request_id,
            }

    summary["responses"] = len(responses)
    for tokens in responses.values():
        if tokens is not None:
            for name, value in zip(("input", "output", "cache_creation", "cache_read"), tokens):
                summary["tokens"][name] += value

    for deviation in record.deviations:
        if _in_period(deviation.at, since, until):
            summary["deviations"].append(deviation.to_dict())
    for position, count in sorted(in_period.items()):
        stretch = {key: value for key, value in whole[position].items() if key != "first_index"}
        stretch["turns_in_period"] = count
        summary["stretches"].append(stretch)
    if summary["stretches"]:
        began = summary["stretches"][0].get("fallback")
        if began is not None and since is not None and began["at"] < since:
            summary["preceding_fallback"] = dict(began)
    if record.turns:
        off_model, below = _off_policy(record.turns[-1], required_model, allowed)
        summary["off_policy_ongoing"] = bool(off_model or below)
    return summary


def deviation_for_report(summary: Mapping[str, Any], *, required_model: Any) -> dict[str, Any] | None:
    """What the user is told: when, from which model to which, the category, the request id.

    Taken from the first refusal or fallback the host recorded in the period; failing that,
    from the fallback that began the stretch the period was spent in; failing that, from the
    first turn that was off the policy, with what the record shows about how that stretch
    began. ``until`` is the last off-policy turn of the period and ``ended`` whether a later
    turn of the record was back on the policy.
    """
    span = {"until": summary.get("last_off_policy_at"),
            "ended": bool(summary.get("turns_off_policy")) and not summary.get("off_policy_ongoing")}
    events = list(summary.get("deviations") or [])
    if events:
        fallbacks = [event for event in events if event.get("kind") == DEVIATION_FALLBACK]
        chosen = (fallbacks or events)[0]
        return {
            "kind": chosen.get("kind"), "when": chosen.get("at"),
            "from_model": chosen.get("from_model") or required_at(required_model, chosen.get("at") or ""),
            "to_model": chosen.get("to_model"), "category": chosen.get("category"),
            "request_id": chosen.get("request_id"), "source": "host_transcript", **span,
        }
    first = summary.get("first_off_policy")
    if not first:
        return None
    stretch = (summary.get("stretches") or [{}])[0]
    began = stretch.get("fallback")
    if began:
        return {
            "kind": DEVIATION_FALLBACK, "when": began.get("at"),
            "from_model": began.get("from_model") or required_at(required_model, began.get("at") or ""),
            "to_model": began.get("to_model"), "category": began.get("category"),
            "request_id": began.get("request_id"), "source": "host_transcript",
            "first_turn_in_period": first.get("at"), "cause": stretch.get("cause"), **span,
        }
    return {
        "kind": "off_policy_turn", "when": stretch.get("announced_at") or first.get("at"),
        "from_model": stretch.get("required_model") or required_at(required_model, first.get("at") or ""),
        "to_model": first.get("model"), "category": None,
        "request_id": first.get("request_id"), "effort": first.get("effort"),
        "source": "host_transcript", "first_turn_in_period": first.get("at"),
        "cause": stretch.get("cause") or CAUSE_UNRECORDED, **span,
    }
