"""Structured errors whose semantics come from ``contracts/errors.json``.

``retryable`` is advice about the *class* of failure, never permission to resend a
side-effecting request: the caller must also look at ``side_effect_status`` and at the
idempotency record. ``OUTCOME_UNKNOWN`` in particular means the external world may
already have done the work.
"""

from __future__ import annotations

from typing import Any

from . import contracts


class GxError(Exception):
    """Base class. ``code`` must exist in contracts/errors.json."""

    code: str = "SCHEMA_INVALID"

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        evidence_ref: str | None = None,
        side_effect_status: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code or self.code
        spec = contracts.error_spec(self.code)
        self.message = message
        self.retryable: bool = bool(spec["retryable"])
        self.side_effect_status: str = side_effect_status or spec["side_effect_status"]
        self.suggested_recovery: str = spec["suggested_recovery"]
        self.category: str = spec["category"]
        self.evidence_ref = evidence_ref
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        payload = {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "side_effect_status": self.side_effect_status,
            "evidence_ref": self.evidence_ref,
            "suggested_recovery": self.suggested_recovery,
        }
        if self.details:
            payload["details"] = self.details
        return payload

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.code}: {self.message}"


def _mk(name: str, code: str) -> type[GxError]:
    return type(name, (GxError,), {"code": code})


DependencyUnmet = _mk("DependencyUnmet", "DEPENDENCY_UNMET")
StaleRevision = _mk("StaleRevision", "STALE_REVISION")
LeaseExpired = _mk("LeaseExpired", "LEASE_EXPIRED")
LeaseHeld = _mk("LeaseHeld", "LEASE_HELD")
EnvNotReady = _mk("EnvNotReady", "ENV_NOT_READY")
ResourceDenied = _mk("ResourceDenied", "RESOURCE_DENIED")
OutcomeUnknown = _mk("OutcomeUnknown", "OUTCOME_UNKNOWN")
EvaluatorInvalid = _mk("EvaluatorInvalid", "EVALUATOR_INVALID")
ClientCapabilityMissing = _mk("ClientCapabilityMissing", "CLIENT_CAPABILITY_MISSING")
PermissionDenied = _mk("PermissionDenied", "PERMISSION_DENIED")
ApprovalRequired = _mk("ApprovalRequired", "APPROVAL_REQUIRED")
BudgetExceeded = _mk("BudgetExceeded", "BUDGET_EXCEEDED")
IdempotencyConflict = _mk("IdempotencyConflict", "IDEMPOTENCY_CONFLICT")
SchemaInvalid = _mk("SchemaInvalid", "SCHEMA_INVALID")
PathTraversalDenied = _mk("PathTraversalDenied", "PATH_TRAVERSAL_DENIED")
ScopeDenied = _mk("ScopeDenied", "SCOPE_DENIED")
CapabilityNotApproved = _mk("CapabilityNotApproved", "CAPABILITY_NOT_APPROVED")
ArtifactNotFound = _mk("ArtifactNotFound", "ARTIFACT_NOT_FOUND")
ArtifactHashMismatch = _mk("ArtifactHashMismatch", "ARTIFACT_HASH_MISMATCH")
ImplementationGap = _mk("ImplementationGap", "IMPLEMENTATION_GAP")
IllegalTransition = _mk("IllegalTransition", "ILLEGAL_TRANSITION")
HoldActive = _mk("HoldActive", "HOLD_ACTIVE")
SourceUnavailable = _mk("SourceUnavailable", "SOURCE_UNAVAILABLE")
ProxyUnavailable = _mk("ProxyUnavailable", "PROXY_UNAVAILABLE")
# --- contract v2 (2026-11-12): the refusals the redesign's gates produce -------------------
EvalSpecNotFrozen = _mk("EvalSpecNotFrozen", "EVAL_SPEC_NOT_FROZEN")
HeldoutLookDenied = _mk("HeldoutLookDenied", "HELDOUT_LOOK_DENIED")
ModelPolicyViolation = _mk("ModelPolicyViolation", "MODEL_POLICY_VIOLATION")
PolicyScreenBlocked = _mk("PolicyScreenBlocked", "POLICY_SCREEN_BLOCKED")
PhaseDenied = _mk("PhaseDenied", "PHASE_DENIED")
GateNotMet = _mk("GateNotMet", "GATE_NOT_MET")
IndependenceRequired = _mk("IndependenceRequired", "INDEPENDENCE_REQUIRED")
ContentHashMismatch = _mk("ContentHashMismatch", "CONTENT_HASH_MISMATCH")
VersionImmutable = _mk("VersionImmutable", "VERSION_IMMUTABLE")
DuplicateExperiment = _mk("DuplicateExperiment", "DUPLICATE_EXPERIMENT")
ConcurrentRun = _mk("ConcurrentRun", "CONCURRENT_RUN")
UpstreamError = _mk("UpstreamError", "UPSTREAM_ERROR")
LocatorNotFound = _mk("LocatorNotFound", "LOCATOR_NOT_FOUND")
GroundsWithdrawn = _mk("GroundsWithdrawn", "GROUNDS_WITHDRAWN")
ReadReceiptMissing = _mk("ReadReceiptMissing", "READ_RECEIPT_MISSING")
StoreUnavailable = _mk("StoreUnavailable", "STORE_UNAVAILABLE")
# --- contract r5 (2026-11-12): a claim made on a plan that has moved; a projection onto another's file ---
StalePlan = _mk("StalePlan", "STALE_PLAN")
ProjectionTargetOccupied = _mk("ProjectionTargetOccupied", "PROJECTION_TARGET_OCCUPIED")


BY_CODE: dict[str, type[GxError]] = {
    cls.code: cls
    for cls in (
        DependencyUnmet, StaleRevision, LeaseExpired, LeaseHeld, EnvNotReady,
        ResourceDenied, OutcomeUnknown, EvaluatorInvalid, ClientCapabilityMissing,
        PermissionDenied, ApprovalRequired, BudgetExceeded, IdempotencyConflict,
        SchemaInvalid, PathTraversalDenied, ScopeDenied, CapabilityNotApproved,
        ArtifactNotFound, ArtifactHashMismatch, ImplementationGap, IllegalTransition,
        HoldActive, SourceUnavailable, ProxyUnavailable,
        EvalSpecNotFrozen, HeldoutLookDenied, ModelPolicyViolation, PolicyScreenBlocked,
        PhaseDenied, GateNotMet, IndependenceRequired, ContentHashMismatch,
        VersionImmutable, DuplicateExperiment, ConcurrentRun, UpstreamError,
        LocatorNotFound, GroundsWithdrawn, ReadReceiptMissing, StoreUnavailable,
        StalePlan, ProjectionTargetOccupied,
    )
}


def raise_for(code: str, message: str, **kwargs: Any) -> None:
    cls = BY_CODE.get(code)
    if cls is None:
        raise GxError(message, code=code, **kwargs)
    raise cls(message, **kwargs)


def all_codes_have_classes() -> list[str]:
    """Error codes declared in the contract with no Python class. Used by a contract test."""
    return [code for code in contracts.error_codes() if code not in BY_CODE]
