"""Where a service publishes what it provides to the rest of the backend.

Four tables, one pattern. A service module decorates a function; the decorator refuses a
name the frozen contract does not define, refuses a second owner for the same name, and
records which module published it. The dispatcher, the submission path, the promotion gate
and the brief compiler then look the function up here instead of importing the service, so
a dependency that would point "backwards" (the task service needing the evidence validator,
the gate needing the skill checks) never becomes an import.

    @tool("gx_memory_search")
    def memory_search(ctx, args): ...                    -> ToolResult | dict

    @bundle_validator("evidence_bundle")
    def check_evidence(ctx, task, bundle, *, commit): ...  -> BundleVerdict

    @check("locator_resolution")
    def locator_resolution(ctx, subject): ...            -> CheckOutcome

    @brief_section("evidence", kinds=("propose_paths",), order=30)
    def evidence_section(ctx, task): ...                 -> BriefSection | None

Only installed handlers are exposed. The frozen contract also contains vocabulary
for optional providers. Reports list these names separately and identify import failures.
"""

from __future__ import annotations

import importlib
import traceback
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Mapping

from . import contracts
from .context import Ctx

# --------------------------------------------------------------------------- results


@dataclass
class ToolResult:
    """What a tool handler returns, before the envelope and its limits are applied."""

    data: dict[str, Any] = field(default_factory=dict)
    artifact_refs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    run_id: str | None = None
    revision: int | None = None
    status: str | None = None

    @classmethod
    def coerce(cls, value: Any) -> "ToolResult":
        if isinstance(value, ToolResult):
            return value
        if isinstance(value, dict):
            return cls(data=value)
        raise TypeError(f"a tool handler must return ToolResult or dict, got {type(value).__name__}")


@dataclass
class RecordVerdict:
    """The backend's judgement of one record of a bundle.

    ``index`` is the record's position in the bundle's list, or -1 for a judgement of the
    bundle as a whole. A refusal carries a code from ``contracts/errors.json``. ``ref`` is
    the id of the row the record became, or would become.
    """

    index: int
    ok: bool
    code: str | None = None
    message: str = ""
    ref: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.ok and self.code is None:
            raise ValueError("a refused record names the error code it was refused with")
        if self.code is not None and self.code not in contracts.error_codes():
            raise ValueError(f"{self.code!r} is not a code in contracts/errors.json")


@dataclass
class BundleVerdict:
    """A bundle judged record by record. Good records are kept when others are refused."""

    bundle_kind: str
    records: list[RecordVerdict]
    #: Whether the bundle meets the acceptance rule of the task it answers.
    accepted: bool
    #: What ``commit=True`` wrote, by kind of row: ``{"evidence": ["ev_…"], …}``.
    created: dict[str, list[str]] = field(default_factory=dict)
    summary: str = ""

    @property
    def accepted_count(self) -> int:
        return sum(1 for record in self.records if record.ok)

    @property
    def rejected_count(self) -> int:
        return sum(1 for record in self.records if not record.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bundle_kind": self.bundle_kind,
            "would_accept": self.accepted,
            "accepted_count": self.accepted_count,
            "rejected_count": self.rejected_count,
            "records": [asdict(record) for record in self.records],
            "created": self.created,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class Subject:
    """What a check, a review or an approval is about: one thing at one content sha256."""

    kind: str
    id: str
    sha256: str
    version: str | None = None


@dataclass
class CheckOutcome:
    """What a check found when the backend ran it. Never what somebody said it would find."""

    result: str
    detail: dict[str, Any] = field(default_factory=dict)
    #: Items needing a judgement, each ``{"fingerprint": str, ...}``, when result is DIAGNOSTIC.
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    artifact_ref: str | None = None
    job_id: str | None = None

    def __post_init__(self) -> None:
        allowed = contracts.gates()["check_result"]["values"]
        if self.result not in allowed:
            raise ValueError(f"check result {self.result!r} is not one of {allowed}")
        if self.result == "DIAGNOSTIC" and not all(item.get("fingerprint") for item in self.diagnostics):
            raise ValueError("every diagnostic carries a fingerprint, so it can be adjudicated")


@dataclass
class BriefSection:
    """One part of a compiled brief. ``refs`` lists what the body was built from."""

    title: str
    body: str
    refs: list[str] = field(default_factory=list)


ToolHandler = Callable[[Ctx, dict[str, Any]], "ToolResult | dict[str, Any]"]
#: ``fn(ctx, task, bundle, *, commit)``. ``task`` is the task row as a mapping. With
#: ``commit=False`` the validator writes nothing (this is ``gx_bundle_check``). With
#: ``commit=True`` it runs inside the submission's transaction: it persists the accepted
#: records and must not begin, commit or roll back a transaction of its own.
BundleValidator = Callable[..., BundleVerdict]
Check = Callable[[Ctx, Subject], CheckOutcome]
#: ``fn(ctx, task) -> BriefSection | None``. None means the section does not apply.
BriefSectionFn = Callable[[Ctx, Mapping[str, Any]], "BriefSection | None"]


@dataclass(frozen=True)
class _SectionEntry:
    name: str
    kinds: tuple[str, ...] | None
    order: int
    fn: BriefSectionFn
    module: str


# --------------------------------------------------------------------------- tables

TOOLS: dict[str, ToolHandler] = {}
BUNDLE_VALIDATORS: dict[str, BundleValidator] = {}
CHECKS: dict[str, Check] = {}
BRIEF_SECTIONS: dict[str, _SectionEntry] = {}

#: name -> module that published it, per table. What :func:`report` prints.
_OWNERS: dict[str, dict[str, str]] = {"tools": {}, "bundle_validators": {}, "checks": {}, "brief_sections": {}}


def _same_function(old: Callable[..., Any], new: Callable[..., Any]) -> bool:
    """A module imported twice re-runs its decorators; that is not a second owner."""
    return (old.__module__, old.__qualname__) == (new.__module__, new.__qualname__)


def _publish(table: dict[str, Any], kind: str, name: str, fn: Callable[..., Any], known: Any) -> None:
    if name not in known:
        raise KeyError(f"{name!r} is not a {kind[:-1].replace('_', ' ')} the frozen contract defines")
    existing = table.get(name)
    if existing is not None and not _same_function(existing, fn):
        raise ValueError(
            f"{kind[:-1].replace('_', ' ')} {name!r} is already published by {_OWNERS[kind][name]}; "
            f"{fn.__module__} cannot publish it too"
        )
    table[name] = fn
    _OWNERS[kind][name] = fn.__module__


def _all_tool_names() -> list[str]:
    return list(contracts.tool_names()) + list(contracts.privileged_tool_names())


def tool(name: str) -> Callable[[ToolHandler], ToolHandler]:
    """Publish the handler of one contract tool, worker or operator."""

    def decorate(fn: ToolHandler) -> ToolHandler:
        _publish(TOOLS, "tools", name, fn, _all_tool_names())
        return fn

    return decorate


def bundle_validator(kind: str) -> Callable[[BundleValidator], BundleValidator]:
    """Publish the validator that judges one bundle kind record by record."""

    def decorate(fn: BundleValidator) -> BundleValidator:
        _publish(BUNDLE_VALIDATORS, "bundle_validators", kind, fn, contracts.bundle_kinds())
        return fn

    return decorate


def check(check_id: str) -> Callable[[Check], Check]:
    """Publish the function the backend runs for one check of ``contracts/gates.json``."""

    def decorate(fn: Check) -> Check:
        _publish(CHECKS, "checks", check_id, fn, contracts.check_ids())
        return fn

    return decorate


def brief_section(name: str, *, kinds: tuple[str, ...] | None = None, order: int = 50) -> Callable[[BriefSectionFn], BriefSectionFn]:
    """Publish one section of the compiled brief.

    ``kinds`` limits the section to those task kinds; None means every kind. Sections are
    laid out by ``order``, then by name, so a brief compiles to the same bytes every time.
    """
    if kinds is not None:
        unknown = sorted(set(kinds) - set(contracts.label_values("task_kind")))
        if unknown:
            raise KeyError(f"brief section {name!r} names task kind(s) the contract does not define: {unknown}")

    def decorate(fn: BriefSectionFn) -> BriefSectionFn:
        existing = BRIEF_SECTIONS.get(name)
        if existing is not None and not _same_function(existing.fn, fn):
            raise ValueError(f"brief section {name!r} is already published by {existing.module}")
        BRIEF_SECTIONS[name] = _SectionEntry(name, tuple(kinds) if kinds is not None else None, order, fn, fn.__module__)
        _OWNERS["brief_sections"][name] = fn.__module__
        return fn

    return decorate


def sections_for(task_kind: str) -> list[_SectionEntry]:
    """The sections that apply to a task kind, in the order a brief lays them out."""
    entries = [entry for entry in BRIEF_SECTIONS.values() if entry.kinds is None or task_kind in entry.kinds]
    return sorted(entries, key=lambda entry: (entry.order, entry.name))


# --------------------------------------------------------------------------- providers

#: Modules that publish into the tables above when imported. One line per package; the
#: owner of a package adds its module here through the lead, and nowhere else.
PROVIDERS: tuple[str, ...] = (
    "gx_core.orchestration.tools", "gx_core.teams.tools", "gx_core.teams.manager_tools",
    "gx_core.artifacts.tools", "gx_core.governance.tools", "gx_core.knowledge.tools",
    "gx_core.jobs.tools", "gx_core.capabilities.tools", "gx_core.hostobs.projection",
)

_LOADED: dict[str, str | None] | None = None


def load_providers(*, force: bool = False) -> dict[str, str | None]:
    """Import every provider once. Returns module -> None, or the reason it did not load.

    A provider that is not there yet and one that raised while importing are both reported,
    with different text, and neither stops the others: the backend comes up with what it has
    and says what it lacks.
    """
    global _LOADED
    if _LOADED is not None and not force:
        return dict(_LOADED)
    loaded: dict[str, str | None] = {}
    for module in PROVIDERS:
        try:
            importlib.import_module(module)
        except ModuleNotFoundError as exc:
            # The provider itself, or the package it would live in, is not there.
            if exc.name == module or (exc.name and module.startswith(exc.name + ".")):
                loaded[module] = "not written yet"
            else:
                loaded[module] = f"import failed: {type(exc).__name__}: {exc}"
        except Exception as exc:  # a provider's own bug must be named, never swallowed
            last = traceback.extract_tb(exc.__traceback__)[-1]
            loaded[module] = f"import failed: {type(exc).__name__}: {exc} ({last.filename}:{last.lineno})"
        else:
            loaded[module] = None
    _LOADED = loaded
    return dict(loaded)


def compatibility_only() -> dict[str, list[str]]:
    """Contract vocabulary without a provider in this distribution."""
    load_providers()
    return {
        "tools": sorted(set(_all_tool_names()) - set(TOOLS)),
        "bundle_validators": sorted(set(contracts.bundle_kinds()) - set(BUNDLE_VALIDATORS)),
        "checks": sorted(set(contracts.check_ids()) - set(CHECKS)),
    }


def report() -> dict[str, Any]:
    """Who published what, what is missing, and which providers did not load."""
    providers = load_providers()
    return {
        "providers": providers,
        "published": {kind: dict(sorted(owners.items())) for kind, owners in _OWNERS.items()},
        "compatibility_only": compatibility_only(),
    }


def handler_for(name: str) -> ToolHandler | None:
    load_providers()
    return TOOLS.get(name)


def validator_for(kind: str) -> BundleValidator | None:
    load_providers()
    return BUNDLE_VALIDATORS.get(kind)


def check_for(check_id: str) -> Check | None:
    load_providers()
    return CHECKS.get(check_id)
