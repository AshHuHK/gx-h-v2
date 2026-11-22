"""Compile plugin-local modules from verified bytes and serve their private namespace."""
from __future__ import annotations

import ast
import importlib.abc
import importlib.machinery
import keyword
import re
import sys
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import CodeType

from ..domain.ids import sha256_of_obj
from .catalog import Package


def namespace_for(plugin_id: str, version: str, digest: str) -> str:
    suffix = sha256_of_obj([plugin_id, version, digest])
    slug = re.sub(r"[^a-zA-Z0-9_]", "_", f"{plugin_id}_{version}")
    return f"gx_ext_{slug}_{suffix}"


@dataclass(frozen=True)
class Source(importlib.abc.Loader):
    filename: str | None
    code: CodeType | None
    package: bool

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        if self.code is not None:
            exec(self.code, module.__dict__)


def _imports(tree, filename: str, package_depth: int) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                if node.level > package_depth:
                    raise ValueError(f"{filename}:{node.lineno}: relative import escapes plugin")
                continue
            if node.module == "gx_core.plugins" and all(alias.name == "author_api" for alias in node.names):
                continue
            names = [node.module]
        else:
            continue
        for name in names:
            if name != "gx_core.plugins.author_api" and name.split(".", 1)[0] not in sys.stdlib_module_names:
                raise ValueError(f"{filename}:{node.lineno}: in-process import {name!r} is outside the Author API, "
                                 "standard library and plugin-local relative modules")


def compile_package(package: Package) -> dict[str, Source]:
    """Compile all importable sources before any code in the selected closure runs."""
    namespace_package = Source(None, None, True)
    modules = {"": namespace_package}
    for relative, data in sorted(package.files.items()):
        path = PurePosixPath(relative)
        if path.suffix != ".py" or "__pycache__" in path.parts:
            continue
        is_package = path.name == "__init__.py"
        parts = path.parts[:-1] if is_package else (*path.parts[:-1], path.stem)
        if not all(part.isidentifier() and not keyword.iskeyword(part) for part in parts):
            continue
        for length in range(1, len(parts)):
            parent = ".".join(parts[:length])
            if not modules.setdefault(parent, namespace_package).package:
                raise ValueError(f"module and package both define {parent}")
        name = ".".join(parts)
        if name in modules and (modules[name] != namespace_package or not is_package):
            raise ValueError(f"module and package both define {name}")
        filename = str(package.directory / relative)
        tree = ast.parse(data, filename=filename)
        _imports(tree, filename, len(parts) + int(is_package))
        modules[name] = Source(filename, compile(tree, filename, "exec", dont_inherit=True), is_package)
    entry, _ = package.manifest.entrypoint.split(":")
    if entry not in modules:
        raise ValueError(f"entrypoint module {entry!r} is absent from the verified package")
    return modules


class VerifiedImports(importlib.abc.MetaPathFinder):
    def __init__(self):
        self.namespaces: dict[str, dict[str, Source]] = {}

    def find_spec(self, fullname, path=None, target=None):
        namespace, _, name = fullname.partition(".")
        modules = self.namespaces.get(namespace)
        if modules is None:
            return None
        if name not in modules:
            raise ModuleNotFoundError(f"no verified plugin module {fullname!r}", name=fullname)
        source = modules[name]
        # An empty submodule search path keeps normal filesystem loaders out.
        # ModuleSpec sets it for packages, including the synthetic root.
        spec = importlib.machinery.ModuleSpec(fullname, source, origin=source.filename, is_package=source.package)
        spec.has_location = source.filename is not None
        return spec
