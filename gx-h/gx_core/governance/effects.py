"""What a promotion does besides changing a status.

``contracts/gates.json`` lists, for some transitions, the *effects* that entering the state
has: a skill is indexed and projected to the hosts, a capability enters the catalogue, a
workflow version is registered, a strategy pointer is flipped. v1 had none of this, so an
asset could become ACTIVE and nothing anywhere read it.

The stream that owns what an effect touches publishes it here, from its ``tools.py``::

    from gx_core.governance.effects import effect, on_transition, projector

    @effect("enter_capability_catalogue")
    def enter(ctx, asset) -> dict: ...

    @on_transition("TOOL_ADAPTER")
    def mirror(ctx, asset, prev_state, new_state) -> None: ...

    @projector("ROLE_INTENT")
    def role_files(ctx, asset) -> list[dict]: ...
        # each {"host", "path", "content": bytes, "embeds": ["/json/pointer", ...]}
        # or   {"host", "skipped": "<why this host gets no file>"}

    @review_rule("DECISION_SKILL")
    def what_a_skill_review_carries(ctx, asset, bundle) -> list[dict]: ...

    @on_review("DECISION_SKILL")
    def after_a_skill_review(ctx, asset, review, task) -> dict | None: ...

    @review_package("DECISION_SKILL")
    def what_a_skill_reviewer_judges(conn, asset) -> dict: ...
        # {"items": [{"item_id", ...}], "instructions", ..., "acceptance"?: {...}}

A projected file carries the asset it was generated from — ``gx_asset_id``,
``gx_asset_version`` and ``gx_asset_sha256`` as ``key: value`` or ``key = "value"`` — and
names, in ``embeds``, the parts of the canonical content it contains verbatim. That is all
the ``projection_parity`` check reads. The effect ``project_to_hosts`` (the host stream's)
calls the projector, writes the files and returns ``{"written": [{"host", "path",
"abs_path", "sha256"}], "skipped": [...]}``.

Effects and transition listeners run inside the promotion's transaction, after the status
has changed, so an effect that raises undoes the promotion. An effect a transition requires and nobody has
published is ``IMPLEMENTATION_GAP``: the asset stays where it was rather than entering a
state that would have no consumer.

An external local effect must also enlist compensation with persistence.store.on_transaction_end
and provide durable recovery. SQL rollback alone does not undo filesystem writes. The host
projection effect owns its full preflight, undo journal and store-open recovery; completion
callbacks follow the outermost transaction even when promotion ran under a savepoint.

``asset`` is the asset row as a dict with ``manifest`` parsed and ``status`` already set to
the new state.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping

from .. import contracts, registry
from ..context import Ctx
from ..errors import ImplementationGap

EffectFn = Callable[[Ctx, Mapping[str, Any]], "dict[str, Any] | None"]
TransitionFn = Callable[[Ctx, Mapping[str, Any], str, str], None]
ProjectorFn = Callable[[Ctx, Mapping[str, Any]], "list[dict[str, Any]]"]
ReviewRuleFn = Callable[[Ctx, Mapping[str, Any], Mapping[str, Any]], "list[dict[str, Any]]"]
ReviewedFn = Callable[[Ctx, Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]], "dict[str, Any] | None"]
PackageFn = Callable[[Any, Mapping[str, Any]], "dict[str, Any]"]

#: (effect name, asset kind or None for every kind) -> function.
EFFECTS: dict[tuple[str, str | None], EffectFn] = {}
#: asset kind -> listeners called after every transition of an asset of that kind.
LISTENERS: dict[str, list[TransitionFn]] = {}
#: asset kind -> the function that says what the host projections of an asset are.
PROJECTORS: dict[str, ProjectorFn] = {}
#: asset kind -> what a review of that kind must carry beyond the general rules.
REVIEW_RULES: dict[str, ReviewRuleFn] = {}
#: asset kind -> listeners called after a review of an asset of that kind was recorded.
REVIEW_LISTENERS: dict[str, list[ReviewedFn]] = {}
#: asset kind -> what a reviewer of an asset of that kind is given to judge.
REVIEW_PACKAGES: dict[str, PackageFn] = {}

_OWNERS: dict[str, str] = {}


def known_effects() -> set[str]:
    """Every effect name ``contracts/gates.json`` uses."""
    names: set[str] = set()
    for table in contracts.gates()["requirements"].values():
        for entry in table.values():
            names.update(entry.get("effects", []))
    return names


def _same(old: Callable[..., Any], new: Callable[..., Any]) -> bool:
    return (old.__module__, old.__qualname__) == (new.__module__, new.__qualname__)


def _kinds(kinds: tuple[str, ...] | None) -> tuple[str | None, ...]:
    if kinds is None:
        return (None,)
    for kind in kinds:
        contracts.require_label("asset_kind", kind)
    return tuple(kinds)


def effect(name: str, *, kinds: tuple[str, ...] | None = None) -> Callable[[EffectFn], EffectFn]:
    """Publish what one effect of the gate table does, for the given asset kinds (or all)."""
    if name not in known_effects():
        raise KeyError(f"{name!r} is not an effect contracts/gates.json names; known: {sorted(known_effects())}")

    def decorate(fn: EffectFn) -> EffectFn:
        for kind in _kinds(kinds):
            key = (name, kind)
            existing = EFFECTS.get(key)
            if existing is not None and not _same(existing, fn):
                raise ValueError(
                    f"effect {name!r} for kind {kind or 'any'} is already published by "
                    f"{existing.__module__}; {fn.__module__} cannot publish it too"
                )
            EFFECTS[key] = fn
            _OWNERS[f"effect:{name}:{kind or '*'}"] = fn.__module__
        return fn

    return decorate


def on_transition(kind: str) -> Callable[[TransitionFn], TransitionFn]:
    """Be told, inside the transaction, whenever an asset of ``kind`` changes state."""
    contracts.require_label("asset_kind", kind)

    def decorate(fn: TransitionFn) -> TransitionFn:
        listeners = LISTENERS.setdefault(kind, [])
        if not any(_same(existing, fn) for existing in listeners):
            listeners.append(fn)
            _OWNERS[f"on_transition:{kind}:{fn.__module__}.{fn.__qualname__}"] = fn.__module__
        return fn

    return decorate


def projector(kind: str) -> Callable[[ProjectorFn], ProjectorFn]:
    """Publish the function that lists the host projections of an asset of ``kind``."""
    contracts.require_label("asset_kind", kind)

    def decorate(fn: ProjectorFn) -> ProjectorFn:
        existing = PROJECTORS.get(kind)
        if existing is not None and not _same(existing, fn):
            raise ValueError(f"projector for {kind} is already published by {existing.__module__}")
        PROJECTORS[kind] = fn
        _OWNERS[f"projector:{kind}"] = fn.__module__
        return fn

    return decorate


def review_rule(kind: str) -> Callable[[ReviewRuleFn], ReviewRuleFn]:
    """Publish what a review of an asset of ``kind`` must carry beyond the general rules.

    ``fn(ctx, asset, bundle) -> [{"field", "index", "message"}]``: each entry refuses the
    review bundle (index -1 for the bundle as a whole, otherwise a position in the list
    named by ``field``). It runs before anything is written, on a dry run too.
    """
    contracts.require_label("asset_kind", kind)

    def decorate(fn: ReviewRuleFn) -> ReviewRuleFn:
        existing = REVIEW_RULES.get(kind)
        if existing is not None and not _same(existing, fn):
            raise ValueError(f"review rule for {kind} is already published by {existing.__module__}")
        REVIEW_RULES[kind] = fn
        _OWNERS[f"review_rule:{kind}"] = fn.__module__
        return fn

    return decorate


def on_review(kind: str) -> Callable[[ReviewedFn], ReviewedFn]:
    """Be told, inside the submission's transaction, when a review of an asset of ``kind`` was recorded.

    ``fn(ctx, asset, review, task) -> dict | None``. ``review`` is what ``record_review``
    returned. A returned ``{"tasks": [...]}`` is reported in the verdict's ``created``.
    """
    contracts.require_label("asset_kind", kind)

    def decorate(fn: ReviewedFn) -> ReviewedFn:
        listeners = REVIEW_LISTENERS.setdefault(kind, [])
        if not any(_same(existing, fn) for existing in listeners):
            listeners.append(fn)
            _OWNERS[f"on_review:{kind}:{fn.__module__}.{fn.__qualname__}"] = fn.__module__
        return fn

    return decorate


def review_package(kind: str) -> Callable[[PackageFn], PackageFn]:
    """Publish what the reviewer of an asset of ``kind`` is given to judge.

    ``fn(conn, asset) -> {"items": [{"item_id", ...}], "instructions": str, ...}``: every item
    gets a verdict in the review bundle (``row_verdicts``), so an item is a question the
    reviewer must answer, not a description. A top-level ``acceptance`` mapping, if present,
    is added to the review task's acceptance. Read-only: it is called when a review is
    requested, inside the requester's transaction. The stream that owns the kind publishes it;
    a kind with none gets :func:`gx_core.governance.reviews.default_package`.
    """
    contracts.require_label("asset_kind", kind)

    def decorate(fn: PackageFn) -> PackageFn:
        existing = REVIEW_PACKAGES.get(kind)
        if existing is not None and not _same(existing, fn):
            raise ValueError(f"review package for {kind} is already published by {existing.__module__}")
        REVIEW_PACKAGES[kind] = fn
        _OWNERS[f"review_package:{kind}"] = fn.__module__
        return fn

    return decorate


def package_builder(kind: str) -> PackageFn | None:
    registry.load_providers()
    return REVIEW_PACKAGES.get(kind)


def review_problems(ctx: Ctx, asset: Mapping[str, Any], bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    registry.load_providers()
    rule = REVIEW_RULES.get(asset["kind"])
    return list(rule(ctx, asset, bundle) or []) if rule is not None else []


def notify_review(ctx: Ctx, asset: Mapping[str, Any], review: Mapping[str, Any], task: Mapping[str, Any]) -> dict[str, Any]:
    registry.load_providers()
    out: dict[str, Any] = {}
    for fn in REVIEW_LISTENERS.get(asset["kind"], []):
        done = fn(ctx, asset, review, task) or {}
        for key, value in done.items():
            if isinstance(value, list):
                out.setdefault(key, []).extend(value)
            else:
                out[key] = value
    return out


def effect_for(name: str, kind: str) -> EffectFn | None:
    registry.load_providers()
    return EFFECTS.get((name, kind)) or EFFECTS.get((name, None))


def projector_for(kind: str) -> ProjectorFn | None:
    registry.load_providers()
    return PROJECTORS.get(kind)


def missing(names: list[str], kind: str) -> list[str]:
    """The effects of a transition that nobody has published for this kind."""
    return [name for name in names if effect_for(name, kind) is None]


def run(ctx: Ctx, asset: Mapping[str, Any], names: list[str]) -> dict[str, Any]:
    """Run a transition's effects in the order the gate table lists them."""
    kind = asset["kind"]
    absent = missing(names, kind)
    if absent:
        raise ImplementationGap(
            f"entering this state has effect(s) {absent} for a {kind} and nothing publishes them; "
            "the asset stays where it is rather than entering a state nothing reads",
            details={"asset_id": asset["asset_id"], "version": asset["version"], "kind": kind,
                     "unpublished_effects": absent},
        )
    results: dict[str, Any] = {}
    for name in names:
        fn = effect_for(name, kind)
        results[name] = fn(ctx, asset) or {}
    return results


def notify(ctx: Ctx, asset: Mapping[str, Any], prev_state: str, new_state: str) -> None:
    registry.load_providers()
    for fn in LISTENERS.get(asset["kind"], []):
        fn(ctx, asset, prev_state, new_state)


def report() -> dict[str, str]:
    """Who published which effect, listener and projector."""
    registry.load_providers()
    return dict(sorted(_OWNERS.items()))
