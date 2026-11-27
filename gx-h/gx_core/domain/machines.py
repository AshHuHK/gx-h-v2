"""State machine enforcement driven by ``contracts/states.json``.

The backend, not a prompt, decides whether a transition is legal. An agent that says
"done" moves a task to SUBMITTED; only artifact validation moves it to ACCEPTED.
"""

from __future__ import annotations

from typing import Any

from .. import contracts
from ..errors import IllegalTransition


def machine(name: str) -> dict[str, Any]:
    machines = contracts.states()["machines"]
    if name not in machines:
        raise KeyError(f"unknown machine {name!r}; known: {sorted(machines)}")
    return machines[name]


def names() -> list[str]:
    return sorted(contracts.states()["machines"])


def all_states(name: str) -> list[str]:
    return list(machine(name)["states"])


def initial_state(name: str) -> str:
    return machine(name)["initial"]


def terminal_states(name: str) -> set[str]:
    return set(machine(name)["terminal"])


def waiting_states(name: str) -> set[str]:
    return set(machine(name).get("waiting", []))


def outcome_states(name: str) -> set[str]:
    """States that end an execution attempt without being dead ends.

    Distinct from :func:`terminal_states`, which means strictly no outgoing transition.
    A blocked path is an outcome, not a terminal: it resumes when its blocker clears.
    """
    return set(machine(name).get("outcome", []))


def is_outcome(name: str, state: str) -> bool:
    return state in outcome_states(name) or state in terminal_states(name)


def is_terminal(name: str, state: str) -> bool:
    return state in terminal_states(name)


def legal_targets(name: str, state: str) -> list[str]:
    spec = machine(name)
    if state not in spec["states"]:
        raise ValueError(f"{state!r} is not a state of {name!r}")
    return list(spec["transitions"].get(state, []))


def can_transition(name: str, from_state: str, to_state: str) -> bool:
    return to_state in legal_targets(name, from_state)


def check_transition(name: str, from_state: str, to_state: str, *, subject_id: str | None = None) -> None:
    """Raise :class:`IllegalTransition` unless the move is declared in the contract."""
    spec = machine(name)
    if from_state not in spec["states"]:
        raise IllegalTransition(
            f"{name}: {from_state!r} is not a declared state",
            details={"machine": name, "from": from_state, "subject_id": subject_id},
        )
    if to_state not in spec["states"]:
        raise IllegalTransition(
            f"{name}: {to_state!r} is not a declared state",
            details={"machine": name, "to": to_state, "subject_id": subject_id},
        )
    if to_state not in spec["transitions"].get(from_state, []):
        raise IllegalTransition(
            f"{name}: {from_state} -> {to_state} is not a legal transition",
            details={
                "machine": name,
                "from": from_state,
                "to": to_state,
                "legal": spec["transitions"].get(from_state, []),
                "subject_id": subject_id,
            },
        )


def rule(name: str, key: str) -> str | None:
    """A declared invariant, so a message to a human can quote the contract verbatim."""
    return machine(name).get("rules", {}).get(key)


def unreachable_states(name: str) -> list[str]:
    """States no transition leads to, excluding the initial one. A contract smell test."""
    spec = machine(name)
    reachable = {spec["initial"]}
    for targets in spec["transitions"].values():
        reachable.update(targets)
    return sorted(set(spec["states"]) - reachable)


def dangling_targets(name: str) -> list[str]:
    """Transition targets that are not declared states. Must always be empty."""
    spec = machine(name)
    declared = set(spec["states"])
    bad: set[str] = set()
    for targets in spec["transitions"].values():
        bad.update(t for t in targets if t not in declared)
    return sorted(bad)
