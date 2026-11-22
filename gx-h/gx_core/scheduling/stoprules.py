"""Whether a run goes on: continue, back to the literature, conclude, pause, or stop.

Two pure functions, each returning a :class:`Decision` with its rule, its reason, what it
needs and the facts it used; core applies it and writes ``decision.as_event()`` under
``decision.event_kind``. This module touches no store.

* :func:`check` - any time the run is settled. STOP (``prohibition_broken``) when a line
  whose limit prohibits its use was spent on all the same (``budgets.line``'s ``broken``;
  ruling 17 of the r5 close). PAUSE only on a need in the closed vocabulary of design r2.2
  §2.8.2 (:mod:`.needs`): today a hold that stops every claim while work is open; a
  prohibited line is never one. Otherwise CONTINUE; when nothing is claimable, in flight, pending or
  waited for, the decision says ``quiescent`` and lists in ``ended`` each task that cannot
  move and that a person can revive, with why, what would revive it and its need (r2 §4.10,
  §2.5.2, §2.7): core then pauses on those needs, or, with none, completes carrying what is
  missing as untested, or stops. Exhausted tasks are not a reason for check itself to pause.
* :func:`evaluate` - when a round closes (the analyze stage), over :class:`StopFacts` that
  :func:`stop_facts` builds from a snapshot and the knowledge stream's ``research.progress``
  answers, which the caller reads and passes in.

**Three kinds of path** (design r2.2a §6.4, the lead's ruling R-e). At analyze a path

* has an **outcome**: the evaluation judged it (a PASS, or the outcome class its verdict
  gave), or it ended ``source_insufficient``, the one tested class that rests on no evaluation;
* is **inconclusive**: evaluated under a FROZEN specification, and the evaluator could not
  judge. It has no outcome and is not untested either; it counts for or against no method;
* is **untested**: no adjudicated verdict on record, an end class outside ``outcome_classes``
  plus ``source_insufficient`` (every machinery class, every ``never`` class), or a verdict
  that rests on a specification an ``evaluator_bug`` voided, until it is re-scored.

Where a judged path stands is the evaluation stream's to say: its reaction records
``standing`` on the path's ``path.evaluated`` event, and this module reads that word and
derives it from nothing else. A path is on exactly one of the decision's three lists
(``with_outcome``, ``inconclusive``, ``untested``), or on none: a path nobody selected,
judged or classed, and a path restated before any verdict, whose restatement is the one
listed. The lists say where every such path of the *run* stands when the round closes,
because the conclusion is about the run; the rules below decide on the paths of the round
that closes (``facts.decided_on``).

The round's rules, in the order they decide (r2.2a §6.4, §6.5, §2.8):

1. **No progress** (``no_progress``). When each of the last
   ``run_budget.stop_after_no_progress_rounds`` counted rounds added no reviewed evidence,
   covered no gap and evaluated no path, the run STOPs. A round whose searches all failed,
   or in which machinery stopped a path, is neutral: it neither counts nor breaks the count
   (R37; a proxy outage is not a dead subject). So is a round whose record left a counter
   out: a missing fact is not evidence of no progress.
2. **A round in which no path has an outcome never rewinds.** It PAUSEs
   (``untested_round``) when an untested path of the round has a need in the closed
   vocabulary that does not already read cleared. Otherwise it CONCLUDEs
   (``conclude_not_established``) when the run holds a recorded verdict under a FROZEN
   specification, in any round: such a conclusion says what was measured and what is
   missing, and supports no claim. With no such verdict it STOPs (``no_tested_path``).
3. **Back to the literature** (``rewind``): when every path of the round with an outcome
   ended in a class of ``reexplore_on``, re-planning rounds remain and a gap the round opened
   is not exhausted. Otherwise CONCLUDE (``conclude``).
4. **A spent line** (``budget_spent``): if the step decided needs a budget line that is
   spent, PAUSE with the line as the need, never stop. A prohibited line (a limit of 0 or
   less) is not spent: selection and job admission refuse a step that needs it.

Every rule is data in :data:`RULES`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Collection, Mapping, NamedTuple, Sequence

from .. import contracts
from . import dispatch
from .needs import NEED_KINDS, PERSON
from .needs import need as a_need  # ``needs`` is a field and an argument here, so the module is not named
from .snapshot import (
    RUN_TERMINAL, STANDING_INCONCLUSIVE, STANDING_OUTCOME, STANDINGS, BudgetLine, Limits, PathFacts, RoundFacts, RunFacts,
    Snapshot,
)

CONTINUE, REWIND, CONCLUDE, PAUSE, STOP, COMPLETE = "CONTINUE", "REWIND", "CONCLUDE", "PAUSE", "STOP", "COMPLETE"
#: The decision vocabulary (design r2.2 §2.8.6). COMPLETE is core's to take, when the
#: workflow's declared completion evidence holds; neither function of this module returns it.
#: A dead end is not a decision: it is input to quiescence.
DECISIONS = (CONTINUE, REWIND, CONCLUDE, PAUSE, STOP, COMPLETE)

#: The event a decision is written as: a round's decision carries the round's counters,
#: which ``snapshot.load`` reads back; a check's decision is about the run now.
EVENT_ROUND = "schedule.round_closed"
EVENT_CHECK = "schedule.checked"


def _taxonomy() -> Mapping[str, Any]:
    return contracts.errors()["failure_taxonomy"]


#: The rules as data. ``judged_classes``: the classes an evaluation gives a path, so a path
#: that carries one holds a recorded verdict; ``tested_classes``: the classes after which a
#: path has a scientific outcome (those, and ``source_insufficient``); ``rewind_on``: the
#: classes that send a run back to the literature, from the contract; ``void_spec_on``: a
#: class that voids every verdict under its specification; ``left_out_statuses``: path states
#: that take a path with no adjudicated verdict off every list (it was restated);
#: ``neutral_round``: the round flags that keep a round out of the no-progress count;
#: ``step_needs``: the budget lines each step spends.
RULES: dict[str, Any] = {
    "progress_counters": ("reviewed_added", "gaps_covered", "paths_evaluated"),
    "neutral_round": ("searches_all_failed", "machinery_failed", "incomplete"),
    "judged_classes": tuple(sorted(_taxonomy()["outcome_classes"]["values"])),
    "tested_classes": tuple(sorted(set(_taxonomy()["outcome_classes"]["values"]) | {"source_insufficient"})),
    "rewind_on": tuple(_taxonomy()["reexplore_on"]["values"]),
    "void_spec_on": ("evaluator_bug",),
    "left_out_statuses": ("SUPERSEDED",),
    "step_needs": {REWIND: ("worker_tasks",), CONCLUDE: ("worker_tasks",)},
}


class StandingMissing(ValueError):
    """A path has a path-level verdict on record and its record does not say where the path stands.

    Verdict to class is the evaluation stream's (design §9): scheduling does not derive an
    outcome from a verdict, so it does not close a round over such a path.
    """


@dataclass(frozen=True)
class PathOutcome:
    """One path of the run as analyze sees it."""

    path_id: str
    status: str
    #: the class that stands for it: the one the evaluation gave it when it has an outcome
    #: (None: it passed), else the class of its latest episode
    failure_class: str | None = None
    #: the specification its verdict was recorded under, else the one it was planned against
    eval_spec_sha256: str | None = None
    #: where the evaluation reaction left it, one of ``snapshot.STANDINGS`` (None: never judged)
    standing: str | None = field(default=None, kw_only=True)
    #: what it needs, when machinery stopped it: its routing's need ``{kind, what, clear_by, ...}``
    need: Mapping[str, Any] | None = field(default=None, kw_only=True)
    #: the candidate evaluations behind its verdict, and what the evaluator or the coverage said
    eval_ids: tuple[str, ...] = field(default=(), kw_only=True)
    reason: str | None = field(default=None, kw_only=True)
    #: its fate belongs to the round that closes (False: an earlier round's, listed and not decided on)
    current: bool = field(default=True, kw_only=True)
    #: the predicate of its need already holds: there is nothing left to wait for
    need_cleared: bool = field(default=False, kw_only=True)


@dataclass(frozen=True)
class GapState:
    gap_id: str
    status: str
    exhausted: bool
    opened_this_round: bool = False


@dataclass(frozen=True)
class StopFacts:
    round_index: int
    #: every path of the run that was selected, judged or classed; ``current`` marks the round's
    paths: tuple[PathOutcome, ...]
    #: closed rounds and the current one, oldest first
    rounds: tuple[RoundFacts, ...]
    gaps: tuple[GapState, ...]
    limits: Limits
    budget: tuple[BudgetLine, ...] = ()
    #: sha256 of the specifications an evaluator bug voided, in any round of the run
    voided_specs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Untested:
    path_id: str
    failure_class: str | None
    #: what it needs ``{kind, what, clear_by, ...}``; ``kind`` outside :data:`NEED_KINDS` (or
    #: None) is not something a person or the machine can clear
    need: Mapping[str, Any] | None
    #: the need's predicate already held when the round closed
    cleared: bool = False

    @property
    def clearable(self) -> bool:
        """Something can still be done about it: a need of the vocabulary that is not already clear."""
        return bool(self.need) and self.need.get("kind") in NEED_KINDS and not self.cleared

    def as_dict(self) -> dict[str, Any]:
        return {"path_id": self.path_id, "failure_class": self.failure_class,
                "need": dict(self.need) if self.need else None, "need_cleared": self.cleared}


@dataclass(frozen=True)
class Inconclusive:
    """A path evaluated under a FROZEN specification that the evaluator could not judge."""

    path_id: str
    eval_ids: tuple[str, ...]
    reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return {"path_id": self.path_id, "eval_ids": list(self.eval_ids), "reason": self.reason}


class Kinds(NamedTuple):
    """The paths given, each on exactly one list or left out."""

    with_outcome: list[tuple[str, str | None]]
    inconclusive: list[Inconclusive]
    untested: list[Untested]
    left_out: list[str]


@dataclass(frozen=True)
class Decision:
    decision: str
    #: the rule that decided (None: nothing stops the run)
    rule: str | None
    reason: str
    #: what a person or the machine must supply: needs of the closed vocabulary
    needs: tuple[Mapping[str, Any], ...] = ()
    #: nothing claimable, in flight, pending or waited for (check only): the stage decides now
    quiescent: bool = False
    #: tasks that cannot move and that a person can revive: {task_id, kind, status, why, revive,
    #: need}; the need is ``dispatch.revival``'s (check only). Core pauses on exactly these needs.
    ended: tuple[Mapping[str, Any], ...] = ()
    #: paths of the run with no scientific outcome and no adjudicated verdict; a conclusion lists every one
    untested: tuple[Untested, ...] = ()
    #: paths of the run with a scientific outcome, with their class (None: no failure)
    with_outcome: tuple[tuple[str, str | None], ...] = ()
    #: the round that closes, with its counters (round decisions only)
    round: RoundFacts | None = None
    facts: Mapping[str, Any] = field(default_factory=dict)
    #: paths of the run evaluated under a FROZEN specification that could not be judged; a
    #: conclusion lists them among its negative results, apart from the untested
    inconclusive: tuple[Inconclusive, ...] = ()

    @property
    def event_kind(self) -> str:
        return EVENT_ROUND if self.round is not None else EVENT_CHECK

    def as_event(self) -> dict[str, Any]:
        """The payload of :attr:`event_kind`; ``snapshot.load`` reads ``round`` back from a round's."""
        closed = None
        if self.round is not None:
            closed = {"index": self.round.index, "reviewed_added": self.round.reviewed_added,
                      "gaps_covered": self.round.gaps_covered, "paths_evaluated": self.round.paths_evaluated,
                      "searches_all_failed": self.round.searches_all_failed,
                      "machinery_failed": self.round.machinery_failed, "incomplete": self.round.incomplete}
        return {"decision": self.decision, "rule": self.rule, "reason": self.reason,
                "needs": [dict(need) for need in self.needs], "quiescent": self.quiescent,
                "ended": [dict(item) for item in self.ended], "untested": [item.as_dict() for item in self.untested],
                "inconclusive": [item.as_dict() for item in self.inconclusive],
                "with_outcome": [list(item) for item in self.with_outcome], "round": closed,
                "facts": dict(self.facts)}


def _spent_lines(budget: Sequence[BudgetLine], names: Sequence[str] | None = None) -> list[BudgetLine]:
    """Lines with nothing left that a budget increase could refill: a prohibited line is never one."""
    return [line for line in budget
            if line.exhausted and not line.prohibited and (names is None or line.name in names)]


def _budget_need(line: BudgetLine) -> dict[str, Any]:
    return a_need("budget_line", line.name, clear_by=PERSON, subject={"kind": "budget_line", "id": line.name},
                  why=f"{line.spent} spent and {line.reserved} reserved of {line.limit}")


def _hold_need(hold: str) -> dict[str, Any]:
    return a_need("hold", hold, clear_by=PERSON, subject={"kind": "hold", "id": hold},
                  why="config/limits.json holds every claim while work is open")


# ------------------------------------------------------------------------------- check

def check(snapshot: Snapshot, *, plan: "dispatch.Plan | None" = None) -> Decision:
    """STOP on a broken prohibition, PAUSE on a need in the closed vocabulary; otherwise CONTINUE. Pure.

    A CONTINUE says whether the run is quiescent. ``plan`` is the plan core already computed
    on the same snapshot, to save computing it again; it must be of this snapshot.
    """
    result = plan if plan is not None else dispatch.plan(snapshot)
    progress = result.progress
    facts = {"progress": progress.state, "as_of_event_id": snapshot.as_of_event_id, "as_of": snapshot.as_of}
    broken = [line for line in snapshot.budget if line.broken]
    if broken and snapshot.run.status not in RUN_TERMINAL:
        return Decision(STOP, "prohibition_broken", "spent against a line whose limit prohibits it: " + "; ".join(
            f"{line.name} ({line.spent} spent of {line.limit})" for line in broken),
                        facts={**facts, "broken": [line.name for line in broken]})
    open_work = bool(progress.in_flight or progress.claimable or progress.settling or progress.pending
                     or progress.awaiting_approval)
    if snapshot.limits.holds and open_work and snapshot.dispatching:
        return Decision(PAUSE, "hold", "config/limits.json holds every claim while work is open: "
                        + ", ".join(snapshot.limits.holds),
                        needs=tuple(_hold_need(hold) for hold in snapshot.limits.holds), facts=facts)
    ended = tuple({"task_id": item.task_id, "kind": item.kind, "status": item.status, "why": item.reason,
                   "revive": item.revive, "need": dict(item.need)} for item in progress.needs if item.need is not None)
    quiescent = progress.state in (dispatch.DEAD_END, dispatch.IDLE)
    return Decision(CONTINUE, None, f"the run's work is {progress.state}: {progress.reason}",
                    quiescent=quiescent, ended=ended, facts=facts)


# ------------------------------------------------------------------------------- rounds

def _no_progress(rounds: Sequence[RoundFacts], window: int, rules: Mapping[str, Any]) -> bool:
    counted = [item for item in rounds if not any(getattr(item, flag) for flag in rules["neutral_round"])]
    if window <= 0 or len(counted) < window:
        return False
    return all(all(getattr(item, name) == 0 for name in rules["progress_counters"]) for item in counted[-window:])


def classify(paths: Sequence[PathOutcome], rules: Mapping[str, Any] = RULES, *,
             voided: Collection[str] = ()) -> Kinds:
    """Each path on exactly one list: with an outcome, inconclusive, untested with what it needs, or left out. Pure.

    ``voided``: sha256 of specifications an evaluator bug voided before this round; the
    specification of a path given here whose class voids it is added.
    """
    tested = set(rules["tested_classes"])
    void = set(voided) | {path.eval_spec_sha256 for path in paths
                          if path.failure_class in set(rules["void_spec_on"]) and path.eval_spec_sha256}
    with_outcome: list[tuple[str, str | None]] = []
    inconclusive: list[Inconclusive] = []
    untested: list[Untested] = []
    left_out: list[str] = []
    for path in sorted(paths, key=lambda item: item.path_id):
        if path.standing is not None and path.standing not in STANDINGS:
            raise ValueError(f"{path.standing!r} is not a standing of a path; expected one of {list(STANDINGS)}")
        cls = path.failure_class
        judged = path.standing in (STANDING_OUTCOME, STANDING_INCONCLUSIVE)
        if path.status in rules["left_out_statuses"] and not judged:
            left_out.append(path.path_id)
        elif judged and path.eval_spec_sha256 in void:
            untested.append(Untested(path.path_id, cls, a_need(
                "readiness", f"re-scoring under a new FROZEN version of specification {path.eval_spec_sha256}, "
                             "which an evaluator bug voided", clear_by=PERSON)))
        elif path.standing == STANDING_INCONCLUSIVE:
            inconclusive.append(Inconclusive(path.path_id, tuple(path.eval_ids), path.reason))
        elif path.standing == STANDING_OUTCOME or cls in tested:
            with_outcome.append((path.path_id, cls))
        else:
            untested.append(Untested(path.path_id, cls, path.need, bool(path.need_cleared)))
    return Kinds(with_outcome, inconclusive, untested, left_out)


def evaluate(facts: StopFacts, *, rules: Mapping[str, Any] = RULES) -> Decision:
    """REWIND, CONCLUDE, PAUSE or STOP for the round that closes, with the rule and the facts. Pure."""
    limits = facts.limits
    current = facts.rounds[-1] if facts.rounds else None
    kinds = classify(facts.paths, rules, voided=facts.voided_specs)
    here = {path.path_id for path in facts.paths if path.current} - set(kinds.left_out)
    with_outcome = [item for item in kinds.with_outcome if item[0] in here]
    judged_classes = set(rules["judged_classes"])
    held = sorted([path for path, cls in kinds.with_outcome if cls is None or cls in judged_classes]
                  + [item.path_id for item in kinds.inconclusive])
    record = {"round_index": facts.round_index,
              "rounds_left": max(0, limits.max_literature_replanning_rounds - facts.round_index),
              "gaps_unexhausted_opened": sorted(gap.gap_id for gap in facts.gaps
                                                if gap.opened_this_round and not gap.exhausted),
              "decided_on": sorted(here), "left_out": list(kinds.left_out), "verdicts_held": held,
              "voided_specs": sorted(set(facts.voided_specs)),
              "needs_already_cleared": [item.path_id for item in kinds.untested if item.cleared]}
    common = dict(untested=tuple(kinds.untested), inconclusive=tuple(kinds.inconclusive),
                  with_outcome=tuple(kinds.with_outcome), round=current, facts=record)

    def decided(decision: str, rule: str, reason: str) -> Decision:
        spent = _spent_lines(facts.budget, rules["step_needs"].get(decision, ()))
        if spent:
            return Decision(PAUSE, "budget_spent",
                            f"{decision} needs {', '.join(line.name for line in spent)}, which is spent ({reason})",
                            needs=tuple(_budget_need(line) for line in spent), **common)
        return Decision(decision, rule, reason, **common)

    window = limits.stop_after_no_progress_rounds
    if _no_progress(facts.rounds, window, rules):
        return Decision(STOP, "no_progress", f"no progress in the last {window} counted round(s): no reviewed "
                                             "evidence added, no gap covered, no path evaluated", **common)
    if not with_outcome:
        clearable = [item for item in kinds.untested if item.path_id in here and item.clearable]
        if clearable:
            return Decision(PAUSE, "untested_round", "no path of the round has a scientific outcome; a person or "
                                                     "the machine can clear what they need",
                            needs=tuple({**dict(item.need), "path_id": item.path_id} for item in clearable), **common)
        if held:
            return decided(CONCLUDE, "conclude_not_established",
                           "no path of the round has a scientific outcome and nothing a person or the machine can "
                           f"clear; the run holds {len(held)} recorded verdict(s) under a FROZEN specification "
                           f"({', '.join(held[:5])}), so it concludes on what was measured and what is missing. "
                           "That conclusion supports no claim")
        return Decision(STOP, "no_tested_path", "no path of the round has a scientific outcome, nothing a person "
                                                "or the machine can clear, and the run holds no recorded verdict",
                        **common)
    rewind_on = set(rules["rewind_on"])
    if all(cls in rewind_on for _path, cls in with_outcome):
        if record["rounds_left"] <= 0:
            return decided(CONCLUDE, "conclude", "every tested path ended in a class that goes back to the "
                                                 "literature, and no re-planning round is left")
        if not record["gaps_unexhausted_opened"]:
            return decided(CONCLUDE, "conclude", "every tested path ended in a class that goes back to the "
                                                 "literature, and every gap the round opened is exhausted")
        return decided(REWIND, "rewind", "every tested path ended in a class that goes back to the literature; "
                                         f"{record['rounds_left']} re-planning round(s) left; unexhausted gap(s) "
                                         + ", ".join(record["gaps_unexhausted_opened"][:5]))
    return decided(CONCLUDE, "conclude", "a tested path ended in an outcome that is not a reason to go back to the "
                                         "literature: " + ", ".join(f"{path} ({cls or 'no failure'})"
                                                                    for path, cls in with_outcome
                                                                    if cls not in rewind_on)[:300])


def _listed(path: PathFacts) -> bool:
    """A path the lists are about: one that was selected, that the evaluation judged, or that carries a class."""
    return path.selection_seq is not None or path.verdict is not None or path.failure_class is not None


def _of_the_round(path: PathFacts, run: RunFacts) -> bool:
    """Its fate belongs to the round that closes: created, selected or judged since that round began."""
    since = run.round_started_event_id
    return (path.round == run.round_index or (path.selection_seq or 0) > since or (path.verdict_seq or 0) > since)


def _outcomes(snapshot: Snapshot, needs: Mapping[str, Mapping[str, Any]] | None = None,
              cleared: Collection[str] = ()) -> tuple[PathOutcome, ...]:
    """The listed paths of the run as the rules see them. A verdict that names no standing is refused."""
    unsaid = sorted(path.path_id for path in snapshot.paths if path.verdict is not None and path.standing is None)
    if unsaid:
        raise StandingMissing(
            f"path(s) {unsaid[:10]} carry a path-level verdict whose record does not say where the path stands "
            f"(standing, one of {list(STANDINGS)}); the round is not closed on a guess")
    out = []
    for path in snapshot.paths:
        if not _listed(path):
            continue
        if path.standing == STANDING_OUTCOME:
            cls = path.verdict_class            # None: it passed, whatever an earlier chain left on record
        elif path.standing == STANDING_INCONCLUSIVE:
            cls = None                          # the third kind carries no class
        else:
            cls = path.verdict_class or path.failure_class
        out.append(PathOutcome(
            path.path_id, path.status, cls, path.verdict_sha256 or path.eval_spec_sha256, standing=path.standing,
            need=(needs or {}).get(path.path_id), eval_ids=tuple(path.eval_ids), reason=path.verdict_reason,
            current=_of_the_round(path, snapshot.run), need_cleared=path.path_id in set(cleared)))
    return tuple(out)


def current_round(snapshot: Snapshot, research: Mapping[str, Any], *, rules: Mapping[str, Any] = RULES
                  ) -> RoundFacts:
    """The counters of the round now closing, and whether it is neutral.

    ``research`` is ``research.progress.run_facts(conn, run_id,
    since_event_id=snapshot.run.round_started_event_id)``: its ``searches_all_failed``
    makes the round neutral. Paths evaluated are counted from the snapshot (an adjudicating
    candidate evaluation first made in this round; an inconclusive one counts), and a path of
    the round that machinery stopped makes it neutral too.
    """
    evidence = research.get("evidence") or {}
    gaps = research.get("gaps") or {}
    tested = set(rules["tested_classes"])
    stopped = [item for item in classify([path for path in _outcomes(snapshot) if path.current], rules).untested
               if item.failure_class is not None and item.failure_class not in tested]
    return RoundFacts(index=snapshot.run.round_index, reviewed_added=int(evidence.get("reviewed_added") or 0),
                      gaps_covered=int(gaps.get("covered") or 0),
                      paths_evaluated=sum(1 for path in snapshot.paths
                                          if path.evaluated_round == snapshot.run.round_index),
                      searches_all_failed=bool(research.get("searches_all_failed")),
                      machinery_failed=bool(stopped))


def stop_facts(snapshot: Snapshot, research: Mapping[str, Any], gaps: Sequence[Mapping[str, Any]], *,
               needs: Mapping[str, Mapping[str, Any]] | None = None, cleared: Collection[str] = ()) -> StopFacts:
    """Assemble what :func:`evaluate` decides on, from a snapshot and the knowledge stream's two answers.

    ``gaps`` is ``research.progress.gap_facts(conn, run_id)``; a gap counts as opened in
    this round when ``research["gaps"]["opened_ids"]`` names it. ``needs`` maps a path id to
    the need its routing recorded (the ``need`` of its ``schedule.routed`` event), and
    ``cleared`` names the paths whose need's predicate already holds now: a round does not
    pause for what is already clear. A specification is voided when a ``schedule.routed``
    event of the run, in any round, routed an evaluator bug on it (its ``fingerprint`` is the
    specification's sha256).
    """
    opened = set((research.get("gaps") or {}).get("opened_ids") or ())
    closed = [item for item in snapshot.rounds if item.index < snapshot.run.round_index]
    voided = sorted({item.fingerprint for item in snapshot.routed
                     if item.failure_class in RULES["void_spec_on"] and item.fingerprint})
    return StopFacts(
        round_index=snapshot.run.round_index, paths=_outcomes(snapshot, needs, cleared),
        rounds=tuple(closed) + (current_round(snapshot, research),),
        gaps=tuple(GapState(str(gap["gap_id"]), str(gap.get("status")), bool(gap.get("exhausted")),
                            str(gap["gap_id"]) in opened) for gap in gaps),
        limits=snapshot.limits, budget=snapshot.budget, voided_specs=tuple(voided))


__all__ = [
    "COMPLETE", "CONCLUDE", "CONTINUE", "DECISIONS", "Decision", "EVENT_CHECK", "EVENT_ROUND", "GapState",
    "Inconclusive",
    "Kinds", "NEED_KINDS", "PAUSE", "PathOutcome", "REWIND", "RULES", "STOP", "StandingMissing", "StopFacts",
    "Untested", "check", "classify", "current_round", "evaluate", "stop_facts",
]
