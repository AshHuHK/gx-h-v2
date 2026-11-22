"""What a failure changes: the route, the bound it counts against, and what happens when that bound is spent.

:func:`route` is a pure function of a failure class and a mapping of facts. It reads the
class's row of :data:`FAILURE_ROUTES` (design r2.2a §6.2), applies the row's conditions
(first match wins), checks the bound of the route it lands on, and when that bound is spent
follows the row's ``when_spent`` for that route, then a route's own
:data:`ROUTE_WHEN_SPENT`. Every decision names the facts it used and, when a person or the
machine must act, a need in the closed vocabulary of r2.2 §2.8.2 (:mod:`.needs`) that says
who can clear it.

A fact the row needs and the caller did not give is refused (:class:`MissingFacts`), and a
fact given a value the row does not accept is refused too (:class:`IllegalFact`); neither is
guessed. ``resource`` is not decided without knowing whether the path's reservation still
holds, whether the machine can ever fit the job and whether the job needs a GPU;
``input_or_format`` and ``job_timeout_or_lost`` not without where the failure arrived
(``arrived_as``, R33), and each accepts only the arrivals it has; ``evaluator_bug`` is
accepted only from the runner or a reproduced case (R39). An unknown ``host_will_recover``
reads as False: a quota failure nobody can say will lift pauses (R22).

:data:`FAILURE_ROUTES` is what the lead freezes into ``errors.json`` at r5, printed by
:func:`table_for_contract`. :data:`ROUTE_BOUNDS` says which line of ``config/limits.json``
each route counts against and which fact counts what was used. A change of route,
condition, bound or need is a change of data, not of a function.

A machinery failure never counts against a method. Whether a failure does is the
contract's ``outcome_classes``; no route, condition or spent bound changes it.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Mapping

from .. import contracts
from . import needs
from .needs import MACHINE, NEED_KINDS, PERSON
from .snapshot import Limits, Snapshot

# ----------------------------------------------------------------------------- vocabulary

RETRY_TASK = "RETRY_TASK"        # the same task, next attempt, brief recompiled with the refusal
RETRY_PATH = "RETRY_PATH"        # a NEW execute_path for the same path, following the accepted one (R33)
REPAIR = "REPAIR"                # an onboarding or authoring task for a version that supersedes the failed one
WAIT = "WAIT"                    # the job waits for the resource; counted per path (R34)
BRANCH = "BRANCH"                # a planner is asked for a branch of the path that cites the failure
NEXT_PATH = "NEXT_PATH"          # the slot is freed; the next wave takes a deferred path of another family
REEXPLORE = "REEXPLORE"          # a gap is opened from the failure; going back to the literature is analyze's call
STOP_RANKING = "STOP_RANKING"    # evaluations under the specification stop counting; a successor is built
HOLD_FOR_USER = "HOLD_FOR_USER"  # held, and an approval asked of a person
PAUSE = "PAUSE"                  # what it reaches waits for its need; ``scope`` says what: run, path or task
END_CHAIN = "END_CHAIN"          # the chain ends out of its bound; its path ends untested and frees its slot
ROUTES = (RETRY_TASK, RETRY_PATH, REPAIR, WAIT, BRANCH, NEXT_PATH, REEXPLORE, STOP_RANKING, HOLD_FOR_USER, PAUSE,
          END_CHAIN)
#: What a PAUSE makes wait, as a decision carries it.
SCOPES = ("run", "path", "task")
#: What a row may declare beside those: resolved by the decision to the failure's path or task.
DEPENDENT = "dependent"
#: The one axis of pause scopes (the contract's ``pause_scope``), each with what it means.
PAUSE_SCOPES: dict[str, str] = {
    "run": "the whole run waits",
    "path": "the failure's path waits; the run pauses only when nothing else proceeds",
    "task": "the failure's task waits; the run pauses only when nothing else proceeds",
    DEPENDENT: "what a row may declare: the failure's path when it has one, else its task. A decision carries "
               "path or task, never dependent",
}

#: Where a failure can arrive (R33; r2.2a §6.2): a refused bundle, a lapsed lease, a run input
#: that fails its check, a failure carried inside an accepted report, a job's own outcome.
#: A class that asks names the arrivals it has under ``legal``.
ARRIVED_AS = ("refused_bundle", "lapse", "run_input", "accepted_report", "job")
#: Who may name an evaluator_bug (R39): the runner's own evidence, or a reproduced case.
EVALUATOR_BUG_SOURCES = ("runner", "reproduced")


def _need(kind: str, what: str, clear_by: str, *, approval: str | None = None) -> dict[str, Any]:
    """A row's need: what must be supplied and who can supply it. ``approval``: the action kind asked for."""
    subject = {"kind": "approval", "action_kind": approval} if approval is not None else None
    return needs.need(kind, what, clear_by=clear_by, subject=subject)


#: The routes that wait for a person or the machine, and so always carry a need: PAUSE and
#: HOLD_FOR_USER until it is supplied, END_CHAIN as what its untested path would need.
ROUTES_WITH_NEED = (PAUSE, HOLD_FOR_USER, END_CHAIN)

#: What a job that can never fit this machine leaves its path needing (ruling R-d): nothing
#: the machine does on its own supplies it.
_NEVER_FITS = _need("device_capacity", "a device, or a limit line, under which the job fits: as planned it never "
                                       "fits this machine", PERSON)


def _when_a_task_is_blocked(what: str) -> dict[str, Any]:
    """The need of a chain that ended out of attempts, which depends on where its failures arrived.

    A refused bundle or a lapse spends the task's own attempts: the task stays BLOCKED (r2.2
    §2.7.1) and a person revives it by granting attempts. A failure carried by an accepted
    report or a job spends executions of the path, each of them an ACCEPTED task: no task is
    BLOCKED, so there is nothing to name, and the path is untested with its class alone.
    """
    return {"fact": "arrived_as", "in": ["accepted_report", "job"], "then": None,
            "else": _need("task_blocked", what, PERSON)}

#: Each failure class of the contract (design r2.2a §6.2). Keys:
#: ``route`` the route it takes; ``requires`` facts without which it is not decided;
#: ``legal`` {fact: values} the values a required fact may have, any other is refused;
#: ``unless`` conditions that send it elsewhere ({fact, is | in, route, scope?}, first match
#: wins); ``when_spent`` {route: route or {fact, is, then, else}} for every bounded route the
#: class can reach, so the row is complete without :data:`ROUTE_WHEN_SPENT`; ``scope`` of a
#: PAUSE (``dependent``: the failure's path, else its task); ``unbounded`` routes this class
#: takes without their usual bound (a host's wait is not a path's); ``needs`` {route: need or
#: {fact, is | in, then, else}} for each route of :data:`ROUTES_WITH_NEED` the class can reach,
#: and for a route that goes on while its path is already untested: what a person or the
#: machine must supply (``{kind, what, clear_by, subject?}``, in the closed vocabulary; a side
#: of a choice may be None: nothing can be named).
FAILURE_ROUTES: dict[str, dict[str, Any]] = {
    "path_unreachable": {
        "route": PAUSE, "scope": DEPENDENT,
        "needs": {PAUSE: _need("path_reachability", "the host, mount and permission of the path to verify", PERSON)},
    },
    "source_insufficient": {"route": REEXPLORE},
    "skill_condition_missing": {
        "route": REPAIR, "when_spent": {REPAIR: END_CHAIN},
        "needs": {END_CHAIN: _need("readiness", "the skill, repaired to ACTIVE", PERSON)},
    },
    "input_or_format": {
        "route": RETRY_TASK, "requires": ["arrived_as"],
        "legal": {"arrived_as": ["refused_bundle", "run_input", "accepted_report", "job"]},
        "unless": [{"fact": "arrived_as", "is": "run_input", "route": PAUSE, "scope": "path"},
                   {"fact": "arrived_as", "in": ["accepted_report", "job"], "route": RETRY_PATH}],
        "when_spent": {RETRY_TASK: END_CHAIN, RETRY_PATH: END_CHAIN},
        "needs": {PAUSE: _need("input", "the run input that failed its schema or existence check (its role and "
                                        "sha256)", PERSON),
                  END_CHAIN: _when_a_task_is_blocked("more attempts for the task that ended the chain: none of "
                                                     "the attempts allowed gave a deliverable in the declared "
                                                     "format")},
    },
    "adapter_or_protocol": {
        "route": REPAIR, "when_spent": {REPAIR: END_CHAIN},
        "needs": {END_CHAIN: _need("readiness", "the adapter, repaired to ACTIVE", PERSON)},
    },
    "environment": {
        "route": REPAIR, "when_spent": {REPAIR: END_CHAIN},
        "needs": {END_CHAIN: _need("readiness", "the environment, probed HEALTHY", PERSON)},
    },
    "resource": {
        "route": WAIT, "requires": ["reservation_holds", "ever_fits", "gpu"],
        "unless": [
            {"fact": "ever_fits", "is": False, "route": BRANCH},
            {"fact": "reservation_holds", "is": False, "route": PAUSE, "scope": "path"},
        ],
        "when_spent": {WAIT: {"fact": "gpu", "is": True, "then": HOLD_FOR_USER, "else": END_CHAIN},
                       BRANCH: END_CHAIN},
        "needs": {PAUSE: _need("budget_line", "the path's budget reservation, which no longer holds", PERSON),
                  HOLD_FOR_USER: _need("approval", "a gpu_job approval: the device stayed busy for every wait the "
                                                   "path is allowed", PERSON, approval="gpu_job"),
                  BRANCH: _NEVER_FITS,
                  END_CHAIN: {"fact": "ever_fits", "is": False, "then": _NEVER_FITS,
                              "else": _need("device_capacity", "a device free enough for the job to start: it "
                                                               "stayed busy for every wait the path is allowed",
                                            MACHINE)}},
    },
    "job_timeout_or_lost": {
        "route": RETRY_TASK, "requires": ["arrived_as"],
        "legal": {"arrived_as": ["lapse", "accepted_report", "job"]},
        "unless": [{"fact": "arrived_as", "in": ["accepted_report", "job"], "route": RETRY_PATH}],
        "when_spent": {RETRY_TASK: END_CHAIN, RETRY_PATH: END_CHAIN},
        "needs": {END_CHAIN: _when_a_task_is_blocked("more attempts for the task that ended the chain: in none of "
                                                     "the attempts allowed did the job finish within its limit")},
    },
    "candidate_below_threshold": {"route": BRANCH, "when_spent": {BRANCH: NEXT_PATH, NEXT_PATH: REEXPLORE}},
    "method_inapplicable": {"route": NEXT_PATH, "when_spent": {NEXT_PATH: REEXPLORE}},
    "hypothesis_unsupported": {"route": REEXPLORE},
    "evaluator_bug": {
        "route": STOP_RANKING, "requires": ["source"], "legal": {"source": list(EVALUATOR_BUG_SOURCES)},
        "when_spent": {STOP_RANKING: HOLD_FOR_USER},
        "needs": {HOLD_FOR_USER: _need("approval", "a person's decision on an evaluator found wrong past its "
                                                   "repair bound", PERSON, approval="eval_spec_change")},
    },
    "host_capability_or_quota": {
        "route": PAUSE, "scope": "run", "unbounded": [WAIT],
        "unless": [{"fact": "host_will_recover", "is": True, "route": WAIT}],
        "needs": {PAUSE: _need("host_quota", "host capability or quota: a person says it is restored and continues",
                               PERSON)},
    },
    "host_model_policy": {"route": HOLD_FOR_USER,
                          "needs": {HOLD_FOR_USER: _need("approval", "the user's decision on the model", PERSON,
                                                         approval="model_switch")}},
    "host_model_refusal": {"route": HOLD_FOR_USER,
                           "needs": {HOLD_FOR_USER: _need("approval", "the user's decision on the refused brief",
                                                          PERSON, approval="worker_refusal")}},
}

#: The global repair line counts per run whatever its name says (design §11 Q10).
_PER_RUN = "the run: counted per run, whatever the line's name says; a handover never resets it"

#: The bound each route counts against: a line of ``config/limits.json`` (a field of
#: :class:`Limits`, or for one task's attempts its own ``max_attempts``) and the fact that
#: says how much of it was used. ``remaining`` bounds are spent at 0. ``line`` and ``scope``
#: are frozen into the contract, so they name no function or module of the code.
ROUTE_BOUNDS: dict[str, tuple[dict[str, str], ...]] = {
    RETRY_TASK: ({"line": "task_budget.max_attempts", "limit_fact": "max_attempts", "used": "attempts_consumed",
                  "scope": "the task (its max_attempts, stamped from the line at creation)"},),
    RETRY_PATH: ({"line": "task_budget.max_attempts", "limit": "max_attempts", "used": "executions_of_path",
                  "scope": "the path: its execute_path tasks across the chain"},),
    REPAIR: ({"line": "engineering_budget.max_repair_attempts_per_fingerprint",
              "limit": "max_repair_attempts_per_fingerprint", "used": "repairs_of_fingerprint",
              "scope": "the capability's, skill's or environment's fingerprint"},
             {"line": "engineering_budget.max_global_repair_rounds_per_session",
              "limit": "max_global_repair_rounds_per_session", "used": "repair_rounds", "scope": _PER_RUN}),
    STOP_RANKING: ({"line": "engineering_budget.max_repair_attempts_per_fingerprint",
                    "limit": "max_repair_attempts_per_fingerprint", "used": "repairs_of_fingerprint",
                    "scope": "the evaluation specification's sha256"},
                   {"line": "engineering_budget.max_global_repair_rounds_per_session",
                    "limit": "max_global_repair_rounds_per_session", "used": "repair_rounds", "scope": _PER_RUN}),
    WAIT: ({"line": "run_budget.resource_waits_per_path", "limit": "resource_waits_per_path",
            "used": "waits_of_path", "scope": "the path: its WAITs and not_started outcomes"},),
    BRANCH: ({"line": "research_budget.initial_candidate_paths", "limit": "initial_candidate_paths",
              "used": "branches_this_round", "scope": "the round"},),
    NEXT_PATH: ({"line": "the candidate set", "remaining": "candidates_left",
                 "scope": "validated paths of another family that no wave has selected"},),
    REEXPLORE: (),     # applied where analyze rewinds: ROUTE_BOUNDS_AT_ANALYZE
    HOLD_FOR_USER: (),
    PAUSE: (),
    END_CHAIN: (),
}

#: Bounds a route counts against that :func:`route` does not check: going back to the
#: literature is analyze's decision (``stoprules.evaluate``), which reads the line.
ROUTE_BOUNDS_AT_ANALYZE: dict[str, tuple[dict[str, str], ...]] = {
    REEXPLORE: ({"line": "research_budget.max_literature_replanning_rounds",
                 "scope": "the run's re-planning rounds, applied when the round closes"},),
}

#: Where a route goes when its bound is spent and the class's row names nothing for it.
ROUTE_WHEN_SPENT = {RETRY_TASK: END_CHAIN, RETRY_PATH: END_CHAIN, REPAIR: END_CHAIN, STOP_RANKING: HOLD_FOR_USER,
                    WAIT: END_CHAIN, BRANCH: NEXT_PATH, NEXT_PATH: REEXPLORE}

#: Routings counted from the ``schedule.routed`` events: one recorded landing is one use.
REPAIR_ROUTES = (REPAIR, STOP_RANKING)


def _taxonomy() -> Mapping[str, Any]:
    return contracts.errors()["failure_taxonomy"]


def outcome_classes() -> frozenset[str]:
    """The classes that describe what a method produced. Only these count against a method."""
    return frozenset(_taxonomy()["outcome_classes"]["values"])


def failure_classes() -> tuple[str, ...]:
    return tuple(_taxonomy()["classes"])


class MissingFacts(KeyError):
    """A route needs a fact the caller did not give. Nothing is decided without it."""

    def __init__(self, failure_class: str, missing: list[str]):
        super().__init__(f"route({failure_class!r}) needs {missing}; it does not decide without them")
        self.failure_class = failure_class
        self.missing = list(missing)


class IllegalFact(ValueError):
    """A fact was given a value its class does not accept. Nothing is decided on it.

    An arrival the class does not have (a lapse reported as ``input_or_format``), or a class
    named by a source that may not name it (an executor naming ``evaluator_bug``, R39).
    """

    def __init__(self, failure_class: str, fact: str, value: Any, legal: Any):
        self.failure_class, self.fact, self.value, self.legal = failure_class, fact, value, tuple(legal)
        super().__init__(f"route({failure_class!r}) accepts {fact} in {list(self.legal)} only, not {value!r}; "
                         "it does not decide on a value it does not know")


#: The name r2 gave the refusal of an ``evaluator_bug`` from a source that may not name it.
NotAcceptedFrom = IllegalFact


@dataclass(frozen=True)
class Bound:
    line: str
    used: int
    limit: int | None
    spent: bool
    scope: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "used": self.used, "limit": self.limit, "spent": self.spent, "scope": self.scope}


@dataclass(frozen=True)
class RouteDecision:
    failure_class: str
    #: the route the class's row names
    declared: str
    #: what happens now: the declared route, a condition's route, or where a spent bound sent it
    route: str
    #: the routes left on the way, each with why
    via: tuple[tuple[str, str], ...]
    #: a bound was spent on the way
    spent: bool
    #: the bounds of every route checked, in order
    bounds: tuple[Bound, ...]
    #: the contract's outcome_classes: only these say something about the method
    counts_against_method: bool
    #: for PAUSE: what waits (run, path or task)
    scope: str | None
    #: what a person or the machine must supply: ``{kind, what, clear_by, why, subject?}`` in
    #: the closed vocabulary (None: nothing). Its ``subject`` is what the row could name;
    #: whoever records the need adds what the failure itself says (the approval's id, the input)
    need: Mapping[str, Any] | None
    reason: str
    facts: Mapping[str, Any] = field(default_factory=dict)

    def as_event(self) -> dict[str, Any]:
        """The payload of ``schedule.routed``; the snapshot reads failure_class, route, the task, path and job
        ids, fingerprint and round."""
        return {"failure_class": self.failure_class, "declared_route": self.declared, "route": self.route,
                "via": [list(step) for step in self.via], "spent": self.spent,
                "bounds": [bound.as_dict() for bound in self.bounds],
                "counts_against_method": self.counts_against_method, "scope": self.scope,
                "need": copy.deepcopy(dict(self.need)) if self.need else None, "reason": self.reason,
                "task_id": self.facts.get("task_id"), "path_id": self.facts.get("path_id"),
                "job_id": self.facts.get("job_id"),
                "fingerprint": self.facts.get("fingerprint"), "round": self.facts.get("round", 0)}


def _bounds(route_name: str, facts: Mapping[str, Any], limits: Limits, failure_class: str) -> tuple[Bound, ...]:
    out = []
    for spec in ROUTE_BOUNDS[route_name]:
        if "remaining" in spec:
            if facts.get(spec["remaining"]) is None:
                raise MissingFacts(failure_class, [spec["remaining"]])
            left = int(facts[spec["remaining"]])
            out.append(Bound(spec["line"], left, None, left <= 0, spec["scope"]))
            continue
        if facts.get(spec["used"]) is None:
            raise MissingFacts(failure_class, [spec["used"]])
        if "limit_fact" in spec:
            if facts.get(spec["limit_fact"]) is None:
                raise MissingFacts(failure_class, [spec["limit_fact"]])
            limit = int(facts[spec["limit_fact"]])
        else:
            value = getattr(limits, spec["limit"])
            if value is None:
                raise MissingFacts(failure_class, [spec["line"]])
            limit = int(value)
        used = int(facts[spec["used"]])
        out.append(Bound(spec["line"], used, limit, used >= limit, spec["scope"]))
    return tuple(out)


def _matches(condition: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    value = facts.get(condition["fact"])
    if "in" in condition:
        return value in condition["in"]
    return value is condition["is"] or (not isinstance(condition["is"], bool) and value == condition["is"])


def _either(choice: Mapping[str, Any], facts: Mapping[str, Any], failure_class: str) -> Any:
    """The side of a ``{fact, is, then, else}`` the facts choose. A fact not given is refused, not read as else."""
    if facts.get(choice["fact"]) is None:
        raise MissingFacts(failure_class, [choice["fact"]])
    return choice["then"] if _matches(choice, facts) else choice["else"]


def _spent_target(row: Mapping[str, Any], current: str, facts: Mapping[str, Any], failure_class: str) -> str | None:
    target = (row.get("when_spent") or {}).get(current)
    if isinstance(target, Mapping):
        return _either(target, facts, failure_class)
    return target if target is not None else ROUTE_WHEN_SPENT.get(current)


def _need_of(row: Mapping[str, Any], current: str, facts: Mapping[str, Any], failure_class: str
             ) -> Mapping[str, Any] | None:
    found = (row.get("needs") or {}).get(current)
    if isinstance(found, Mapping) and "fact" in found:
        return _either(found, facts, failure_class)
    return found


def route(failure_class: str, facts: Mapping[str, Any], *, limits: Limits,
          table: Mapping[str, Mapping[str, Any]] = FAILURE_ROUTES) -> RouteDecision:
    """The level a failure moves the plan to, the bound it counts against, and whether that is spent.

    ``facts`` holds the counts :func:`facts_for` computes and the facts the caller observed:
    ``arrived_as`` (where the failure came in, one of :data:`ARRIVED_AS`), ``reservation_holds``
    and ``ever_fits`` (resource), ``gpu`` (the job needs a GPU), ``host_will_recover`` (the
    host's record says the quota lifts), ``source`` (who named an evaluator_bug). ``limits``
    gives the lines.
    """
    if failure_class not in failure_classes():
        raise KeyError(f"{failure_class!r} is not a failure class of the contract")
    row = table[failure_class]
    missing = [name for name in row.get("requires", ()) if facts.get(name) is None]
    if missing:
        raise MissingFacts(failure_class, missing)
    for name, legal in (row.get("legal") or {}).items():
        if facts.get(name) not in legal:
            raise IllegalFact(failure_class, name, facts.get(name), legal)
    declared = current = row["route"]
    scope = row.get("scope")
    via: list[tuple[str, str]] = []
    for condition in row.get("unless", ()):
        if _matches(condition, facts):
            via.append((current, f"{condition['fact']} is {facts.get(condition['fact'])!r}"))
            current = condition["route"]
            scope = condition.get("scope", scope)
            break
    checked: list[Bound] = []
    spent_on_the_way = False
    seen = {current}
    while True:
        bounds = () if current in row.get("unbounded", ()) else _bounds(current, facts, limits, failure_class)
        checked.extend(bounds)
        spent = [bound for bound in bounds if bound.spent]
        if not spent:
            break
        spent_on_the_way = True
        following = _spent_target(row, current, facts, failure_class)
        if following is None or following in seen:
            break
        via.append((current, "spent: " + ", ".join(f"{bound.line} {bound.used} of {bound.limit}" for bound in spent)))
        current = following
        seen.add(current)
    need = _need_of(row, current, facts, failure_class)
    if current != PAUSE:
        scope = None
    elif scope == DEPENDENT:
        scope = "path" if facts.get("path_id") else "task"
    reason = f"{failure_class} routes to {declared}"
    if via:
        reason += "; " + "; ".join(f"left {step}: {why}" for step, why in via) + f"; lands on {current}"
    return RouteDecision(
        failure_class=failure_class, declared=declared, route=current, via=tuple(via), spent=spent_on_the_way,
        bounds=tuple(checked), counts_against_method=failure_class in outcome_classes(), scope=scope,
        need={**copy.deepcopy(dict(need)), "why": reason} if need else None, reason=reason,
        facts={key: facts[key] for key in sorted(facts)})


def facts_for(snapshot: Snapshot, *, task_id: str | None = None, path_id: str | None = None,
              fingerprint: str | None = None, job_id: str | None = None, **observed: Any) -> dict[str, Any]:
    """The counting facts of one failure, from a snapshot. Pure.

    ``fingerprint`` is what a repair is counted per: the capability's, skill's or
    environment's fingerprint, or the evaluation specification's sha256 for an evaluator
    bug. ``job_id`` is the job whose failure this is, when it is a job's. ``observed``
    carries what the caller saw and the snapshot cannot: ``arrived_as``,
    ``reservation_holds``, ``ever_fits`` (``capacity.fits``), ``gpu``,
    ``host_will_recover``, ``source``; a None value is left out, so ``route`` refuses a
    class that needs it.

    ``waits_of_path`` is how often the path waited for the device before this failure
    (design r2.2 §6.2: a WAIT or a not_started outcome counts per path). A wait that has a
    job row is that row, as the exec stream counts it; a wait that has none (refused at
    admission) is the routed landing that names no job, and so is the landing of a job exec
    does not count. A job's row and its own landing are one wait, and the failure being
    routed is not a wait before itself.
    """
    facts: dict[str, Any] = {"task_id": task_id, "path_id": path_id, "job_id": job_id, "fingerprint": fingerprint,
                             "round": snapshot.run.round_index}
    task = snapshot.by_id.get(task_id) if task_id is not None else None
    if task is not None:
        facts["attempt"] = task.attempt
        facts["attempts_consumed"] = task.attempts_consumed    # what max_attempts bounds (contract r5)
        facts["max_attempts"] = task.max_attempts
    repairs = [item for item in snapshot.routed if item.route in REPAIR_ROUTES]
    facts["repair_rounds"] = len(repairs)
    facts["repairs_of_fingerprint"] = sum(1 for item in repairs if fingerprint and item.fingerprint == fingerprint)
    facts["branches_this_round"] = sum(1 for item in snapshot.routed
                                       if item.route == BRANCH and item.round == snapshot.run.round_index)
    path = snapshot.path_by_id.get(path_id) if path_id is not None else None
    waited = set(path.resource_wait_jobs) if path is not None else set()
    facts["waits_of_path"] = len(waited - {job_id}) + sum(
        1 for item in snapshot.routed if item.route == WAIT and path_id is not None and item.path_id == path_id
        and item.job_id not in waited and (item.job_id is None or item.job_id != job_id))
    facts["executions_of_path"] = sum(1 for other in snapshot.tasks
                                      if other.kind == "execute_path" and path_id is not None
                                      and other.path_id == path_id)
    family = path.method_family if path is not None else None
    facts["candidates_left"] = sum(1 for other in snapshot.paths
                                   if other.status == "VALIDATED" and other.selection_seq is None
                                   and other.path_id != path_id and (family is None or other.method_family != family))
    for name, value in observed.items():
        if value is not None:
            facts[name] = value
    return facts


def landings(row: Mapping[str, Any]) -> tuple[str, ...]:
    """Every route a class's row can land on, in the order :func:`route` reaches them. Pure.

    Its route, the routes of its conditions, and, for each of those with a bound (and not
    ``unbounded``), where that bound sends it when spent, followed until nothing new.
    """
    found = list(dict.fromkeys([row["route"]] + [item["route"] for item in row.get("unless", ())]))
    index = 0
    while index < len(found):
        current = found[index]
        index += 1
        if not ROUTE_BOUNDS[current] or current in row.get("unbounded", ()):
            continue
        target = (row.get("when_spent") or {}).get(current, ROUTE_WHEN_SPENT.get(current))
        nexts = [target["then"], target["else"]] if isinstance(target, Mapping) else [target]
        found.extend(item for item in nexts if item is not None and item not in found)
    return tuple(found)


def table_for_contract(table: Mapping[str, Mapping[str, Any]] = FAILURE_ROUTES) -> dict[str, Any]:
    """The table as the lead freezes it into ``errors.json``: per class, its route object. A copy.

    ``bounds`` lists, for every route the class can land on, the lines it counts against
    (none for an ``unbounded`` route; ``applied_by: analyze`` for one only analyze checks).
    """
    out = {}
    for name in failure_classes():
        row = copy.deepcopy(dict(table[name]))
        bounds = {}
        for item in landings(row):
            if item in row.get("unbounded", ()):
                bounds[item] = []
                continue
            bounds[item] = ([{"line": spec["line"], "scope": spec["scope"]} for spec in ROUTE_BOUNDS[item]]
                            + [{"line": spec["line"], "scope": spec["scope"], "applied_by": "analyze"}
                               for spec in ROUTE_BOUNDS_AT_ANALYZE.get(item, ())])
        row["bounds"] = bounds
        row["counts_against_method"] = name in outcome_classes()
        out[name] = row
    return out


def vocabulary_for_contract() -> dict[str, Any]:
    """The closed vocabularies the table is written in, as the lead freezes them beside it."""
    return {"failure_route": list(ROUTES), "pause_scope": dict(PAUSE_SCOPES), "arrived_as": list(ARRIVED_AS),
            "evaluator_bug_source": list(EVALUATOR_BUG_SOURCES), **needs.vocabulary_for_contract()}


__all__ = [
    "ARRIVED_AS", "BRANCH", "Bound", "DEPENDENT", "END_CHAIN", "EVALUATOR_BUG_SOURCES", "FAILURE_ROUTES",
    "HOLD_FOR_USER",
    "IllegalFact", "MissingFacts", "NEED_KINDS", "NEXT_PATH", "NotAcceptedFrom", "PAUSE", "PAUSE_SCOPES", "REEXPLORE",
    "REPAIR",
    "REPAIR_ROUTES", "RETRY_PATH", "RETRY_TASK", "ROUTES", "ROUTES_WITH_NEED", "ROUTE_BOUNDS",
    "ROUTE_BOUNDS_AT_ANALYZE", "ROUTE_WHEN_SPENT", "RouteDecision", "SCOPES", "STOP_RANKING", "WAIT", "facts_for",
    "failure_classes", "landings", "outcome_classes", "route", "table_for_contract", "vocabulary_for_contract",
]
