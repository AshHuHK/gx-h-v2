"""Governance: one lifecycle and one executed gate for every governed object.

v1 gates stored attestations: a check result, a prerequisite and an implementation's
evidence level were values the caller wrote, so an author alone took its own skill to
ACTIVE, and an adapter naming a binary that does not exist followed it on the same
approval. v2 turns each of those into something the backend did or observed, bound to one
content sha256:

* :mod:`~gx_core.governance.assets` — the registry (a version is its bytes) and the one
  promotion function.
* :mod:`~gx_core.governance.checks` — the check runner, the only writer of ``check_runs``.
* :mod:`~gx_core.governance.reviews` — reviews, with the independence the backend computed.
* :mod:`~gx_core.governance.approvals` — approvals bound to a subject and a sha256, granted
  by a person or by the user's standing policy applied to facts the backend checked.
* :mod:`~gx_core.governance.gate` — "is this transition's gate met for these bytes", read
  from rows; also what the evaluation builder calls for its own machine.
* :mod:`~gx_core.governance.effects` — what entering a state does, published by the stream
  that owns what it touches.
* :mod:`~gx_core.governance.pointers` — the strategy pointer a service reads.

The functions other streams call keep the signatures of the implementation plan (§3).
"""

from . import approvals, assets, checks, effects, gate, pointers, reviews, subjects  # noqa: F401

__all__ = ["approvals", "assets", "checks", "effects", "gate", "pointers", "reviews", "subjects"]
