"""What a pause, a hold or an untested path may need: one closed vocabulary and one schema.

Design r2.2 §2.8.2. A run pauses only on something a person or the machine can clear, and
says what: ``{kind, subject?, what, why, clear_by}``.

* ``kind`` is one of :data:`NEED_KINDS`. Each kind has exactly one clearing predicate,
  published by the stream that holds the fact (:data:`NEEDS` names the stream and says in
  words when the need is cleared). ``continue`` is refused while any need is uncleared.
* ``subject`` is what the predicate is evaluated on (a line, an approval, a capability, an
  input, a task), as a mapping. Whoever records the need fills it in; a row of the route
  table names only what it can know before the failure happens (the action kind of the
  approval a hold asks for).
* ``what`` says what must be supplied, ``why`` what made it necessary.
* ``clear_by`` is :data:`PERSON` when nothing the system does on its own will clear the
  need, and :data:`MACHINE` when the backend holds a fact that says it clears without a
  person (a device that is busy now, not one the job can never fit).

This module is data and two small pure functions. It imports nothing from the package, so
every other module of it, and any other stream, may read the vocabulary without a cycle.
"""

from __future__ import annotations

from typing import Any, Mapping

PERSON, MACHINE = "person", "machine"
#: Who can make a need clear.
CLEAR_BY = (PERSON, MACHINE)

#: The closed vocabulary (design r2.2 §2.8.2): for each kind, when it is cleared and which
#: stream publishes the predicate that says so. ``host_quota`` has none: a person continues,
#: and that statement is what is recorded. ``subject``: the shapes the predicate's owner
#: reads in a need's ``subject`` (alternatives), where that owner has said; None where it
#: has not. They are not part of the frozen vocabulary.
NEEDS: dict[str, dict[str, Any]] = {
    "budget_line": {
        "cleared_when": "the line has room; at the pause core calls budgets.request_increase and names the "
                        "approval",
        "predicate_from": ("eval",),
        "subject": ("{kind: budget_line, id: <line>}",)},
    "hold": {
        "cleared_when": "no hold of that scope is in config/limits.json",
        "predicate_from": ("core",),
        "subject": ("{kind: hold, id: <hold id>}",)},
    "approval": {
        "cleared_when": "the approval is decided GRANTED",
        "predicate_from": ("govern",),
        "subject": ("{kind: approval, action_kind, id: <approval id, once requested>}",)},
    "readiness": {
        "cleared_when": "the capability is ACTIVE at the planned sha, or the environment reads HEALTHY",
        "predicate_from": ("exec", "govern"),
        "subject": ("{capability_version, manifest_sha256?}", "{capability_id}", "{env_id}")},
    "input": {
        "cleared_when": "the input file re-hashes to the recorded sha256",
        "predicate_from": ("core",),
        "subject": None},
    "device_capacity": {
        "cleared_when": "a fresh capacity snapshot says the job can start",
        "predicate_from": ("exec",),
        "subject": ("{capability_version}",)},
    "host_quota": {
        "cleared_when": "a person continues; recorded as the person's statement",
        "predicate_from": (),
        "subject": None},
    "host_effort": {
        "cleared_when": "the coordinator's session serves the rows' minimum effort",
        "predicate_from": ("host",),
        "subject": None},
    "pin": {
        "cleared_when": "require_pins passes, or a run_repin approval is GRANTED",
        "predicate_from": ("core",),
        "subject": None},
    "path_reachability": {
        "cleared_when": "the recorded host, mount and permission check passes",
        "predicate_from": ("exec",),
        "subject": ("{capability_version}", "{env_id}", "{paths: [{path, access}], host?}")},
    "independent_review": {
        "cleared_when": "a HUMAN review of the subject exists, or a delegating host holds the coordinator",
        "predicate_from": ("govern", "host"),
        "subject": None},
    "task_blocked": {
        "cleared_when": "the task is no longer BLOCKED",
        "predicate_from": ("core",),
        "subject": ("{kind: task, id: <task id>}",)},
    "reaction": {
        "cleared_when": "the run's max event id, its pinned workflow digest or the sha256 of config/limits.json "
                        "has moved since the reaction failed",
        "predicate_from": ("core",),
        "subject": None},
}
NEED_KINDS = tuple(NEEDS)

#: The fields of a need. ``subject`` is optional; ``why`` is absent from a row of the route
#: table (the decision that lands on the row says why) and present in every recorded need.
FIELDS = ("kind", "subject", "what", "why", "clear_by")
#: What a need may also say: the path or the task that waits for it (the scope of a pause).
WAITING = ("path_id", "task_id")

#: What would move a task whose chain ended, by why it is BLOCKED (``hold_json.reason``; at
#: contract r5 ``tasks.blocked_reason``): the plan's words for it. Design r2.2 §2.7: a person
#: revives it by an approval of kind ``budget_increase`` on the task that grants attempts. No
#: tool cancels one task (``gx_run_control`` cancels the run), so that is the other way out.
ENDED_WHAT = {
    "out_of_attempts": "more attempts for this task, granted by a person (a budget_increase approval on the task); "
                       "or cancelling the run",
    "not_dispatchable": "a fix of why it could not be dispatched (its brief, the gate that refused it), then more "
                        "attempts granted by a person (a budget_increase approval on the task); or cancelling the run",
    "routed": "what its failure's route ran out of, then more attempts granted by a person (a budget_increase "
              "approval on the task); or cancelling the run",
    "dependency_cancelled": "a new chain for what the cancelled dependency was to produce; or cancelling the run",
    "skipped": "nothing: a person decided to skip it (its worker_refusal approval DENIED); or cancelling the run",
}
#: What would move a task BLOCKED for a reason the table does not list, or for none on record.
ENDED_WHAT_OTHERWISE = "a person's decision on why it is blocked; or cancelling the run"
#: The reasons a person's revival covers (§2.7; the lead, 2026-11-13T01:47Z): only a task
#: BLOCKED for one of them is a need. Any other ended task is never one: nothing a person
#: approves moves it (dependency_cancelled), or a person has just decided it (skipped), and
#: a need nobody will clear would keep ``continue`` refused for every other need.
REVIVABLE = ("out_of_attempts", "not_dispatchable", "routed")


def need(kind: str, what: str, *, clear_by: str, why: str | None = None,
         subject: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """A need in the closed vocabulary. An unknown kind, no ``what`` or an unknown ``clear_by`` is refused."""
    built: dict[str, Any] = {"kind": kind, "what": what, "clear_by": clear_by}
    if subject is not None:
        built["subject"] = dict(subject)
    if why is not None:
        built["why"] = why
    found = problems(built)
    if found:
        raise ValueError("not a need of the closed vocabulary: " + "; ".join(found))
    return built


def problems(value: Any, *, recorded: bool = False) -> list[str]:
    """What keeps ``value`` from being a need of the vocabulary; empty when nothing does. Pure.

    ``recorded``: the need is being written down (a decision, an event), so it must say why.
    """
    if not isinstance(value, Mapping):
        return [f"a need is a mapping, not {type(value).__name__}"]
    found = []
    if value.get("kind") not in NEED_KINDS:
        found.append(f"kind {value.get('kind')!r} is not one of {list(NEED_KINDS)}")
    if not isinstance(value.get("what"), str) or not value["what"].strip():
        found.append("it does not say what must be supplied")
    if value.get("clear_by") not in CLEAR_BY:
        found.append(f"clear_by {value.get('clear_by')!r} is not one of {list(CLEAR_BY)}")
    if "subject" in value and not isinstance(value["subject"], Mapping):
        found.append("subject is not a mapping")
    if recorded and (not isinstance(value.get("why"), str) or not value["why"].strip()):
        found.append("it does not say why")
    unknown = sorted(set(value) - set(FIELDS) - set(WAITING))
    if unknown:
        found.append(f"unknown field(s) {unknown}")
    return found


def ended_what(reason: str | None) -> str:
    """What would move a task BLOCKED for ``reason``. Pure."""
    return ENDED_WHAT.get(reason, ENDED_WHAT_OTHERWISE)


def ended_task(task_id: str, reason: str | None, why: str) -> dict[str, Any]:
    """The need of a task whose chain ended (design r2.2 §2.7): only a person revives it. Pure.

    One definition for every place that names it: the pause (``stoprules.check``), the
    plan's revive and the ``schedule.chain_ended`` event core writes. Only a reason of
    :data:`REVIVABLE` has one; any other is refused, as is an empty ``why`` (the need is a
    recorded one, so it says why).
    """
    if reason not in REVIVABLE:
        raise ValueError(f"a task BLOCKED for {reason!r} is no need: a person's revival covers {list(REVIVABLE)}")
    made = need("task_blocked", ended_what(reason), clear_by=PERSON, why=why, subject={"kind": "task", "id": task_id})
    found = problems(made, recorded=True)
    if found:
        raise ValueError("not a need that can be recorded: " + "; ".join(found))
    return made


def vocabulary_for_contract() -> dict[str, Any]:
    """The vocabulary as the lead freezes it (the label axis ``pause_need_kind`` and the need schema)."""
    return {
        "pause_need_kind": {kind: {"cleared_when": spec["cleared_when"],
                                   "predicate_from": list(spec["predicate_from"])}
                            for kind, spec in NEEDS.items()},
        "clear_by": list(CLEAR_BY),
        "need": {"required": ["kind", "what", "why", "clear_by"], "optional": ["subject", *WAITING]},
    }


__all__ = ["CLEAR_BY", "ENDED_WHAT", "ENDED_WHAT_OTHERWISE", "FIELDS", "MACHINE", "NEEDS", "NEED_KINDS", "PERSON",
           "REVIVABLE", "WAITING", "ended_task", "ended_what", "need", "problems", "vocabulary_for_contract"]
