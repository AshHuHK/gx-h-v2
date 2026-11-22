"""Approvals: bound to one subject at one sha256, granted by a person or by the user's standing policy.

    request(ctx, action_kind="gpu_job", subject=subject, run_id=run_id, request={...})  -> dict
    require_granted(conn, action_kind="gpu_job", subject=subject)                       -> dict
    decide(ctx, approval_id=…, decision="GRANTED", note="…")                            -> dict

Three rules of ``contracts/states.json`` (machine ``approval``) shape this module.

**Bound.** An approval names ``subject_kind``, ``subject_id`` and ``subject_sha256`` and
unlocks exactly that. v1 approvals named no subject, so one grant unlocked anything.

**Who.** A worker requests; it never grants. GRANTED and DENIED are written by the operator
surface (``decide``), or — for GRANTED only — by the standing policy below.

**Standing.** ``config/approvals.json`` holds the approvals the user gave in words. When a
request matches a rule, the backend checks the rule's conditions *itself* and, if they hold,
grants at once and records the grant as made under that policy: ``decided_by`` is the
policy file's sha256, ``policy_ref`` the rule, and the note quotes the instruction. It is
never recorded as a person's decision. The conditions of a rule are facts some service
holds (a screen result, a capability's measured needs, a look ledger), so the service that
owns the facts publishes the evaluator::

    from gx_core.governance.approvals import condition, ConditionResult

    @condition("gpu_job")
    def within_limits(ctx, *, subject, request, rule) -> ConditionResult: ...

An action kind with no published evaluator is never granted by policy: the request stays
PENDING and says why. Kinds listed under ``never_standing`` (a change of model, sending
data out, starting a service) always wait for a person. And a switch of
``config/limits.json`` that is off is a prohibition, not a missing permission: nothing here
grants against it, whoever asks.

**A decision is announced.** Whatever gives an approval a status other than PENDING — a
person, the standing policy, a deadline passing, a backend event withdrawing it — goes
through one function, which writes the ``approval.<status>`` event and, in the same
transaction, one ``approval.decided`` outbox row (:func:`decided_trigger`). A run that waits
for the approval is moved by that row, not by whoever happens to ask next.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .. import config, contracts, registry
from ..context import SCOPE_OPERATOR, Ctx
from ..domain import clock, ids, machines
from ..errors import (
    ApprovalRequired,
    ArtifactNotFound,
    ContentHashMismatch,
    PermissionDenied,
    SchemaInvalid,
)
from ..persistence import store
from ..registry import Subject
from . import subjects

MACHINE = "approval"
PENDING = machines.initial_state(MACHINE)
GRANTED, DENIED, EXPIRED, REVOKED = "GRANTED", "DENIED", "EXPIRED", "REVOKED"
for _name in (GRANTED, DENIED, EXPIRED, REVOKED):
    if _name not in machines.all_states(MACHINE):  # pragma: no cover
        raise RuntimeError(f"approval state {_name!r} left the contract")

POLICY_FILE = "approvals.json"

#: Switches of config/limits.json that forbid a whole action kind while they are off.
#: Finer limits (sizes, scopes, budgets) belong to the kind's condition evaluator.
PROHIBITING_SWITCHES: dict[str, tuple[str, ...]] = {
    "gpu_job": ("gpu_jobs",),
    "external_download": ("network_downloads",),
    "service_start": ("start_or_restart_gx_services",),
}

#: ``Principal.source`` of a command-line call a person confirmed on a terminal. The value is
#: ``gx_core.api.SOURCE_INTERACTIVE_TERMINAL``; it is repeated here because the dispatcher
#: imports the providers and so cannot be imported from one (a test holds the two equal).
INTERACTIVE_SOURCE = "interactive_terminal"

#: The topic of the trigger an approval writes when it is decided (SCHEDULING_DESIGN r2.2 §2.4.1).
DECIDED_TOPIC = "approval.decided"


def decided_trigger_key(approval_id: str, status: str) -> str:
    """The idempotency key of an ``approval.decided`` outbox row: one row for each status an
    approval is given, and the machine gives an approval each status at most once."""
    return f"{DECIDED_TOPIC}:{approval_id}:{status}"


def decided_trigger(row: sqlite3.Row, status: str, event_id: int, decided_by: str) -> dict[str, Any]:
    """What an ``approval.decided`` outbox row says: which approval, of which run (``None`` for
    an approval no run owns), was given which status by what, the event that records it, and
    what the approval is bound to. Identifiers and state words only: the note, the request and
    the facts a policy checked are on the approval row and its event, where a reaction reads
    them. ``decided_by`` is ``person:<subject>``, ``policy:<sha256>``, ``expiry`` or the
    backend event that withdrew the approval."""
    return {"run_id": row["run_id"], "subject": row["approval_id"], "event_id": event_id, "status": status,
            "action_kind": row["action_kind"], "subject_kind": row["subject_kind"],
            "subject_id": row["subject_id"], "subject_sha256": row["subject_sha256"], "decided_by": decided_by}


# --------------------------------------------------------------------------- conditions

@dataclass
class ConditionResult:
    """What a rule's evaluator found: whether the rule's conditions hold, and on what facts."""

    ok: bool
    facts: dict[str, Any] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


ConditionFn = Callable[..., ConditionResult]

CONDITIONS: dict[str, ConditionFn] = {}
_CONDITION_OWNERS: dict[str, str] = {}


def condition(action_kind: str) -> Callable[[ConditionFn], ConditionFn]:
    """Publish the evaluator of the standing rule(s) for one approval action kind.

    ``fn(ctx, *, subject, request, rule) -> ConditionResult``. It reads limits and rows
    itself; ``request`` is what the requesting *service* passed, never a host's words.
    """
    contracts.require_label("approval_action_kind", action_kind)

    def decorate(fn: ConditionFn) -> ConditionFn:
        existing = CONDITIONS.get(action_kind)
        if existing is not None and (existing.__module__, existing.__qualname__) != (fn.__module__, fn.__qualname__):
            raise ValueError(
                f"the condition for {action_kind!r} is already published by {existing.__module__}; "
                f"{fn.__module__} cannot publish it too"
            )
        CONDITIONS[action_kind] = fn
        _CONDITION_OWNERS[action_kind] = fn.__module__
        return fn

    return decorate


def condition_for(action_kind: str) -> ConditionFn | None:
    registry.load_providers()
    return CONDITIONS.get(action_kind)


def condition_owners() -> dict[str, str]:
    registry.load_providers()
    return dict(sorted(_CONDITION_OWNERS.items()))


# ------------------------------------------------------------------------------- policy

def policy() -> tuple[dict[str, Any], str]:
    """The standing policy and the sha256 of its bytes. Re-read on every call."""
    raw = (config.CONFIG_DIR / POLICY_FILE).read_bytes()
    return json.loads(raw.decode("utf-8")), hashlib.sha256(raw).hexdigest()


def prohibited_by_switch(action_kind: str) -> dict[str, Any] | None:
    for name in PROHIBITING_SWITCHES.get(action_kind, ()):
        if not config.switch_enabled(name):
            return {"switch": name, "reason": config.switch(name).get("reason")}
    return None


def person_shown(ctx: Ctx) -> bool:
    """Whether this call was shown to a person, as far as the transport can establish.

    On the command line that is a principal whose source is an interactive terminal: the
    client sets it only after reading a confirmation from a TTY, which a model's shell does
    not have. Over MCP it is a call to a tool the contract marks ``requires_user_interaction``,
    which makes the client ask its user on every call in every permission mode. Naming the
    operator scope is neither.
    """
    if ctx.principal.source == INTERACTIVE_SOURCE:
        return True
    if ctx.transport == "mcp_stdio" and ctx.tool:
        try:
            return bool(contracts.any_tool_spec(ctx.tool).get("requires_user_interaction"))
        except KeyError:
            return False
    return False


# ------------------------------------------------------------------------------ records

def _view(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "approval_id": row["approval_id"], "status": row["status"], "action_kind": row["action_kind"],
        "run_id": row["run_id"],
        "subject": {"kind": row["subject_kind"], "id": row["subject_id"], "sha256": row["subject_sha256"]},
        "requested_by": row["requested_by"], "requested_at": row["requested_at"],
        "decided_at": row["decided_at"], "granted_by": row["decided_by"] if row["status"] == GRANTED else None,
        "decided_by": row["decided_by"], "policy_ref": row["policy_ref"],
        "decision_note": row["decision_note"], "expires_at": row["expires_at"],
        "request": store.loads(row, "request_json", {}) or {},
        "decision_detail": store.loads(row, "decision_json", None),
    }


#: What a decision of a ``worker_refusal`` carries beyond the word (contract r5,
#: approvals.decision_json): ``{choice, note?, omit_sections?, model?}``, the choice a value of
#: labels.refusal_decision. reword and authorize_once are GRANTED, skip is DENIED.
WORKER_REFUSAL = contracts.require_label("approval_action_kind", "worker_refusal")
REFUSAL_CHOICES = tuple(contracts.label_values("refusal_decision"))
_CHOICE_DECISION = {"reword": GRANTED, "authorize_once": GRANTED, "skip": DENIED}
if set(_CHOICE_DECISION) != set(REFUSAL_CHOICES):  # pragma: no cover - contract drift
    raise RuntimeError("the refusal decisions of the contract and the decision each goes with have parted")
_DETAIL_KEYS = {"choice", "note", "omit_sections", "model"}


def check_decision_detail(action_kind: str, decision: str, detail: Any) -> dict[str, Any] | None:
    """The ``decision_detail`` a decision may carry, normalised; ``SCHEMA_INVALID`` otherwise.

    Only a ``worker_refusal`` carries one, and it must: reword (GRANTED, with a note for the
    next brief and the sections to leave out) or authorize_once (GRANTED, with the one model
    allowed for that task), or skip (DENIED). A REVOKED, or a decision of any other kind, is the
    word and the note alone.
    """
    if action_kind != WORKER_REFUSAL or decision == REVOKED:
        if detail is not None:
            raise SchemaInvalid(
                f"decision_detail is what a worker_refusal decision carries; a {decision} of {action_kind} is the "
                "word and the note", details={"action_kind": action_kind, "decision": decision})
        return None
    if not isinstance(detail, Mapping):
        raise SchemaInvalid("a worker_refusal is decided with decision_detail {choice, note?, omit_sections?, "
                            "model?}: reword or authorize_once with GRANTED, skip with DENIED",
                            details={"decision": decision})
    unknown = sorted(set(detail) - _DETAIL_KEYS)
    if unknown:
        raise SchemaInvalid(f"decision_detail has unknown key(s) {unknown}", details={"unknown": unknown})
    choice = detail.get("choice")
    if choice not in REFUSAL_CHOICES:
        raise SchemaInvalid(f"decision_detail.choice {choice!r} is not one of {list(REFUSAL_CHOICES)}",
                            details={"choice": choice})
    if _CHOICE_DECISION[choice] != decision:
        raise SchemaInvalid(f"choice {choice} goes with decision {_CHOICE_DECISION[choice]}, not {decision}",
                            details={"choice": choice, "decision": decision})
    out: dict[str, Any] = {"choice": choice}
    note = detail.get("note")
    if note is not None:
        if not (isinstance(note, str) and note.strip()):
            raise SchemaInvalid("decision_detail.note, when given, is text for the next brief", details={})
        out["note"] = note.strip()
    sections = detail.get("omit_sections")
    if sections is not None:
        if choice != "reword" or not isinstance(sections, list) or not all(
                isinstance(item, str) and item.strip() for item in sections):
            raise SchemaInvalid("decision_detail.omit_sections is a list of section names, given with reword",
                                details={"choice": choice})
        out["omit_sections"] = [item.strip() for item in sections]
    model = detail.get("model")
    if choice == "authorize_once":
        if not (isinstance(model, str) and model.strip()):
            raise SchemaInvalid("authorize_once names the model it authorizes for that one task", details={})
        out["model"] = model.strip()
    elif model is not None:
        raise SchemaInvalid("a model is named only with authorize_once", details={"choice": choice})
    return out


def get(conn: sqlite3.Connection, approval_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no approval {approval_id!r}", details={"approval_id": approval_id})
    return _view(row)


def _rows(conn: sqlite3.Connection, action_kind: str, subject: Subject) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM approvals WHERE action_kind=? AND subject_kind=? AND subject_id=?"
        " AND subject_sha256=? ORDER BY rowid",
        (action_kind, subject.kind, subject.id, subject.sha256),
    ))


def _set_status(
    conn: sqlite3.Connection, row: sqlite3.Row, to_state: str, *, actor: str, decided_by: str,
    policy_ref: str | None, note: str, payload: Mapping[str, Any] | None = None,
    detail: Mapping[str, Any] | None = None,
) -> None:
    machines.check_transition(MACHINE, row["status"], to_state, subject_id=row["approval_id"])
    conn.execute(
        "UPDATE approvals SET status=?, decided_at=?, decided_by=?, policy_ref=?, decision_note=?,"
        " decision_json=? WHERE approval_id=?",
        (to_state, clock.now_iso(), decided_by, policy_ref, note,
         store._j(dict(detail)) if detail is not None else None, row["approval_id"]),
    )
    event_id = store.append_event(
        conn, kind=f"approval.{to_state.lower()}", actor=actor, run_id=row["run_id"],
        subject_kind="approval", subject_id=row["approval_id"], prev_state=row["status"],
        new_state=to_state, reason=row["action_kind"], evidence_level="EXECUTION_OBSERVED",
        payload={"action_kind": row["action_kind"], "decided_by": decided_by, "policy_ref": policy_ref,
                 "subject": {"kind": row["subject_kind"], "id": row["subject_id"],
                             "sha256": row["subject_sha256"]}, **dict(payload or {})},
    )
    store.enqueue_outbox(
        conn, topic=DECIDED_TOPIC, idempotency_key=decided_trigger_key(row["approval_id"], to_state),
        payload=decided_trigger(row, to_state, event_id, decided_by),
    )


def _expire_if_due(conn: sqlite3.Connection, row: sqlite3.Row) -> sqlite3.Row:
    """An approval past ``expires_at`` is EXPIRED the next time anyone looks."""
    if row["expires_at"] and row["status"] in (PENDING, GRANTED) and clock.is_past(row["expires_at"]):
        with store.transaction(conn) as tx:
            _set_status(tx, row, EXPIRED, actor="backend", decided_by="expiry", policy_ref=row["policy_ref"],
                        note=f"expired at {row['expires_at']}")
        return conn.execute("SELECT * FROM approvals WHERE approval_id=?", (row["approval_id"],)).fetchone()
    return row


# ------------------------------------------------------------------------------ request

def _require_subject(subject: Subject) -> None:
    if not isinstance(subject, Subject) or not (subject.kind and subject.id and subject.sha256):
        raise SchemaInvalid(
            "an approval is bound to one subject and one content sha256; give a Subject with kind, id "
            "and sha256",
            details={"subject": subjects.as_dict(subject) if isinstance(subject, Subject) else repr(subject)},
        )


#: (action_kind, subject_kind) that only a person decides, whatever a standing rule for the kind says.
#: Reviving a task that ended its chain (contract r5 task.rules.recovery: "A person may revive the dead
#: task by a budget_increase approval that grants attempts"): budget_increase has a standing rule for
#: raising a budget line inside its ceiling, and that rule must never reach a task.
PERSON_ONLY_SUBJECTS: dict[tuple[str, str], str] = {
    (contracts.require_label("approval_action_kind", "budget_increase"), "task"): (
        "reviving a task that ended its chain is a person's decision (contract r5 task.rules.recovery); "
        "the standing policy never grants a budget_increase on a task"),
}


def _apply_policy(ctx: Ctx, row: sqlite3.Row, subject: Subject, request: Mapping[str, Any]) -> dict[str, Any]:
    """Grant under a standing rule when its conditions hold. Returns why not, otherwise."""
    action_kind = row["action_kind"]
    document, policy_sha = policy()
    outcome: dict[str, Any] = {"policy_sha256": policy_sha, "granted": False, "pending_reason": None}

    never = document.get("never_standing", {})
    if action_kind in never:
        outcome["pending_reason"] = f"{action_kind} is never granted by the standing policy: {never[action_kind]}"
        return outcome
    if (action_kind, subject.kind) in PERSON_ONLY_SUBJECTS:
        outcome["pending_reason"] = PERSON_ONLY_SUBJECTS[(action_kind, subject.kind)]
        return outcome
    forbidden = prohibited_by_switch(action_kind)
    if forbidden is not None:
        outcome["pending_reason"] = (
            f"switch {forbidden['switch']} of config/limits.json is off ({forbidden['reason']}); a switch "
            "is a prohibition and no approval unlocks it"
        )
        outcome["prohibited_by_switch"] = forbidden
        return outcome
    person_denied = [r for r in _rows(ctx.conn, action_kind, subject)
                     if r["status"] == DENIED and not str(r["decided_by"] or "").startswith("policy:")]
    if person_denied:
        outcome["pending_reason"] = (
            f"approval {person_denied[-1]['approval_id']} for the same subject and sha256 was denied by "
            f"{person_denied[-1]['decided_by']}; a standing rule does not overrule a decision"
        )
        return outcome
    rules = [rule for rule in document.get("rules", []) if rule.get("action_kind") == action_kind]
    if not rules:
        outcome["pending_reason"] = f"the standing policy has no rule for {action_kind}"
        return outcome
    evaluator = condition_for(action_kind)
    if evaluator is None:
        outcome["pending_reason"] = (
            f"no evaluator is published for the conditions of {action_kind}; a standing rule is "
            "applied on facts the backend checked, never on the request's word"
        )
        return outcome

    reasons: list[str] = []
    for rule in rules:
        result = evaluator(ctx, subject=subject, request=request, rule=rule)
        if not isinstance(result, ConditionResult):
            raise TypeError(f"the condition for {action_kind} returned {type(result).__name__}, not ConditionResult")
        if result.ok:
            instruction = document.get("instruction", {})
            policy_ref = f"config/{POLICY_FILE}#{rule['id']}@{policy_sha}"
            note = (
                f"Granted under standing rule {rule['id']!r} of config/{POLICY_FILE} (sha256 {policy_sha}). "
                f"It rests on the instruction given by {instruction.get('given_by', 'the user')} on "
                f"{instruction.get('given_on', '?')}: \"{instruction.get('quote', '')}\". "
                f"Conditions checked by the backend: {ids.canonical_json(result.facts)}"
            )
            with store.transaction(ctx.conn) as tx:
                _set_status(
                    tx, row, GRANTED, actor=ctx.actor, decided_by=f"policy:{policy_sha}",
                    policy_ref=policy_ref, note=note,
                    payload={"rule_id": rule["id"], "policy_sha256": policy_sha,
                             "instruction_quote": instruction.get("quote"), "facts": result.facts},
                )
            outcome.update(granted=True, rule_id=rule["id"], policy_ref=policy_ref, facts=result.facts)
            return outcome
        reasons.extend(f"{rule['id']}: {reason}" for reason in (result.reasons or ["conditions not met"]))
    outcome["pending_reason"] = "; ".join(reasons)
    return outcome


def request(
    ctx: Ctx,
    *,
    action_kind: str,
    subject: Subject,
    run_id: str | None = None,
    request: Mapping[str, Any],
) -> dict[str, Any]:
    """Ask for an approval of ``action_kind`` for ``subject`` and apply the standing policy at once.

    Returns the approval as a dict with ``status`` GRANTED or PENDING. A GRANTED approval for
    the same action, subject and sha256 that still holds is returned as it is; a PENDING one
    has the policy applied again, because the facts may have changed since.
    """
    contracts.require_label("approval_action_kind", action_kind)
    _require_subject(subject)
    conn = ctx.conn
    request_payload = dict(request or {})

    current: sqlite3.Row | None = None
    for row in reversed(_rows(conn, action_kind, subject)):
        row = _expire_if_due(conn, row)
        if row["status"] in (GRANTED, PENDING):
            current = row
            break
    if current is not None and current["status"] == GRANTED:
        return {**_view(current), "reused": True, "pending_reason": None}

    if current is None:
        approval_id = ids.new_id("approval")
        with store.transaction(conn) as tx:
            tx.execute(
                "INSERT INTO approvals(approval_id, run_id, action_kind, subject_kind, subject_id,"
                " subject_sha256, request_json, status, requested_by, requested_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (approval_id, run_id, action_kind, subject.kind, subject.id, subject.sha256,
                 store._j(request_payload), PENDING, ctx.actor, clock.now_iso()),
            )
            store.append_event(
                tx, kind="approval.requested", actor=ctx.actor, run_id=run_id, subject_kind="approval",
                subject_id=approval_id, new_state=PENDING, reason=action_kind,
                evidence_level="EXECUTION_OBSERVED",
                payload={"action_kind": action_kind, "subject": subjects.as_dict(subject)},
            )
        current = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
        reused = False
    else:
        reused = True

    outcome = _apply_policy(ctx, current, subject, request_payload)
    fresh = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (current["approval_id"],)).fetchone()
    view = _view(fresh)
    view.update(reused=reused, pending_reason=outcome["pending_reason"], policy_sha256=outcome["policy_sha256"])
    if outcome.get("prohibited_by_switch"):
        view["prohibited_by_switch"] = outcome["prohibited_by_switch"]
    if outcome.get("facts") is not None:
        view["facts"] = outcome["facts"]
    return view


def find_granted(conn: sqlite3.Connection, *, action_kind: str, subject: Subject) -> dict[str, Any] | None:
    for row in reversed(_rows(conn, action_kind, subject)):
        row = _expire_if_due(conn, row)
        if row["status"] == GRANTED:
            return _view(row)
    return None


def require_granted(conn: sqlite3.Connection, *, action_kind: str, subject: Subject) -> dict[str, Any]:
    """The GRANTED approval for exactly this action, subject and sha256, or ``APPROVAL_REQUIRED``."""
    contracts.require_label("approval_action_kind", action_kind)
    _require_subject(subject)
    granted = find_granted(conn, action_kind=action_kind, subject=subject)
    if granted is not None:
        return granted
    rows = _rows(conn, action_kind, subject)
    last = _view(rows[-1]) if rows else None
    raise ApprovalRequired(
        f"no granted approval of {action_kind} for {subject.kind} {subject.id} at sha256 "
        f"{subject.sha256[:16]}…" + (f" (approval {last['approval_id']} is {last['status']})" if last else ""),
        details={"action_kind": action_kind, "subject": subjects.as_dict(subject),
                 "approval_id": last["approval_id"] if last else None,
                 "status": last["status"] if last else None},
    )


# ------------------------------------------------------------------------------- decide

def decide(
    ctx: Ctx,
    *,
    approval_id: str,
    decision: str,
    note: str,
    expected_subject_sha256: str | None = None,
    policy_ref: str | None = None,
    decision_detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The operator surface: grant, deny or revoke one approval.

    With ``policy_ref`` the standing policy is applied again, now, and the grant is recorded
    as that policy. Without it this is a person's decision, which requires that the call
    was shown to a person; a model's shell naming the operator scope is not one.
    ``decision_detail`` is what the decision carries beyond the word (contract r5): for a
    ``worker_refusal`` the person's choice (:func:`check_decision_detail`), recorded as
    ``approvals.decision_json``; core applies it when it reads the decided approval.
    """
    if ctx.principal.scope != SCOPE_OPERATOR:
        raise PermissionDenied(
            "an approval is decided through the operator surface; a worker requests and never grants",
            details={"scope": ctx.principal.scope},
        )
    if decision not in (GRANTED, DENIED, REVOKED):
        raise SchemaInvalid(
            f"decision {decision!r} is not one of {[GRANTED, DENIED, REVOKED]}",
            details={"decision": decision},
        )
    if not (isinstance(note, str) and note.strip()):
        raise SchemaInvalid("a decision states its reason in the note")
    # contract r5: the caller's scope, the shape of the arguments, that a person is there to decide
    # or policy_ref names the standing policy -- and only then the approval itself
    if policy_ref is None and not person_shown(ctx):
        raise PermissionDenied(
            "this decision belongs to a person and the call was not shown to one; decide it where a "
            "person is, or name the standing policy with policy_ref",
            details={"approval_id": approval_id},
        )
    if policy_ref is not None and decision != GRANTED:
        raise PermissionDenied(
            f"the standing policy only grants; a {decision} is a person's decision",
            details={"approval_id": approval_id, "decision": decision},
        )
    if policy_ref is not None and decision_detail is not None:
        raise SchemaInvalid("a standing policy carries no choice of a person; a decision with decision_detail is "
                            "a person's", details={"approval_id": approval_id})
    conn = ctx.conn
    row = conn.execute("SELECT * FROM approvals WHERE approval_id=?", (approval_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound(f"no approval {approval_id!r}", details={"approval_id": approval_id})
    row = _expire_if_due(conn, row)
    if expected_subject_sha256 is not None and expected_subject_sha256 != row["subject_sha256"]:
        raise ContentHashMismatch(
            f"approval {approval_id} is bound to sha256 {str(row['subject_sha256'])[:16]}…, not "
            f"{expected_subject_sha256[:16]}…",
            details={"approval_id": approval_id, "bound_sha256": row["subject_sha256"],
                     "expected_subject_sha256": expected_subject_sha256},
        )
    subject = Subject(kind=row["subject_kind"], id=row["subject_id"], sha256=row["subject_sha256"])
    # what the person chose; the standing policy's own refusal of a kind it never grants comes below
    detail = None if policy_ref is not None else check_decision_detail(row["action_kind"], decision, decision_detail)

    if decision == GRANTED:
        forbidden = prohibited_by_switch(row["action_kind"])
        if forbidden is not None:
            raise PermissionDenied(
                f"switch {forbidden['switch']} of config/limits.json is off ({forbidden['reason']}); a "
                "switch is a prohibition and no approval unlocks it",
                details={"approval_id": approval_id, **forbidden},
            )
        if policy_ref is not None:
            outcome = _apply_policy(ctx, row, subject, store.loads(row, "request_json", {}) or {})
            if not outcome["granted"]:
                raise PermissionDenied(
                    f"the standing policy does not grant approval {approval_id}: {outcome['pending_reason']}",
                    details={"approval_id": approval_id, "pending_reason": outcome["pending_reason"]},
                )
            return get(conn, approval_id)

    with store.transaction(conn) as tx:
        _set_status(tx, row, decision, actor=ctx.actor, decided_by=f"person:{ctx.principal.subject}",
                    policy_ref=None, note=note, payload={"transport": ctx.transport}, detail=detail)
    return get(conn, approval_id)


#: The backend event that withdrew an approval, as ``invalidate`` records it in ``decided_by``:
#: one lower-case word, so that it never reads as a person's decision (``person:…``), the
#: standing policy's (``policy:…``) or a deadline passing (``expiry``).
_WITHDRAWN_BY = re.compile(r"[a-z][a-z0-9_]{0,63}")


def invalidate(conn: sqlite3.Connection, subject: Subject, *, actor: str, reason: str,
               decided_by: str = "requarantine", approval_id: str | None = None) -> dict[str, int]:
    """Withdraw every approval bound to this subject's sha256: PENDING → EXPIRED, GRANTED → REVOKED.

    Called on requarantine (the default ``decided_by``), and by the job service when a job ends
    before it starts, so that its PENDING approval stops counting as a waiter
    (``decided_by="job_ended"``). ``decided_by`` names the backend event that withdrew them.
    ``approval_id`` optionally narrows this to one already verified exact request.
    """
    if not isinstance(decided_by, str) or not _WITHDRAWN_BY.fullmatch(decided_by) or decided_by == "expiry":
        raise SchemaInvalid(
            "decided_by names the backend event that withdrew the approvals, as one lower-case word; a person's "
            "decision, the standing policy and a deadline passing are recorded by their own paths",
            details={"decided_by": repr(decided_by)[:80]})
    counts = {"revoked": 0, "expired": 0}
    if approval_id is not None and (not isinstance(approval_id, str) or not approval_id):
        raise SchemaInvalid("an exact approval invalidation names a nonempty approval_id")
    query = (
        "SELECT * FROM approvals WHERE subject_kind=? AND subject_id=? AND subject_sha256=?"
        " AND status IN (?, ?)")
    values = (subject.kind, subject.id, subject.sha256, GRANTED, PENDING)
    if approval_id is not None:
        # A caller that has verified one obsolete pending request can narrow the
        # subject invalidation without touching another run's or granted history.
        query += " AND approval_id=?"
        values += (approval_id,)
    rows = conn.execute(query, values).fetchall()
    for row in rows:
        target = REVOKED if row["status"] == GRANTED else EXPIRED
        _set_status(conn, row, target, actor=actor, decided_by=decided_by, policy_ref=row["policy_ref"],
                    note=reason)
        counts["revoked" if target == REVOKED else "expired"] += 1
    return counts
