"""Cold Run selection of exact GX plugins for one canonical workflow."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..errors import SchemaInvalid, VersionImmutable

if TYPE_CHECKING:
    from .catalog import Key, Pin
    from .loader import LoadedPlugin


@dataclass(frozen=True)
class Selection:
    plugins: tuple[LoadedPlugin, ...]

    @property
    def pins(self) -> list[dict[str, str]]:
        return [{"id": plugin.pin.id, "version": plugin.pin.version, "sha256": plugin.pin.digest,
                 "api_version": plugin.manifest.api_version} for plugin in self.plugins]


_EMPTY = Selection(())


def _key(plugin_id, version) -> Key:
    from .manifest import check_identity
    try:
        check_identity(plugin_id, version)
    except ValueError as exc:
        raise SchemaInvalid(str(exc)) from exc
    return plugin_id, version


def _references(values: list) -> tuple[Key, ...]:
    keys = []
    for value in values:
        if not isinstance(value, str) or value.count("@") != 1:
            raise SchemaInvalid(f"expected an exact plugin id@version, got {value!r}")
        keys.append(_key(*value.split("@")))
    if len(set(keys)) != len(keys):
        raise SchemaInvalid("duplicate plugin selection")
    return tuple(keys)


def _stored(values) -> dict[Key, Pin]:
    from .catalog import Pin
    from .manifest import API_VERSION

    if not isinstance(values, list):
        raise SchemaInvalid("stored plugin pins must be a list")
    pins = {}
    for row in values:
        if not isinstance(row, dict) or set(row) != {"id", "version", "sha256", "api_version"}:
            raise SchemaInvalid("stored plugin pins require id, version, sha256 and api_version")
        key = _key(row["id"], row["version"])
        if key in pins:
            raise SchemaInvalid(f"duplicate stored plugin pin for {key[0]}@{key[1]}")
        digest = row["sha256"]
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise SchemaInvalid(f"invalid stored SHA-256 for {key[0]}@{key[1]}")
        if row["api_version"] != API_VERSION:
            raise VersionImmutable(f"{key[0]}@{key[1]} requires saved Author API {row['api_version']!r}, "
                                   f"installed API is {API_VERSION}")
        pins[key] = Pin(*key, digest)
    return pins


def _validate_bindings(plugins: tuple[LoadedPlugin, ...], graph: Key, explicit: set[Key], error) -> None:
    names = {}
    for plugin in plugins:
        pin = plugin.pin
        ref = f"{pin.id}@{pin.version}"
        contributions = plugin.contributions
        workflow = contributions.workflows.get(graph)
        if (pin.id, pin.version) in explicit and contributions.workflows and workflow is None:
            raise error(f"{ref} has no workflow contribution for the selected graph {graph[0]}@{graph[1]}")
        for kind, entries in (("hook", workflow.hooks if workflow else ()), ("tool", contributions.tools)):
            for name in entries:
                prior = names.get((kind, name))
                if prior is not None:
                    raise error(f"duplicate {kind} {name!r} for {graph[0]}@{graph[1]} in {prior} and {ref}")
                names[kind, name] = ref


def _load(roots: tuple[Key, ...], graph: Key, pins: dict[Key, Pin] | None, *, restoring: bool) -> Selection:
    from .loader import bootstrap

    error = VersionImmutable if pins is not None else SchemaInvalid
    if pins is not None and not set(roots) <= pins.keys():
        raise VersionImmutable("requested plugin selection exceeds the saved Run pins")
    registry = bootstrap(selected=roots, frozen_closure=pins.values() if pins is not None else None)
    if registry.failures:
        failures = {"@".join(key): reason for key, reason in registry.failures.items()}
        raise error(f"plugins unavailable for {graph[0]}@{graph[1]}: " + "; ".join(failures.values()),
                    details={"workflow_id": graph[0], "workflow_version": graph[1], "plugins": failures})
    if pins is not None and set(registry.order) != pins.keys():
        raise VersionImmutable("requested plugin dependency closure differs from the saved Run pins")
    order = registry.order if pins is None else pins
    plugins = tuple(registry.get(*key) for key in order)
    explicit = set(roots)
    if restoring:
        # P1 stores the complete closure. A dependency may also provide other graphs.
        # Only top-level workflow providers have to target this Run's graph.
        for plugin in plugins:
            explicit.difference_update((dep.id, dep.version) for dep in plugin.manifest.dependencies)
    _validate_bindings(plugins, graph, explicit, error)
    return Selection(plugins)


def select(references, workflow_id: str, version: str, *, expected=None) -> Selection:
    """Resolve exact operator references for a new Run or match a previously saved selection."""
    if not isinstance(references, list):
        raise SchemaInvalid("plugin selection must be a list of exact id@version strings")
    if not references and (expected is None or expected == []):
        return _EMPTY
    roots = _references(references)
    pins = None if expected is None else _stored(expected)
    return _load(roots, (workflow_id, str(version)), pins, restoring=False)


def restore(pins, workflow_id: str, version: str) -> Selection:
    """Restore a Run's original order, package digests, API versions and optional dependencies."""
    if isinstance(pins, list) and not pins:
        return _EMPTY
    stored = _stored(pins)
    return _load(tuple(stored), (workflow_id, str(version)), stored, restoring=True)
