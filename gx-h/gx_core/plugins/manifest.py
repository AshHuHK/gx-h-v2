"""The plugin author's immutable package declaration and host compatibility rules."""
from __future__ import annotations

import json
import keyword
import operator
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from ..schema import validate


GX_VERSION = "3.0.0"
API_VERSION = "1.0"
_SCHEMA = json.loads(Path(__file__).with_name("manifest.schema.json").read_text(encoding="utf-8"))
_COMPAT_TERM = re.compile(r"(>=|<=|==|!=|>|<|=)?\s*((?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){0,2})")
_COMPARE = {"<": operator.lt, "<=": operator.le, "==": operator.eq, "=": operator.eq,
            "!=": operator.ne, ">=": operator.ge, ">": operator.gt}


@dataclass(frozen=True)
class Dependency:
    id: str
    version: str
    optional: bool = False


@dataclass(frozen=True)
class Extension:
    id: str
    contract: str
    version: str


@dataclass(frozen=True)
class Manifest:
    id: str
    version: str
    api_version: str
    kind: str
    entrypoint: str
    gx_compat: str
    dependencies: tuple[Dependency, ...]
    extensions: tuple[Extension, ...]
    description: str | None
    author: str | None
    license: str | None
    labels: Mapping[str, str]


def _unique_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError(f"duplicate JSON field {key!r}")
        fields[key] = value
    return fields


def check_identity(plugin_id: str, version: str) -> None:
    for key, value in (("id", plugin_id), ("version", version)):
        pattern = _SCHEMA["properties"][key]["pattern"]
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError(f"invalid plugin {key} {value!r}")
    # SemVer forbids leading zeroes in numeric prerelease identifiers.
    prerelease = version.split("+", 1)[0].partition("-")[2]
    if any(part.isdecimal() and len(part) > 1 and part.startswith("0") for part in prerelease.split(".")):
        raise ValueError(f"invalid plugin prerelease version {version!r}")


def _release(value: str) -> tuple[int, ...]:
    parts = tuple(int(component) for component in value.split("."))
    return parts + (0,) * (3 - len(parts))


def _compatible(expression: str) -> bool:
    installed = _release(GX_VERSION)
    satisfied = True
    for term in expression.split(","):
        match = _COMPAT_TERM.fullmatch(term.strip())
        if not match:
            raise ValueError(f"invalid gx_compat comparison {term!r}")
        compare, release = match.groups()
        if not _COMPARE[compare or "=="](installed, _release(release)):
            satisfied = False
    return satisfied


def parse_manifest(data: bytes) -> Manifest:
    """Parse a complete manifest and check it against this installed GX/API version."""
    value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_fields)
    errors = validate(value, _SCHEMA)
    if errors:
        raise ValueError("invalid plugin manifest: " + ", ".join(errors))
    check_identity(value["id"], value["version"])
    entrypoint = value["entrypoint"]
    if (not re.fullmatch(_SCHEMA["properties"]["entrypoint"]["pattern"], entrypoint)
            or any(keyword.iskeyword(part) for part in re.split(r"[:.]", entrypoint))):
        raise ValueError(f"invalid plugin entrypoint {entrypoint!r}")
    if value["api_version"] != API_VERSION:
        raise ValueError(f"incompatible API {value['api_version']!r}; installed API is {API_VERSION}")
    if not _compatible(value["gx_compat"]):
        raise ValueError(f"gx_compat {value['gx_compat']!r} excludes installed GX {GX_VERSION}")
    dependencies = tuple(Dependency(**item) for item in value["dependencies"])
    for dep in dependencies:
        check_identity(dep.id, dep.version)
    if len({(dep.id, dep.version) for dep in dependencies}) != len(dependencies):
        raise ValueError("duplicate plugin dependency")
    extensions = tuple(Extension(**item) for item in value["extensions"])
    if any(not ext.id.strip() or not ext.version.strip() for ext in extensions):
        raise ValueError("extension id and contract version must have values")
    if len({ext.id for ext in extensions}) != len(extensions):
        raise ValueError("duplicate plugin extension id")
    labels = value.get("labels", {})
    if any(not isinstance(label, str) for label in labels.values()):
        raise ValueError("plugin labels must be strings")
    return Manifest(value["id"], value["version"], API_VERSION, value["kind"], entrypoint,
                    value["gx_compat"], dependencies, extensions, value.get("description"),
                    value.get("author"), value.get("license"), MappingProxyType(labels))
