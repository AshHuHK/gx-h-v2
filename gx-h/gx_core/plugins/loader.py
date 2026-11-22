"""Bootstrap a frozen selection of operator-installed plugins and their exact dependencies."""
from __future__ import annotations

import importlib
import inspect
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterable, Mapping

from .. import config
from .author_api import Contributions, RegistrationContext
from .catalog import Catalog, Key, Package, Pin
from .imports import Source, VerifiedImports, compile_package, namespace_for
from .manifest import Manifest


@dataclass(frozen=True)
class LoadedPlugin:
    pin: Pin
    manifest: Manifest
    namespace: str
    contributions: Contributions
    _files: Mapping[str, bytes]

    def read_file(self, relative_path: str) -> bytes:
        """Access the verified package snapshot, including capability bundle documents."""
        return self._files[relative_path]


@dataclass(frozen=True)
class Registry:
    plugins: Mapping[Pin, LoadedPlugin]
    order: tuple[Key, ...]
    failures: Mapping[Key, str]

    def get(self, plugin_id: str, version: str, digest: str | None = None) -> LoadedPlugin:
        if digest is not None:
            result = self.plugins.get(Pin(plugin_id, version, digest))
        else:
            result = next((plugin for pin, plugin in self.plugins.items()
                           if pin.id == plugin_id and pin.version == version), None)
        if result is None:
            key = (plugin_id, version)
            raise ValueError(self.failures.get(key, f"{plugin_id}@{version} with the requested pin is unavailable"))
        return result


_imports = VerifiedImports()
_registered: dict[Pin, LoadedPlugin] = {}
_lock = threading.RLock()


def _check_contributions(package: Package, contributions: Contributions) -> None:
    declared = {extension.contract for extension in package.manifest.extensions}
    actual = {kind for kind, entries in (("workflow", contributions.workflows), ("tool", contributions.tools),
                                         ("capability", contributions.capabilities)) if entries}
    if declared != actual:
        raise ValueError(f"declared extensions {sorted(declared)} differ from registered contracts {sorted(actual)}")
    for ref in contributions.capabilities:
        if ("@" not in ref or "/" in ref) and ref not in package.files:
            raise ValueError(f"capability bundle {ref!r} is absent from the verified package")


def _register(package: Package, sources: dict[str, Source]) -> LoadedPlugin:
    pin = package.pin
    namespace = namespace_for(pin.id, pin.version, pin.digest)
    prefix = namespace + "."
    with _lock:
        if pin in _registered:
            return _registered[pin]
        if any(name == namespace or name.startswith(prefix) for name in tuple(sys.modules)):
            raise ValueError(f"plugin import namespace {namespace} is already occupied")
        if _imports not in sys.meta_path:
            sys.meta_path.insert(0, _imports)
        _imports.namespaces[namespace] = sources
        try:
            module, name = package.manifest.entrypoint.split(":")
            entry = importlib.import_module(f"{namespace}.{module}")
            register = getattr(entry, name)
            ctx = RegistrationContext()
            result = register(ctx)
            if result is not None:
                if inspect.iscoroutine(result):
                    result.close()
                raise ValueError("register(ctx) must complete synchronously and return None")
            contributions = ctx.freeze()
            _check_contributions(package, contributions)
            loaded = LoadedPlugin(pin, package.manifest, namespace, contributions, package.files)
            _registered[pin] = loaded
            return loaded
        finally:
            if pin not in _registered:
                for fullname in tuple(sys.modules):
                    if fullname == namespace or fullname.startswith(prefix):
                        del sys.modules[fullname]
                del _imports.namespaces[namespace]
                if not _imports.namespaces:
                    sys.meta_path.remove(_imports)


def _label(key: Key) -> str:
    return "@".join(key)


class _Resolver:
    """Select and preflight a complete dependency closure before running its entrypoints."""

    def __init__(self, catalog: Catalog, expected: dict[Key, set[str]], frozen: bool):
        self.catalog = catalog
        self.expected = expected
        self.frozen = frozen
        self.verified: dict[Key, tuple[Package, dict[str, Source]]] = {}

    def closure(self, root: Key) -> list[Key]:
        order = []
        visited = set()
        path = []

        def visit(key: Key) -> None:
            if key in visited:
                return
            if key in path:
                cycle = path[path.index(key):] + [key]
                raise ValueError("dependency cycle: " + " -> ".join(map(_label, cycle)))
            path.append(key)
            if key not in self.verified:
                try:
                    package = self.catalog.verified(key)
                    pin = package.pin
                    expected = self.expected.get(key)
                    if expected and expected != {pin.digest}:
                        raise ValueError(f"expected exact digest {sorted(expected)}, catalog has {pin.digest}")
                    with _lock:
                        accepted = _registered.get(pin)
                    sources = {} if accepted else compile_package(package)
                    self.verified[key] = package, sources
                except Exception as exc:
                    chain = " -> ".join(map(_label, path))
                    raise ValueError(f"{chain}: {exc}") from exc
            package, _ = self.verified[key]
            for dep in sorted(package.manifest.dependencies, key=lambda item: (item.id, item.version)):
                dependency = (dep.id, dep.version)
                if self.frozen and dependency not in self.expected:
                    if dep.optional:
                        continue
                    chain = " -> ".join(map(_label, [*path, dependency]))
                    raise ValueError(f"{chain}: required dependency is missing from the frozen closure")
                if not dep.optional or self.catalog.has(dependency) or self.expected.get(dependency):
                    visit(dependency)
            path.pop()
            visited.add(key)
            order.append(key)

        visit(root)
        return order


def bootstrap(install_root: Path | None = None, catalog_path: Path | None = None,
              selected: Iterable[Key | Pin] = (), *, frozen_closure: Iterable[Pin] | None = None) -> Registry:
    """Load exact roots. On restoration only the stored closure can supply dependencies."""
    expected: dict[Key, set[str]] = {}
    for item in selected:
        if isinstance(item, Pin):
            expected.setdefault((item.id, item.version), set()).add(item.digest)
        else:
            plugin_id, version = item
            expected.setdefault((plugin_id, version), set())
    roots = sorted(expected)
    frozen = frozen_closure is not None
    if frozen:
        restored: dict[Key, set[str]] = {}
        for pin in frozen_closure:
            restored.setdefault((pin.id, pin.version), set()).add(pin.digest)
        for key, requested in expected.items():
            if key not in restored or not requested.issubset(restored[key]):
                raise ValueError(f"{_label(key)} is outside the frozen plugin closure")
        expected = restored
    loaded = {}
    failures = {}
    if roots:
        try:
            catalog = Catalog(install_root if install_root is not None else config.HARNESS_ROOT / "plugins" / "installed",
                              catalog_path if catalog_path is not None else config.CONFIG_DIR / "workflow_packages.json")
        except (ValueError, OSError) as exc:
            failures = {key: f"installation catalog: {exc}" for key in roots}
        else:
            resolver = _Resolver(catalog, expected, frozen)
            registration_errors = {}
            for key in roots:
                try:
                    for member in resolver.closure(key):
                        package, sources = resolver.verified[member]
                        if member in registration_errors:
                            raise ValueError(registration_errors[member])
                        if package.pin not in loaded:
                            try:
                                loaded[package.pin] = _register(package, sources)
                            except (Exception, SystemExit) as exc:
                                error = f"loading {_label(member)}: {type(exc).__name__}: {exc}"
                                registration_errors[member] = error
                                raise ValueError(error) from exc
                except (ValueError, RecursionError) as exc:
                    failures[key] = str(exc)
    return Registry(MappingProxyType(loaded), tuple((pin.id, pin.version) for pin in loaded), MappingProxyType(failures))
