"""Public registration interface for one digest-pinned GX plugin package.

The loader calls register(ctx) once and freezes its declarations. GX binds
callbacks to their Run and existing service owners when executing them.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from ..capabilities.manifest import parse_capability_version as _parse_capability_version
from ..orchestration.hooks import WorkflowHooks as _WorkflowHooks

__all__ = ["RegistrationContext", "RunContext", "Contributions", "ToolContribution", "WorkflowContribution"]


@dataclass(frozen=True, slots=True)
class TaskOperations:
    read_context: Callable
    inputs: Callable
    propose: Callable


@dataclass(frozen=True, slots=True)
class ArtifactOperations:
    read: Callable
    publish: Callable


@dataclass(frozen=True, slots=True)
class JobOperations:
    inspect_capability: Callable
    propose: Callable
    status: Callable
    wait: Callable
    cancel: Callable


@dataclass(frozen=True, slots=True)
class EvaluationOperations:
    propose: Callable


@dataclass(frozen=True, slots=True)
class IntentOperations:
    items: Callable


@dataclass(frozen=True, slots=True)
class RunContext:
    """Named operations valid for the duration of this Run's hook or Root callback."""
    run_id: str
    task_id: str | None
    attempt: int | None
    tasks: TaskOperations
    intent: IntentOperations
    artifacts: ArtifactOperations
    jobs: JobOperations
    evaluations: EvaluationOperations


@dataclass(frozen=True, slots=True)
class WorkflowContribution:
    """A reference to a versioned GX graph with its contributed hook bindings."""

    workflow_id: str
    version: str
    hooks: Mapping[str, Callable | bool]


@dataclass(frozen=True, slots=True)
class ToolContribution:
    name: str
    schema: Mapping[str, Any]
    handler: Callable
    description: str

    def as_spec(self) -> dict[str, Any]:
        """Return an independent, JSON-ready GX WorkerTools descriptor."""
        return {"type": "function", "name": self.name, "description": self.description,
                "inputSchema": _copy_schema(self.schema, freeze=False)}


@dataclass(frozen=True, slots=True)
class GoalMethod:
    describe: Callable | None = None
    validate: Callable | None = None
    bind: Callable | None = None
    project: Callable | None = None
    dependencies: Callable | None = None


@dataclass(frozen=True, slots=True)
class GoalToolContribution(ToolContribution):
    capability: str


@dataclass(frozen=True, slots=True)
class Contributions:
    workflows: Mapping[tuple[str, str], WorkflowContribution]
    tools: Mapping[str, ToolContribution]
    capabilities: tuple[str, ...]
    goal_methods: Mapping[str, GoalMethod]
    goal_tools: Mapping[str, GoalToolContribution]
    research_modules: Mapping[tuple[str, str], Mapping[str, Any]]


_Key = TypeVar("_Key")
_Entry = TypeVar("_Entry")


class _Registrar(Generic[_Key, _Entry]):
    def __init__(self):
        self._pending: dict[_Key, _Entry] | None = {}

    def _add(self, key: _Key, entry: _Entry) -> None:
        if self._pending is None:
            raise RuntimeError("plugin registration is frozen")
        if key in self._pending:
            raise ValueError(f"duplicate plugin contribution {key!r}")
        self._pending[key] = entry

    def _seal(self) -> Mapping[_Key, _Entry]:
        if self._pending is None:
            raise RuntimeError("plugin registration is frozen")
        entries = MappingProxyType(self._pending.copy())
        self._pending = None
        return entries


def _name(value: str, label: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"{label} must be a nonempty string without surrounding whitespace")


_HOOK_FIELDS = {field.name: field.default for field in fields(_WorkflowHooks)}
_MISSING = object()


class _WorkflowRegistrar(_Registrar[tuple[str, str], WorkflowContribution]):
    def register(self, *, workflow_id: str, version: str, hooks=None) -> None:
        _name(workflow_id, "workflow_id")
        _name(version, "workflow version")
        if hooks is None:
            bindings = {}
        elif isinstance(hooks, Mapping):
            bindings = dict(hooks)
        else:
            bindings = {}
            for name in _HOOK_FIELDS:
                value = getattr(hooks, name, _MISSING)
                if value is not _MISSING:
                    bindings[name] = value
            if not bindings:
                raise ValueError("expected named workflow hook bindings")
        for name, hook in bindings.items():
            if name not in _HOOK_FIELDS:
                raise ValueError(f"unknown workflow hook {name!r}")
            if isinstance(_HOOK_FIELDS[name], bool):
                if type(hook) is not bool:
                    raise TypeError(f"{name} requires a boolean")
            elif not callable(hook):
                raise TypeError(f"{name} requires a callable")
        self._add((workflow_id, version), WorkflowContribution(workflow_id, version, MappingProxyType(bindings)))


def _copy_schema(value, *, freeze: bool):
    """Make immutable registration data or JSON containers for the Root descriptor."""
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("tool schema object keys must be strings")
        result = {key: _copy_schema(item, freeze=freeze) for key, item in value.items()}
        return MappingProxyType(result) if freeze else result
    if isinstance(value, (list, tuple)):
        items = (_copy_schema(item, freeze=freeze) for item in value)
        return tuple(items) if freeze else list(items)
    if value is None or type(value) in (str, int, bool) or (type(value) is float and math.isfinite(value)):
        return value
    raise ValueError("tool schema must contain JSON values and finite numbers")


class _ToolRegistrar(_Registrar[str, ToolContribution]):
    def register(self, *, name: str, schema: Mapping[str, Any], handler: Callable, description: str = "") -> None:
        """Register handler(ctx, arguments) with the input schema GX advertises to the Root."""
        _name(name, "tool name")
        if not isinstance(schema, Mapping) or schema.get("type") != "object":
            raise ValueError(f"tool {name!r} needs an object input schema")
        if not callable(handler) or not isinstance(description, str):
            raise TypeError(f"tool {name!r} needs a callable handler and a textual description")
        self._add(name, ToolContribution(name, _copy_schema(schema, freeze=True), handler, description))


class _CapabilityRegistrar(_Registrar[str, str]):
    def add_manifest(self, manifest_bundle_ref: str) -> None:
        """Attach an existing capability@version or a package-relative JSON bundle."""
        ref = manifest_bundle_ref
        _name(ref, "capability manifest reference")
        if "@" in ref and "/" not in ref:
            _parse_capability_version(ref)
        else:
            path = PurePosixPath(ref)
            if (not ref.endswith(".json") or path.is_absolute() or ".." in path.parts
                    or path.as_posix() != ref or any(char in ref for char in ("\\", ":", "\0"))):
                raise ValueError(f"expected a canonical package-relative capability manifest path, got {ref!r}")
        self._add(ref, ref)


class _GoalMethods(_Registrar[str, GoalMethod]):
    def register(self, capability, *, describe=None, validate=None, bind=None, project=None, dependencies=None):
        _parse_capability_version(capability)
        method = GoalMethod(describe, validate, bind, project, dependencies)
        if any(hook is not None and not callable(hook) for hook in
               (describe, validate, bind, project, dependencies)):
            raise TypeError("Goal method callbacks must be callable")
        self._add(capability, method)


class _GoalTools(_Registrar[str, GoalToolContribution]):
    def register(self, *, name, capability, schema, handler, description=""):
        _parse_capability_version(capability)
        _name(name, "Goal tool name")
        if not callable(handler) or not isinstance(description, str) or not isinstance(schema, Mapping) or schema.get("type") != "object":
            raise ValueError("A Goal tool needs a handler, description and object input schema")
        self._add(name, GoalToolContribution(name, _copy_schema(schema, freeze=True), handler, description, capability))


class _ResearchModules(_Registrar):
    """Declare research roles independently of package identity and method implementations."""

    def register(self, *, module_id, version, purpose, output, cost_class, focuses, instructions,
                 capabilities, tools, input_roles, efforts=("medium", "deep"), catalog_access="assigned",
                 published_inputs_only=False):
        if not isinstance(module_id, str) or not re.fullmatch(r"[a-z][a-z0-9_.-]*", module_id):
            raise ValueError("research module_id must be a stable identifier")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9][0-9A-Za-z.-]*", version):
            raise ValueError("research module version must be an explicit version")
        for name, value in (("purpose", purpose), ("output", output), ("cost_class", cost_class),
                            ("instructions", instructions)):
            _name(value, name)
        groups = {"focuses": focuses, "capabilities": capabilities, "tools": tools,
                  "input_roles": input_roles, "efforts": efforts}
        for name, values in groups.items():
            if not isinstance(values, (list, tuple)):
                raise ValueError(f"{name} must be a list")
            for value in values:
                _name(value, name)
            if len(set(values)) != len(values):
                raise ValueError(f"{name} contains duplicate values")
        if not focuses or not efforts or set(efforts) - {"medium", "deep"}:
            raise ValueError("a research module needs a focus and at least one supported effort")
        if "medium" in efforts and set(focuses) - {"data", "model", "workflow", "evaluation"}:
            raise ValueError("medium supports data, model, workflow and evaluation inventory")
        for capability in capabilities:
            _parse_capability_version(capability)
        if catalog_access not in ("assigned", "public_methods"):
            raise ValueError("catalog_access must be assigned or public_methods")
        if type(published_inputs_only) is not bool:
            raise ValueError("published_inputs_only must be a boolean")
        # Omit the default so declarations already pinned by A keep their original identity.
        discovery = {"catalog_access": catalog_access} if catalog_access != "assigned" else {}
        if published_inputs_only:
            discovery["published_inputs_only"] = True
        self._add((module_id, version), _copy_schema({
            "module_id": module_id, "version": version, "purpose": purpose, "output": output,
            "cost_class": cost_class, "instructions": instructions, **groups, **discovery,
        }, freeze=True))


class RegistrationContext:
    """The declarations for one call to a package entrypoint."""

    __slots__ = ("workflows", "tools", "capabilities", "goal_methods", "goal_tools", "research_modules", "_snapshot", "_files")

    def __init__(self, files=None):
        self._files = files if files is not None else {}
        self.workflows = _WorkflowRegistrar()
        self.tools = _ToolRegistrar()
        self.capabilities = _CapabilityRegistrar()
        self.goal_methods = _GoalMethods()
        self.goal_tools = _GoalTools()
        self.research_modules = _ResearchModules()
        self._snapshot: Contributions | None = None

    def read_file(self, path: str) -> bytes:
        """Read an immutable member of the package verified for this registration."""
        return self._files[path]

    def freeze(self) -> Contributions:
        if self._snapshot is None:
            self._snapshot = Contributions(
                workflows=self.workflows._seal(),
                tools=self.tools._seal(),
                capabilities=tuple(self.capabilities._seal()),
                goal_methods=self.goal_methods._seal(),
                goal_tools=self.goal_tools._seal(),
                research_modules=self.research_modules._seal(),
            )
        return self._snapshot
