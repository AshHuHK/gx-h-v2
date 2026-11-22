"""Persistent teams over the existing task, lease and budget authorities.

Management grants come from a coordinator and are fenced by leadership generation.
Neither a team name nor a message body grants authority. All local effects, their event
and the idempotency answer commit together. This module performs no Host calls.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Mapping

from .. import config, contracts
from ..context import Ctx, SCOPE_OPERATOR, SCOPE_WORKER
from ..domain import clock, ids, machines
from ..errors import (
    ArtifactHashMismatch, ArtifactNotFound, ContentHashMismatch, DependencyUnmet, IdempotencyConflict, IllegalTransition,
    ImplementationGap, LeaseExpired, OutcomeUnknown, PermissionDenied, PhaseDenied,
    SchemaInvalid, ScopeDenied, StaleRevision,
)
from ..persistence import store


@contextmanager
def _team_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        with store.transaction(conn) as tx:
            yield tx
    except IdempotencyConflict as error:
        # This local request rolled back. The earlier request retains its own receipt.
        # A failed rollback is raised separately as OutcomeUnknown by the store.
        error.side_effect_status = "ROLLED_BACK"
        raise


def _machine() -> Mapping[str, Any]:
    try:
        return contracts.states()["machines"]["team"]
    except KeyError as exc:
        raise ImplementationGap("the installed contract has no team management machine") from exc


def _rule(name: str) -> Any:
    try:
        return _machine()["rules"][name]
    except KeyError as exc:
        raise ImplementationGap(f"the team contract has no {name!r} rule") from exc


def _text(value: Any, field: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SchemaInvalid(f"{field} is non-empty text of at most {maximum} characters")
    return value


def _integer(value: Any, field: str, *, minimum: int = 0, maximum: int = (1 << 63) - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise SchemaInvalid(f"{field} is an integer between {minimum} and {maximum}")
    return value


def _shape(payload: Mapping[str, Any], required: tuple[str, ...], optional: tuple[str, ...] = ()) -> None:
    missing = [key for key in required if key not in payload]
    extra = sorted(set(payload) - set(required) - set(optional))
    if missing or extra:
        raise SchemaInvalid("payload does not match its action", details={"missing": missing, "extra": extra})


def _json(value: Any, *, field: str, ceiling: int) -> str:
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise SchemaInvalid(f"{field} must be finite JSON") from exc
    if len(encoded.encode("utf-8")) > ceiling:
        raise SchemaInvalid(f"{field} exceeds its {ceiling} byte limit")
    return encoded


def _id(kind: str) -> str:
    return f"{kind}_{secrets.token_hex(16)}"


def _run(ctx: Ctx, run_id: str) -> sqlite3.Row:
    row = ctx.conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise ArtifactNotFound("the run does not exist", details={"run_id": run_id})
    if not ctx.principal.may_access_project(row["project_ref"]):
        raise ScopeDenied("the run is outside the caller's project scope")
    return row


def _require_pins(ctx: Ctx, run: sqlite3.Row) -> None:
    from ..orchestration import seams, service

    if run["status"] in machines.terminal_states("run") or run["status"] == "PAUSED":
        raise PhaseDenied("a stopped or paused run cannot change its team")
    if run["workflow_id"]:
        workflow = service._pinned_workflow(ctx.conn, run)
        service.require_pins(run, workflow)
        return
    if run["contract_sha256"] and run["contract_sha256"] != contracts.contract_digest()["__combined__"]:
        raise ContentHashMismatch("the run contract differs from its installed contract")
    if run["agent_policy_sha256"] and run["agent_policy_sha256"] != seams.agent_policy_for_run(run)[1]:
        raise ContentHashMismatch("the run's pinned agent policy changed")


def _team(conn: sqlite3.Connection, run_id: str, team_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM teams WHERE team_id=?", (_text(team_id, "team_id"),)).fetchone()
    if row is None:
        raise ArtifactNotFound("the team does not exist", details={"team_id": team_id})
    if row["run_id"] != run_id:
        raise ScopeDenied("a team belongs to another run")
    return row


def _task(conn: sqlite3.Connection, run_id: str, task_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM tasks WHERE task_id=?", (_text(task_id, "task_id"),)).fetchone()
    if row is None:
        raise ArtifactNotFound("the task does not exist", details={"task_id": task_id})
    if row["run_id"] != run_id:
        raise ScopeDenied("a task belongs to another run")
    return row


def _context(conn: sqlite3.Connection, run_id: str, ref: str) -> dict[str, Any]:
    """A portable team context is an immutable published artifact of the same run."""
    row = conn.execute("SELECT artifact_ref,run_id,sha256,bytes FROM artifacts WHERE artifact_ref=? AND published=1",
                       (_text(ref, "context_ref"),)).fetchone()
    if row is None:
        raise ArtifactNotFound("no published artifact for this team context")
    if row["run_id"] != run_id:
        raise ScopeDenied("the team context belongs to another run")
    return {"artifact_ref": row["artifact_ref"], "sha256": row["sha256"], "bytes": row["bytes"]}


def _ancestors(conn: sqlite3.Connection, run_id: str, parent_id: str | None) -> list[sqlite3.Row]:
    found, seen = [], set()
    while parent_id is not None:
        if parent_id in seen:
            raise SchemaInvalid("the stored team ancestry contains a cycle")
        seen.add(parent_id)
        row = _team(conn, run_id, parent_id)
        found.append(row)
        parent_id = row["parent_team_id"]
    return found


def _tree_depth(conn: sqlite3.Connection, run_id: str, team_id: str | None, parent_id: str | None) -> None:
    ancestors = _ancestors(conn, run_id, parent_id)
    if team_id is not None and team_id in {row["team_id"] for row in ancestors}:
        raise SchemaInvalid("reparent would create a team cycle")
    height = 0
    if team_id is not None:
        # The tree was validated at every write. UNION's path also detects corrupted
        # descendants instead of accepting a new parent above an existing cycle.
        frontier, seen = [(team_id, 0)], set()
        while frontier:
            node, depth = frontier.pop()
            if node in seen:
                raise SchemaInvalid("the stored team subtree contains a cycle")
            seen.add(node)
            height = max(height, depth)
            frontier.extend((row["team_id"], depth + 1) for row in conn.execute(
                "SELECT team_id FROM teams WHERE run_id=? AND parent_team_id=?", (run_id, node)))
    total = len(ancestors) + height
    maximum = _integer(_rule("max_depth"), "team.max_depth")
    if total > maximum:
        raise SchemaInvalid("the entire team subtree must fit the depth limit",
                            details={"subtree_max_depth": total, "max_depth": maximum})


def _accepts_work(conn: sqlite3.Connection, run_id: str, row: sqlite3.Row) -> None:
    for ancestor in [row, *_ancestors(conn, run_id, row["parent_team_id"])]:
        if ancestor["status"] != "ACTIVE":
            raise DependencyUnmet("the team and every ancestor must be active for new admission",
                                  details={"team_id": ancestor["team_id"], "status": ancestor["status"]})


def _observed_worker(ctx: Ctx, run: sqlite3.Row, subject: str, *, task_id: str | None = None) -> str | None:
    """Resolve identity from backend observations, never an enrollment's free text."""
    from ..hostobs import native_context
    if native_context.is_native_run(ctx.conn, run["run_id"]):
        call = native_context.call_for_ctx(ctx)
        if task_id is not None:
            from ..hostobs.native_observation import worker_for_claim
            return worker_for_claim(ctx, run_id=run["run_id"], task_id=task_id, principal_subject=subject)
        # Enrollment precedes claim. Root management authority is checked by the
        # ordinary TeamManager coordinator guard; actual identity comes from this
        # connection's registered official child metadata, never a request UUID.
        matches = [f"codex:{facts['session_id']}:{thread_id}"
                   for thread_id, facts in call._issuer.threads.items()
                   if call._issuer.principal_for(thread_id).subject == subject]
        return matches[0] if len(matches) == 1 else None
    if ctx.principal.scope == SCOPE_OPERATOR and ctx.principal.subject == subject \
            and ctx.principal.source == "interactive_terminal":
        return f"human:{subject}"
    from ..hostobs import observation
    policy_doc, _ = observation.policy_of_run(ctx.conn, run["run_id"])
    host = observation.run_host_kind(ctx.conn, run["run_id"])
    if host == "codex" and task_id is not None:
        if subject != ctx.principal.subject:
            return None
        from ..hostobs.codex_dispatch import verified_worker_for_claim
        return verified_worker_for_claim(ctx, run_id=run["run_id"], task_id=task_id, principal_subject=subject)
    hosts = [host] if host else contracts.label_values("host_kind")
    identities = set()
    for name in hosts:
        tie = observation.tie_of_subject(ctx.conn, host=name, subject=subject, policy_doc=policy_doc)
        if tie is not None and tie.get("borne_out_by"):
            identities.add(f"{name}:{tie['session_ref']}:{tie['agent_ref'] or 'main'}")
    if host == "claude_code" and task_id is not None:
        return next(iter(identities)) if len(identities) == 1 else None
    # An independently read record from a previous task is also a verified binding.
    for row in ctx.conn.execute(
        "SELECT DISTINCT w.observed_worker_ref FROM worker_observations w"
        " JOIN leases l ON l.lease_id=w.lease_id JOIN tasks t ON t.task_id=w.task_id"
        " WHERE t.run_id=? AND l.principal=? AND w.evidence_level='EXECUTION_OBSERVED'"
        " AND w.observation_source=? AND w.observed_worker_ref IS NOT NULL",
        (run["run_id"], subject, observation.SOURCE_TRANSCRIPT),
    ):
        identities.add(row["observed_worker_ref"])
    return next(iter(identities)) if len(identities) == 1 else None


def require_assignment(ctx: Ctx, task: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any] | None:
    """Atomically guard a claim. The caller holds the transaction issuing its lease.

    Old tasks with no assignment keep their existing semantics. Changing a leader or
    membership never touches an already issued task lease. This function writes nothing.
    """
    # Old-contract stores have no team tables; compatibility never manufactures them.
    if ctx.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='task_assignments'").fetchone() is None:
        return None
    assignment = ctx.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task["task_id"],)).fetchone()
    if assignment is None:
        return None
    run = _run(ctx, task["run_id"])
    if not ctx.conn.in_transaction:
        raise SchemaInvalid("assignment admission must share the transaction that issues the task lease")
    _require_pins(ctx, run)
    row = _team(ctx.conn, task["run_id"], assignment["team_id"])
    _accepts_work(ctx.conn, task["run_id"], row)
    if assignment["status"] != "ASSIGNED":
        raise DependencyUnmet("a pending handoff must settle before another claim")
    if assignment["subject"] != ctx.principal.subject:
        raise ScopeDenied("the task is assigned to another backend principal")
    member = ctx.conn.execute("SELECT * FROM team_members WHERE team_id=? AND subject=?",
                              (row["team_id"], ctx.principal.subject)).fetchone()
    if member is None or member["status"] != "ACTIVE":
        raise ScopeDenied("the claimant is not an active member of the assigned team")
    observed = _observed_worker(ctx, run, ctx.principal.subject, task_id=task["task_id"])
    if observed is None:
        raise ScopeDenied("the claimant has no unambiguous verified worker binding")
    for expected in (assignment["observed_worker_ref"], member["observed_worker_ref"]):
        if expected is not None and expected != observed:
            raise ScopeDenied("the verified worker differs from the assignment or membership binding")
    assignment_intent(ctx.conn, assignment)
    from ..artifacts.service import ArtifactService
    service = ArtifactService(ctx.conn)
    for product in assignment_artifacts(ctx.conn, task, assignment).values():
        checked = service.verify(product["artifact_ref"])
        if not checked["ok"]:
            raise ArtifactHashMismatch("an assigned artifact no longer matches its pinned bytes")
    return {**dict(assignment), "verified_worker_ref": observed}


def assignment_intent(conn: sqlite3.Connection, assignment: Mapping[str, Any] | sqlite3.Row) -> dict[str, Any]:
    """The exact immutable intent assigned to a task, independent of the current head."""
    row = conn.execute("SELECT * FROM team_intents WHERE team_id=? AND version=?",
                       (assignment["team_id"], assignment["intent_version"])).fetchone()
    if row is None:
        raise ArtifactNotFound("the assignment's immutable team intent does not exist")
    document = json.loads(row["intent_json"])
    if row["sha256"] != assignment["intent_sha256"] or ids.sha256_of_obj(document) != row["sha256"]:
        raise ContentHashMismatch("the assigned team intent no longer matches its pinned hash")
    return {"version": row["version"], "sha256": row["sha256"], "document": document}


def assignment_artifacts(conn: sqlite3.Connection, task: Mapping[str, Any],
                         assignment: Mapping[str, Any] | sqlite3.Row) -> dict[str, dict[str, Any]]:
    """Resolve already-published assignment products. Reading never manufactures one."""
    row = conn.execute("SELECT artifact_ref,sha256,bytes FROM artifacts WHERE run_id=? AND task_id=?"
                       " AND kind='team_intent' AND sha256=? AND published=1 ORDER BY created_at,artifact_ref LIMIT 1",
                       (task["run_id"], task["task_id"], assignment["intent_sha256"])).fetchone()
    if row is None:
        raise ArtifactNotFound("the assigned team intent has not been published", details={"task_id": task["task_id"]})
    products = {"team_intent": dict(row)}
    if assignment["context_ref"] is not None:
        products["context"] = _context(conn, task["run_id"], assignment["context_ref"])
    return products


class TeamManager:
    """Team intent and collaboration for one caller, with a single store authority."""

    def __init__(self, ctx: Ctx):
        self.ctx, self.conn = ctx, ctx.conn

    def _authorize(self, run_id: str, team: sqlite3.Row | None, scope: str,
                   coordinator_lease_ref: str | None, expected_generation: int | None,
                   *, coordinator_only: bool = False) -> None:
        if coordinator_lease_ref is not None:
            from ..orchestration.service import require_coordinator
            require_coordinator(self.ctx, self.conn, run_id, coordinator_lease_ref, renew=False)
            return
        if coordinator_only or team is None:
            raise ScopeDenied("this team operation requires the live run coordinator lease")
        if scope == "message" and expected_generation is None:
            member = self.conn.execute("SELECT * FROM team_members WHERE team_id=? AND subject=? AND status='ACTIVE'",
                                       (team["team_id"], self.ctx.principal.subject)).fetchone()
            observed = _observed_worker(self.ctx, _run(self.ctx, run_id), self.ctx.principal.subject)
            if member is not None and observed is not None and member["observed_worker_ref"] in (None, observed):
                return
        if expected_generation is None or expected_generation != team["leader_generation"]:
            raise LeaseExpired("the team leader generation has changed")
        if team["leader_subject"] != self.ctx.principal.subject or not team["leader_expires_at"] \
                or clock.is_past(team["leader_expires_at"]):
            raise LeaseExpired("the caller does not hold the team's current management lease")
        grant = self.conn.execute(
            "SELECT * FROM team_grants WHERE team_id=? AND subject=? AND generation=? AND revoked_at IS NULL",
            (team["team_id"], self.ctx.principal.subject, expected_generation)).fetchone()
        if grant is None or scope not in json.loads(grant["scopes_json"]):
            raise ScopeDenied("the backend has not granted this team management operation")

    def _view(self, team: sqlite3.Row | Mapping[str, Any], *, intent: bool = True) -> dict[str, Any]:
        value = dict(team)
        if intent:
            version = self.conn.execute("SELECT intent_json FROM team_intents WHERE team_id=? AND version=?",
                                        (value["team_id"], value["intent_version"])).fetchone()
            value["intent"] = json.loads(version["intent_json"]) if version else None
        value["logical_depth"] = len(_ancestors(self.conn, value["run_id"], value["parent_team_id"]))
        return value

    def _mutate(self, run_id: str, operation: str, payload: Mapping[str, Any], *,
                expected_revision: int, idempotency_key: str, coordinator_lease_ref: str | None,
                expected_generation: int | None, apply: Callable[[sqlite3.Row], tuple[str, dict[str, Any]]],
                message: bool = False, expected_team_revision: int | None = None) -> dict[str, Any]:
        if self.ctx.principal.scope not in (SCOPE_WORKER, SCOPE_OPERATOR):
            raise PermissionDenied("this principal may not manage teams")
        _text(run_id, "run_id")
        _text(idempotency_key, "idempotency_key", maximum=1024)
        _integer(expected_revision, "expected_revision")
        if expected_generation is not None:
            _integer(expected_generation, "expected_generation")
        if not isinstance(payload, Mapping):
            raise SchemaInvalid("payload is an object")
        # The scope and caller are part of the immutable product; a second principal
        # cannot replay an authority-bearing answer using a stolen request key.
        request = {"run_id": run_id, "operation": operation, "payload": dict(payload),
                   "principal": {"subject": self.ctx.principal.subject, "scope": self.ctx.principal.scope,
                                 "project_refs": sorted(set(self.ctx.principal.project_refs))},
                   "coordinator_lease_ref": coordinator_lease_ref,
                   "expected_generation": expected_generation, "expected_revision": expected_revision}
        if expected_team_revision is not None:
            _integer(expected_team_revision, "expected_team_revision", minimum=1)
            if operation in ("create", "reparent", "handoff") or message:
                raise SchemaInvalid("cross-team operations use expected_revision")
            request["expected_team_revision"] = expected_team_revision
        _json(request, field="team request", ceiling=32768)
        key = store.tool_key("gx_team_message" if message else "gx_team_manage", idempotency_key)
        with _team_transaction(self.conn) as tx:
            run = _run(self.ctx, run_id)
            idem = store.begin_idempotent(tx, key, "team.management", request)
            if idem.replay:
                return {**idem.response, "replayed": True}
            if not idem.fresh:
                raise OutcomeUnknown("an unfinished team operation must be reconciled")
            if expected_team_revision is not None:
                team = _team(tx, run_id, payload.get("team_id"))
                if team["revision"] != expected_team_revision:
                    raise StaleRevision("the team's revision has changed", details={
                        "team_id": team["team_id"], "expected": expected_team_revision,
                        "current": team["revision"]})
            elif not message:
                store.check_revision(tx, run_id, expected_revision)
            if run["status"] in machines.terminal_states("run"):
                raise PhaseDenied("a closed run accepts no new team operation")
            _require_pins(self.ctx, run)
            config.check_holds("teams")
            team_id, result = apply(run)
            if result.pop("_unchanged", False):
                event = tx.execute("SELECT event_id FROM events WHERE subject_kind='team_message' AND subject_id=?"
                                   " ORDER BY event_id LIMIT 1", (result["message_id"],)).fetchone()
                out = {"run_id": run_id, "revision": int(run["revision"]), "event_id": int(event[0]),
                       "replayed": False, "message": self._message_view(result["message_id"])}
                store.complete_idempotent(tx, key, out)
                return out
            if message:
                previous = revision = int(run["revision"])
            else:
                previous, revision = store.bump_revision(tx, run_id)

            if not message and operation != "create":
                tx.execute("UPDATE teams SET revision=revision+1,updated_at=? WHERE team_id=?",
                           (clock.now_iso(), team_id))
            event_id = store.append_event(
                tx, kind=f"team.{operation}", actor=self.ctx.actor, run_id=run_id,
                subject_kind="team_message" if message else "team",
                subject_id=result["message_id"] if message else team_id,
                prev_revision=previous, new_revision=revision, evidence_level="EXECUTION_OBSERVED",
                payload={"team_id": team_id, "operation": operation, **result})
            out = {"run_id": run_id, "revision": revision, "event_id": event_id, "replayed": False}
            if message:
                out["message"] = self._message_view(result["message_id"])
            else:
                out.update(team=self._view(_team(tx, run_id, team_id)), result=result)
            store.complete_idempotent(tx, key, out)
            # Successful coordinator activity renews the actual run lease. A team's
            # bounded management lease remains separate from every worker lease.
            if coordinator_lease_ref is not None:
                from ..orchestration.service import require_coordinator
                require_coordinator(self.ctx, tx, run_id, coordinator_lease_ref)
            return out

    def manage(self, *, run_id: str, action: str, payload: Mapping[str, Any], expected_revision: int,
               idempotency_key: str, coordinator_lease_ref: str | None = None,
               expected_generation: int | None = None, expected_team_revision: int | None = None) -> dict[str, Any]:
        actions = {"create": self._create, "revise": self._revise, "reparent": self._reparent,
                   "transition": self._transition, "leader": self._leader, "grant": self._grant,
                   "member": self._member, "assign": self._assign, "handoff": self._handoff}
        if not isinstance(action, str) or action not in actions:
            raise SchemaInvalid("unknown team management action", details={"actions": sorted(actions)})
        auth = {"coordinator_lease_ref": coordinator_lease_ref, "expected_generation": expected_generation}
        def apply(run):
            target = payload
            call = self.ctx.native_call
            if call is not None and isinstance(payload.get("subject"), str):
                # Resolve inside the operation, after replay. A later restart or
                # alias collision cannot change an existing immutable receipt.
                subject = payload["subject"]
                removing_legacy = action == "member" and payload.get("action") == "remove" and self.conn.execute(
                    "SELECT 1 FROM team_members WHERE team_id=? AND subject=?", (payload.get("team_id"), subject)).fetchone()
                cancelling = action == "handoff" and payload.get("cancel_pending") is True
                if not removing_legacy and not cancelling:
                    from ..hostobs.native_spawn import resolve_subject
                    target = {**payload, "subject":resolve_subject(self.conn, call, subject)}
            return actions[action](run, target, **auth)
        return self._mutate(run_id, action, payload, expected_revision=expected_revision,
                            idempotency_key=idempotency_key, **auth,
                            expected_team_revision=expected_team_revision, apply=apply)

    def _create(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("intent",), ("parent_team_id", "context_ref"))
        parent = _team(self.conn, run["run_id"], p["parent_team_id"]) if p.get("parent_team_id") else None
        self._authorize(run["run_id"], parent, "create_child", **auth)
        if parent is not None:
            _accepts_work(self.conn, run["run_id"], parent)
        _tree_depth(self.conn, run["run_id"], None, p.get("parent_team_id"))
        intent = self._intent(p["intent"])
        if p.get("context_ref") is not None:
            _context(self.conn, run["run_id"], p["context_ref"])
        team_id, now = _id("gxtm"), clock.now_iso()
        digest = ids.sha256_of_obj(p["intent"])
        self.conn.execute(
            "INSERT INTO teams(team_id,run_id,parent_team_id,status,intent_version,intent_sha256,"
            "context_ref,budget_ref,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (team_id, run["run_id"], p.get("parent_team_id"), _machine()["initial"], 1, digest,
             p.get("context_ref"), run["budget_ref"], now, now))
        self.conn.execute("INSERT INTO team_intents VALUES(?,?,?,?,?,?)",
                          (team_id, 1, digest, intent, self.ctx.actor, now))
        return team_id, {"created": True}

    def _intent(self, value: Any) -> str:
        if not isinstance(value, Mapping):
            raise SchemaInvalid("team intent is an object")
        _text(value.get("objective"), "intent.objective", maximum=4000)
        return _json(dict(value), field="team intent", ceiling=int(_rule("max_intent_bytes")))

    def _revise(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("team_id", "intent"), ("context_ref",))
        row = _team(self.conn, run["run_id"], p["team_id"])
        self._authorize(run["run_id"], row, "intent", **auth)
        if row["status"] in _machine()["terminal"] or row["status"] == "DRAINING":
            raise IllegalTransition("a draining or closed team's intent is immutable")
        intent = self._intent(p["intent"])
        context_ref = p.get("context_ref", row["context_ref"])
        if context_ref is not None:
            _context(self.conn, run["run_id"], context_ref)
        version, digest = row["intent_version"] + 1, ids.sha256_of_obj(p["intent"])
        self.conn.execute("INSERT INTO team_intents VALUES(?,?,?,?,?,?)",
                          (row["team_id"], version, digest, intent, self.ctx.actor, clock.now_iso()))
        self.conn.execute("UPDATE teams SET intent_version=?,intent_sha256=?,context_ref=? WHERE team_id=?",
                          (version, digest, context_ref, row["team_id"]))
        return row["team_id"], {"previous_intent_version": row["intent_version"]}

    def _reparent(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("team_id",), ("parent_team_id",))
        row = _team(self.conn, run["run_id"], p["team_id"])
        self._authorize(run["run_id"], row, "reparent", coordinator_only=True, **auth)
        if row["status"] in _machine()["terminal"] or row["status"] == "DRAINING":
            raise IllegalTransition("a draining or closed team cannot be reparented")
        parent_id = p.get("parent_team_id")
        _tree_depth(self.conn, run["run_id"], row["team_id"], parent_id)
        if parent_id is not None:
            _accepts_work(self.conn, run["run_id"], _team(self.conn, run["run_id"], parent_id))
        self.conn.execute("UPDATE teams SET parent_team_id=? WHERE team_id=?", (parent_id, row["team_id"]))
        return row["team_id"], {"previous_parent_team_id": row["parent_team_id"]}

    def _transition(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("team_id", "status"))
        row = _team(self.conn, run["run_id"], p["team_id"])
        self._authorize(run["run_id"], row, "lifecycle", **auth)
        target = _text(p["status"], "status")
        if target not in _machine()["transitions"].get(row["status"], ()):
            raise IllegalTransition("the team transition is not legal",
                                    details={"previous": row["status"], "requested": target})
        if target == "ACTIVE":
            for parent in _ancestors(self.conn, run["run_id"], row["parent_team_id"]):
                if parent["status"] != "ACTIVE":
                    raise DependencyUnmet("a team activates only below active ancestors")
        if target == "CLOSED":
            blockers = self._close_blockers(row)
            if any(blockers.values()):
                raise DependencyUnmet("the team must drain before closing", details=blockers)
        self.conn.execute("UPDATE teams SET status=? WHERE team_id=?", (target, row["team_id"]))
        return row["team_id"], {"previous_status": row["status"], "status": target}

    def _close_blockers(self, row: sqlite3.Row) -> dict[str, int]:
        task_terminals = tuple(machines.terminal_states("task"))
        job_terminals = tuple(machines.terminal_states("job"))
        def count(sql: str, args: tuple[Any, ...]) -> int:
            return int(self.conn.execute(sql, args).fetchone()[0])
        task_slots = ",".join("?" for _ in task_terminals)
        job_slots = ",".join("?" for _ in job_terminals)
        base = " FROM task_assignments a JOIN tasks t ON t.task_id=a.task_id WHERE a.team_id=?"
        return {
            "children": count("SELECT COUNT(*) FROM teams WHERE parent_team_id=? AND status!='CLOSED'", (row["team_id"],)),
            "tasks": count("SELECT COUNT(*)" + base + f" AND t.status NOT IN ({task_slots})", (row["team_id"], *task_terminals)),
            "jobs": count("SELECT COUNT(*) FROM jobs j JOIN task_assignments a ON a.task_id=j.task_id"
                          f" WHERE a.team_id=? AND j.status NOT IN ({job_slots})", (row["team_id"], *job_terminals)),
            "leases": count("SELECT COUNT(*) FROM leases l JOIN task_assignments a ON a.task_id=l.task_id"
                            " WHERE a.team_id=? AND l.released_at IS NULL AND l.expires_at>?", (row["team_id"], clock.now_iso())),
            "pending_transfers": count("SELECT COUNT(*) FROM task_assignments WHERE team_id=? AND status='PENDING_TRANSFER'", (row["team_id"],)),
            "incoming_transfers": count("SELECT COUNT(*) FROM task_assignments WHERE status='PENDING_TRANSFER'"
                                        " AND json_extract(pending_json,'$.team_id')=?", (row["team_id"],)),
            "reservations": count("SELECT COUNT(*) FROM budget_reservations b JOIN task_assignments a ON a.task_id=b.subject_id"
                                  " WHERE a.team_id=? AND b.subject_kind='task' AND b.status='HELD'", (row["team_id"],)),
        }

    def _leader(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("team_id", "subject", "lease_seconds"))
        row = _team(self.conn, run["run_id"], p["team_id"])
        self._authorize(run["run_id"], row, "leader", coordinator_only=True, **auth)
        if row["status"] in _machine()["terminal"]:
            raise IllegalTransition("a closed team cannot acquire a leader")
        subject = _text(p["subject"], "subject")
        seconds = _integer(p["lease_seconds"], "lease_seconds", minimum=1,
                           maximum=int(_rule("max_leader_lease_seconds")))
        generation = int(row["leader_generation"]) + 1
        self.conn.execute("UPDATE teams SET leader_subject=?,leader_generation=?,leader_expires_at=? WHERE team_id=?",
                          (subject, generation, clock.iso_after(seconds), row["team_id"]))
        self.conn.execute("UPDATE team_grants SET revoked_at=? WHERE team_id=? AND revoked_at IS NULL",
                          (clock.now_iso(), row["team_id"]))
        return row["team_id"], {"previous_leader": row["leader_subject"], "previous_generation": row["leader_generation"],
                                "leader_subject": subject, "leader_generation": generation}

    def _grant(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("team_id", "subject", "scopes"), ("task_ids",))
        row = _team(self.conn, run["run_id"], p["team_id"])
        self._authorize(run["run_id"], row, "grant", coordinator_only=True, **auth)
        if p["subject"] != row["leader_subject"] or not row["leader_expires_at"] or clock.is_past(row["leader_expires_at"]):
            raise ScopeDenied("a grant is bound to the current live leader")
        scopes = p["scopes"]
        if not isinstance(scopes, list) or any(not isinstance(scope, str) or scope not in _rule("grant_scopes") for scope in scopes):
            raise SchemaInvalid("scopes are values of the frozen team grant vocabulary")
        scopes = sorted(set(scopes))
        task_ids = p.get("task_ids", [])
        if not isinstance(task_ids, list) or len(task_ids) > int(_rule("max_read_rows")) \
                or any(not isinstance(value, str) for value in task_ids):
            raise SchemaInvalid("task_ids is a bounded array of task references")
        for task_id in task_ids:
            _task(self.conn, run["run_id"], task_id)
            assigned = self.conn.execute("SELECT team_id FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
            if assigned is not None and assigned["team_id"] != row["team_id"]:
                raise ScopeDenied("a task already assigned to another team is delegated only through handoff")
        self.conn.execute(
            "INSERT INTO team_grants(team_id,subject,generation,scopes_json,granted_by,created_at,revoked_at,task_ids_json)"
            " VALUES(?,?,?,?,?,?,NULL,?) ON CONFLICT(team_id,subject,generation)"
            " DO UPDATE SET scopes_json=excluded.scopes_json,granted_by=excluded.granted_by,created_at=excluded.created_at,"
            " revoked_at=NULL,task_ids_json=excluded.task_ids_json",
            (row["team_id"], p["subject"], row["leader_generation"], ids.canonical_json(scopes), self.ctx.actor,
             clock.now_iso(), ids.canonical_json(sorted(set(task_ids)))))
        return row["team_id"], {"subject": p["subject"], "generation": row["leader_generation"], "scopes": scopes,
                                "task_ids": sorted(set(task_ids))}

    def _member(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("team_id", "subject", "action"), ("observed_worker_ref",))
        row = _team(self.conn, run["run_id"], p["team_id"])
        self._authorize(run["run_id"], row, "members", **auth)
        subject = _text(p["subject"], "subject")
        if p["action"] not in ("add", "remove"):
            raise SchemaInvalid("member action is add or remove")
        if p["action"] == "add":
            if row["status"] in _machine()["terminal"] or row["status"] == "DRAINING":
                raise IllegalTransition("a draining or closed team accepts no new member")
            expected = p.get("observed_worker_ref")
            if expected is not None:
                _text(expected, "observed_worker_ref")
                if expected != _observed_worker(self.ctx, run, subject):
                    raise ScopeDenied("the requested membership binding is not a verified worker")
            now = clock.now_iso()
            self.conn.execute("INSERT INTO team_members VALUES(?,?,?,?,?,?) ON CONFLICT(team_id,subject)"
                              " DO UPDATE SET observed_worker_ref=excluded.observed_worker_ref,status=excluded.status,updated_at=excluded.updated_at",
                              (row["team_id"], subject, expected, "ACTIVE", now, now))
        else:
            if self.conn.execute("SELECT 1 FROM team_members WHERE team_id=? AND subject=?",
                                 (row["team_id"], subject)).fetchone() is None:
                raise ArtifactNotFound("the member does not exist")
            self.conn.execute("UPDATE team_members SET status='LEFT',updated_at=? WHERE team_id=? AND subject=?",
                              (clock.now_iso(), row["team_id"], subject))
        return row["team_id"], {"subject": subject, "membership": p["action"], "worker_leases_preserved": True}

    def _assignment_target(self, run: sqlite3.Row, team: sqlite3.Row, p: Mapping[str, Any]) -> dict[str, Any]:
        _accepts_work(self.conn, run["run_id"], team)
        subject = _text(p["subject"], "subject")
        member = self.conn.execute("SELECT * FROM team_members WHERE team_id=? AND subject=?",
                                   (team["team_id"], subject)).fetchone()
        if member is None or member["status"] != "ACTIVE":
            raise ScopeDenied("the assignment requires active team membership")
        expected = p.get("observed_worker_ref", member["observed_worker_ref"])
        if expected is not None:
            _text(expected, "observed_worker_ref")
            if member["observed_worker_ref"] not in (None, expected) or _observed_worker(self.ctx, run, subject) != expected:
                raise ScopeDenied("the assignment worker binding is not verified")
        context_ref = p.get("context_ref", team["context_ref"])
        if context_ref is not None:
            _context(self.conn, run["run_id"], context_ref)
        return {"team_id": team["team_id"], "subject": subject, "observed_worker_ref": expected, "context_ref": context_ref,
                "intent_version": team["intent_version"], "intent_sha256": team["intent_sha256"]}

    def _publish_context_and_brief(self, run_id: str, task_id: str, *, delegated_by: str | None) -> dict[str, Any]:
        # Freeze eligibility at the GX handoff, before a delayed worker can acquire.
        self.conn.execute(
            "UPDATE task_assignments SET delegated_by=?,offered_attempt=(SELECT attempt FROM tasks WHERE task_id=?),"
            "offered_control_epoch=(SELECT control_epoch FROM runs WHERE run_id=?),"
            "offered_leader_generation=(SELECT leader_generation FROM teams WHERE teams.team_id=task_assignments.team_id) "
            "WHERE task_id=?", (delegated_by, task_id, run_id, task_id))
        from ..artifacts.service import ArtifactService
        from .service import TeamService
        assignment = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (task_id,)).fetchone()
        intent = assignment_intent(self.conn, assignment)
        service = ArtifactService(self.conn)
        service.put(run_id=run_id, kind="team_intent", media_type="application/json",
                    content=ids.canonical_json(intent["document"]), task_id=task_id,
                    origin="BACKEND", put_by=self.ctx.actor)
        brief_ref, brief_sha = TeamService(self.conn, ctx=self.ctx)._compile_brief(self.conn, task_id, raise_errors=True)
        if brief_ref is None or brief_sha is None:
            raise ImplementationGap("an assigned task requires a real compiled brief with its frozen context")
        return {"brief_ref": brief_ref, "brief_sha256": brief_sha}

    def _busy_task(self, task_id: str) -> bool:
        from .recovery import live_lease
        if live_lease(self.conn, task_id) is not None:
            return True
        terminal = tuple(machines.terminal_states("job"))
        return self.conn.execute("SELECT 1 FROM jobs WHERE task_id=? AND status NOT IN (" + ",".join("?" for _ in terminal) + ") LIMIT 1",
                                 (task_id, *terminal)).fetchone() is not None

    def _require_task_grant(self, team: sqlite3.Row, task_id: str, coordinator_lease_ref: str | None) -> None:
        if coordinator_lease_ref is not None:
            return
        grant = self.conn.execute("SELECT task_ids_json FROM team_grants WHERE team_id=? AND subject=?"
                                  " AND generation=? AND revoked_at IS NULL",
                                  (team["team_id"], self.ctx.principal.subject, team["leader_generation"])).fetchone()
        if grant is None or task_id not in json.loads(grant["task_ids_json"]):
            raise ScopeDenied("the coordinator has not delegated this exact task to the team")

    def _assign(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("team_id", "task_id", "subject"), ("observed_worker_ref", "context_ref"))
        row = _team(self.conn, run["run_id"], p["team_id"])
        self._authorize(run["run_id"], row, "assign", **auth)
        task = _task(self.conn, run["run_id"], p["task_id"])
        self._require_task_grant(row, task["task_id"], auth.get("coordinator_lease_ref"))
        if self.conn.execute("SELECT 1 FROM task_assignments WHERE task_id=?", (p["task_id"],)).fetchone():
            raise IllegalTransition("The task already has an assignment. Enroll the intended backend_subject with member add, "
                                    "then use handoff with team_id, task_id, recipient_team_id and subject. "
                                    "For a correction within one team use the same team ID twice.")
        if task["status"] in machines.terminal_states("task") or self._busy_task(task["task_id"]):
            raise DependencyUnmet("a first assignment requires a nonterminal task with no live lease or job")
        target, now = self._assignment_target(run, row, p), clock.now_iso()
        self.conn.execute("INSERT INTO task_assignments(task_id,team_id,subject,observed_worker_ref,context_ref,"
                          "assignment_generation,status,pending_json,assigned_by,created_at,updated_at,intent_version,intent_sha256)"
                          " VALUES(?,?,?,?,?,1,?,NULL,?,?,?,?,?)",
                          (task["task_id"], row["team_id"], target["subject"], target["observed_worker_ref"],
                           target["context_ref"], "ASSIGNED", self.ctx.actor, now, now,
                           target["intent_version"], target["intent_sha256"]))
        brief = self._publish_context_and_brief(run["run_id"], task["task_id"],
            delegated_by=None if auth.get("coordinator_lease_ref") else self.ctx.principal.subject)
        return row["team_id"], {"task_id": task["task_id"], "assignment_generation": 1, "status": "ASSIGNED", **brief}

    def _handoff(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("team_id", "task_id", "recipient_team_id", "subject"), ("observed_worker_ref", "context_ref", "cancel_pending"))
        source = _team(self.conn, run["run_id"], p["team_id"])
        target_team = _team(self.conn, run["run_id"], p["recipient_team_id"])
        self._authorize(run["run_id"], source, "assign", **auth)
        _task(self.conn, run["run_id"], p["task_id"])
        assigned = self.conn.execute("SELECT * FROM task_assignments WHERE task_id=?", (p["task_id"],)).fetchone()
        if assigned is None or assigned["team_id"] != source["team_id"]:
            raise ScopeDenied("the task is not assigned to the sending team")
        if "cancel_pending" in p and not isinstance(p["cancel_pending"], bool):
            raise SchemaInvalid("cancel_pending is a boolean")
        if p.get("cancel_pending"):
            if assigned["pending_json"] is None:
                raise DependencyUnmet("the task has no pending handoff to cancel")
            pending = json.loads(assigned["pending_json"])
            if pending["team_id"] != target_team["team_id"] or pending["subject"] != p["subject"]:
                raise ScopeDenied("the cancellation must identify the exact pending recipient")
            self.conn.execute("UPDATE task_assignments SET status='ASSIGNED',pending_json=NULL,updated_at=? WHERE task_id=?",
                              (clock.now_iso(), p["task_id"]))
            return source["team_id"], {"task_id": p["task_id"], "status": "ASSIGNED", "handoff_cancelled": True,
                                       "worker_leases_preserved": True}
        self._authorize(run["run_id"], target_team, "assign", **auth)
        self._require_task_grant(target_team, p["task_id"], auth.get("coordinator_lease_ref"))
        target = self._assignment_target(run, target_team, p)
        if assigned["pending_json"] and json.loads(assigned["pending_json"]) != target:
            raise IdempotencyConflict("a different handoff is already pending for the task")
        if self._busy_task(p["task_id"]):
            self.conn.execute("UPDATE task_assignments SET status='PENDING_TRANSFER',pending_json=?,updated_at=? WHERE task_id=?",
                              (ids.canonical_json(target), clock.now_iso(), p["task_id"]))
            return source["team_id"], {"task_id": p["task_id"], "status": "PENDING_TRANSFER", "worker_leases_preserved": True}
        self.conn.execute("UPDATE task_assignments SET team_id=?,subject=?,observed_worker_ref=?,context_ref=?,"
                          "assignment_generation=assignment_generation+1,status='ASSIGNED',pending_json=NULL,assigned_by=?,"
                          "updated_at=?,intent_version=?,intent_sha256=? WHERE task_id=?",
                          (target["team_id"], target["subject"], target["observed_worker_ref"], target["context_ref"],
                           self.ctx.actor, clock.now_iso(), target["intent_version"], target["intent_sha256"], p["task_id"]))
        brief = self._publish_context_and_brief(run["run_id"], p["task_id"],
            delegated_by=None if auth.get("coordinator_lease_ref") else self.ctx.principal.subject)
        return source["team_id"], {"task_id": p["task_id"], "recipient_team_id": target["team_id"], "status": "ASSIGNED",
                                   "assignment_generation": assigned["assignment_generation"] + 1, **brief}

    def message(self, *, run_id: str, action: str, payload: Mapping[str, Any], expected_revision: int,
                idempotency_key: str, coordinator_lease_ref: str | None = None,
                expected_generation: int | None = None) -> dict[str, Any]:
        if action not in ("send", "ack"):
            raise SchemaInvalid("message action is send or ack")
        auth = {"coordinator_lease_ref": coordinator_lease_ref, "expected_generation": expected_generation}
        method = self._send if action == "send" else self._ack
        return self._mutate(run_id, "message_sent" if action == "send" else "message_acknowledged", payload,
                            expected_revision=expected_revision, idempotency_key=idempotency_key, **auth,
                            message=True, apply=lambda run: method(run, payload, **auth))

    def _send(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("sender_team_id", "recipient_team_id", "kind", "correlation_key"),
               ("body", "task_id", "context_ref", "causal_event_id", "artifact_refs"))
        sender = _team(self.conn, run["run_id"], p["sender_team_id"])
        recipient = _team(self.conn, run["run_id"], p["recipient_team_id"])
        self._authorize(run["run_id"], sender, "message", **auth)
        if sender["status"] in _machine()["terminal"] or recipient["status"] in _machine()["terminal"]:
            raise IllegalTransition("a closed team accepts no new message")
        if p["kind"] not in _rule("message_kinds"):
            raise SchemaInvalid("message kind is not in the frozen vocabulary")
        _text(p["correlation_key"], "correlation_key")
        if p.get("task_id"):
            _task(self.conn, run["run_id"], p["task_id"])
        if p.get("context_ref") is not None:
            _context(self.conn, run["run_id"], p["context_ref"])
        if p.get("causal_event_id") is not None:
            causal = _integer(p["causal_event_id"], "causal_event_id", minimum=1)
            event = self.conn.execute("SELECT run_id FROM events WHERE event_id=?", (causal,)).fetchone()
            if event is None or event["run_id"] != run["run_id"]:
                raise ScopeDenied("the message's causal event is not an event of its run")
        refs = p.get("artifact_refs", [])
        if not isinstance(refs, list) or len(refs) > 16 or any(not isinstance(ref, str) for ref in refs):
            raise SchemaInvalid("artifact_refs is an array of at most sixteen references")
        if refs:
            from ..artifacts.service import ArtifactService
            service = ArtifactService(self.conn)
            for ref in refs:
                if ids.kind_of(ref) != "artifact":
                    raise SchemaInvalid("message artifacts are published artifact references, never filesystem paths")
                desc = service.describe(ref)
                if desc.get("run_id") != run["run_id"]:
                    raise ScopeDenied("message artifacts belong to its run")
                checked = service.verify(ref)
                if not checked["ok"]:
                    if checked.get("reason") == "file_missing":
                        raise ArtifactNotFound("the message artifact's registered bytes are missing")
                    raise ArtifactHashMismatch("the message artifact no longer matches its published bytes")
        encoded = _json(dict(p), field="team message", ceiling=int(_rule("max_message_bytes")))
        digest = ids.sha256_of_obj(dict(p))
        previous = self.conn.execute("SELECT * FROM team_messages WHERE run_id=? AND sender_subject=? AND correlation_key=?",
                                     (run["run_id"], self.ctx.principal.subject, p["correlation_key"])).fetchone()
        if previous is not None:
            if previous["payload_sha256"] != digest:
                raise IdempotencyConflict("the correlation key already identifies a different message")
            return sender["team_id"], {"message_id": previous["message_id"], "deduplicated": True, "_unchanged": True}
        message_id = _id("gxtmsg")
        self.conn.execute("INSERT INTO team_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                          (message_id, run["run_id"], sender["team_id"], recipient["team_id"], self.ctx.principal.subject,
                           p["kind"], p.get("task_id"), None, p.get("context_ref"),
                           p.get("causal_event_id"), p["correlation_key"], digest, encoded, clock.now_iso()))
        return sender["team_id"], {"message_id": message_id, "deduplicated": False}

    def _ack(self, run: sqlite3.Row, p: Mapping[str, Any], **auth: Any) -> tuple[str, dict[str, Any]]:
        _shape(p, ("message_id",))
        row = self.conn.execute("SELECT * FROM team_messages WHERE message_id=?", (_text(p["message_id"], "message_id"),)).fetchone()
        if row is None:
            raise ArtifactNotFound("the message does not exist")
        if row["run_id"] != run["run_id"]:
            raise ScopeDenied("the message belongs to another run")
        recipient = _team(self.conn, run["run_id"], row["recipient_team_id"])
        self._authorize(run["run_id"], recipient, "message", **auth)
        existing = self.conn.execute("SELECT 1 FROM team_message_acks WHERE message_id=? AND subject=?",
                                     (row["message_id"], self.ctx.principal.subject)).fetchone()
        self.conn.execute("INSERT OR IGNORE INTO team_message_acks VALUES(?,?,?,?)",
                          (row["message_id"], self.ctx.principal.subject, recipient["team_id"], clock.now_iso()))
        return recipient["team_id"], {"message_id": row["message_id"], "_unchanged": existing is not None}

    def _message_view(self, message_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM team_messages WHERE message_id=?", (message_id,)).fetchone()
        value = dict(row)
        value["payload"] = json.loads(value.pop("payload_json"))
        acknowledgements, remaining = [], int(_rule("max_message_bytes")) // 2
        for record in self.conn.execute("SELECT subject FROM team_message_acks WHERE message_id=? ORDER BY subject LIMIT ?",
                                       (message_id, int(_rule("max_read_rows")))):
            size = len(record["subject"].encode("utf-8")) + 4
            if size > remaining:
                break
            acknowledgements.append(record["subject"])
            remaining -= size
        total = int(self.conn.execute("SELECT COUNT(*) FROM team_message_acks WHERE message_id=?", (message_id,)).fetchone()[0])
        value.update(acknowledged_by=acknowledgements, acknowledged_total=total,
                     acknowledged_truncated=total - len(acknowledgements))
        return value

    def snapshot(self, *, run_id: str, team_id: str | None = None,
                 after_event_id: int = 0, limit: int | None = None) -> dict[str, Any]:
        """A bounded, consistent read. Nothing here settles or renews anything."""
        maximum = int(_rule("max_read_rows"))
        limit = maximum if limit is None else _integer(limit, "limit", minimum=1, maximum=maximum)
        _integer(after_event_id, "after_event_id")
        owns = not self.conn.in_transaction
        if owns:
            self.conn.execute("BEGIN")
        try:
            run = _run(self.ctx, run_id)
            if team_id is not None:
                _team(self.conn, run_id, team_id)
            clause, args = "t.run_id=?", [run_id]
            if team_id is not None:
                clause += " AND t.team_id=?"
                args.append(team_id)
            queries = {
                "teams": ("SELECT t.* FROM teams t WHERE " + clause + " ORDER BY t.created_at,t.team_id", args),
                "members": ("SELECT m.* FROM team_members m JOIN teams t ON t.team_id=m.team_id WHERE " + clause + " ORDER BY m.team_id,m.subject", args),
                "assignments": ("SELECT a.* FROM task_assignments a JOIN teams t ON t.team_id=a.team_id WHERE " + clause + " ORDER BY a.task_id", args),
            }
            message_where = "m.run_id=? AND e.event_id>?"
            message_args = [run_id, after_event_id]
            if team_id is not None:
                message_where += " AND (m.sender_team_id=? OR m.recipient_team_id=?)"
                message_args.extend([team_id, team_id])
            # A correlated MIN avoids duplicate rows when another idempotency key sends
            # an identical correlation product and records a duplicate-send event.
            message_from = (" FROM team_messages m JOIN events e ON e.event_id=(SELECT MIN(z.event_id)"
                            " FROM events z WHERE z.subject_kind='team_message' AND z.subject_id=m.message_id"
                            " AND z.kind='team.message_sent') WHERE " + message_where)
            queries["messages"] = ("SELECT m.*,e.event_id AS event_id" + message_from + " ORDER BY e.event_id", message_args)
            out: dict[str, Any] = {"run_id": run_id, "revision": int(run["revision"]),
                                   "as_of_event_id": int(self.conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]),
                                   "counts": {}, "truncated": {}}
            remaining_bytes = 18000
            for name, (sql, parameters) in queries.items():
                total = int(self.conn.execute("SELECT COUNT(*) FROM (" + sql + ")", parameters).fetchone()[0])
                values = []
                for record in self.conn.execute(sql + " LIMIT ?", [*parameters, limit]):
                    if name == "teams":
                        value = self._view(record)
                    elif name == "messages":
                        value = {**self._message_view(record["message_id"]), "event_id": record["event_id"]}
                    else:
                        value = dict(record)
                        if "pending_json" in value:
                            value["pending"] = json.loads(value.pop("pending_json") or "null")
                    size = len(ids.canonical_json(value).encode("utf-8"))
                    if size > remaining_bytes:
                        break
                    remaining_bytes -= size
                    values.append(value)
                out[name], out["counts"][name], out["truncated"][name] = values, total, total - len(values)
            return out
        finally:
            if owns:
                self.conn.execute("ROLLBACK")
