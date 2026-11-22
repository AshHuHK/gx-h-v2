"""Public registration interface for one digest-pinned GX plugin package.

The loader calls register(ctx) once and freezes its declarations. GX binds
callbacks to their Run and existing service owners when executing them.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, fields
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Any, Generic, TypeVar

from ..capabilities.manifest import parse_capability_version as _parse_capability_version
from ..orchestration.hooks import WorkflowHooks as _WorkflowHooks

__all__ = ["RegistrationContext", "Contributions", "ToolContribution", "WorkflowContribution"]


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
class Contributions:
    workflows: Mapping[tuple[str, str], WorkflowContribution]
    tools: Mapping[str, ToolContribution]
    capabilities: tuple[str, ...]


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


class RegistrationContext:
    """The declarations for one call to a package entrypoint."""

    __slots__ = ("workflows", "tools", "capabilities", "_snapshot")

    def __init__(self):
        self.workflows = _WorkflowRegistrar()
        self.tools = _ToolRegistrar()
        self.capabilities = _CapabilityRegistrar()
        self._snapshot: Contributions | None = None

    def freeze(self) -> Contributions:
        if self._snapshot is None:
            self._snapshot = Contributions(
                workflows=self.workflows._seal(),
                tools=self.tools._seal(),
                capabilities=tuple(self.capabilities._seal()),
            )
        return self._snapshot
