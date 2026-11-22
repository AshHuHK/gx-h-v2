"""Reference policy helpers. Production code must integrate with GX auth and DB transactions."""
from dataclasses import dataclass
from enum import Enum


class InputKind(str, Enum):
    SUPPLEMENT = "supplement"
    EXPLICIT_CHANGE = "explicit_change"
    AMBIGUOUS_PROPOSAL = "ambiguous_proposal"


class ChangeOperation(str, Enum):
    TIGHTEN = "tighten"
    RELAX = "relax"
    REPLACE_OBJECTIVE = "replace_objective"
    MODIFY_OTHER = "modify_other"


@dataclass(frozen=True)
class ChangeFacts:
    kind: InputKind
    operation: ChangeOperation | None = None
    explicit_human_intent: bool = False
    machine_checkable: bool = False
    has_human_authorization: bool = False
    has_followup_confirmation: bool = False


@dataclass(frozen=True)
class CommitDecision:
    allowed: bool
    requires_preview: bool
    reason: str


def decide_commit(facts: ChangeFacts) -> CommitDecision:
    if facts.kind == InputKind.SUPPLEMENT:
        return CommitDecision(True, False, "Register source separately; do not bump Intent version.")
    if facts.kind == InputKind.AMBIGUOUS_PROPOSAL:
        return CommitDecision(False, False, "Ambiguous language must remain a proposal and be clarified.")
    if not facts.explicit_human_intent:
        return CommitDecision(False, False, "No explicit human request to change formal Intent.")
    if not facts.has_human_authorization:
        return CommitDecision(False, False, "GX could not verify trusted main-thread human input.")
    if facts.operation in {
        ChangeOperation.RELAX,
        ChangeOperation.REPLACE_OBJECTIVE,
    }:
        if not facts.has_followup_confirmation:
            return CommitDecision(False, True, "Relaxation/objective replacement requires a later human confirmation.")
    if facts.operation == ChangeOperation.TIGHTEN and not facts.machine_checkable:
        return CommitDecision(False, False, "A hard constraint must be machine-checkable to enforce deterministically.")
    return CommitDecision(True, False, "Eligible for impact analysis and transactional commit.")


def classify_for_display(text: str, model_proposal: dict | None = None) -> InputKind:
    """
    Do not treat this helper as an authorization or commit classifier.
    Model output is only a proposal. Human intent must be explicitly confirmed.
    """
    if model_proposal and model_proposal.get("explicit_change_candidate"):
        return InputKind.EXPLICIT_CHANGE
    if model_proposal and model_proposal.get("supplement_candidate"):
        return InputKind.SUPPLEMENT
    return InputKind.AMBIGUOUS_PROPOSAL
