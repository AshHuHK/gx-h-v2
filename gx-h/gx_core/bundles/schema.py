"""Generic validation of a bundle against ``contracts/bundles.json``.

The contract writes field types in a small notation and this module is the one place
that reads it: ``str``, ``int``, ``num``, ``bool``, ``object``, ``array``, ``array<T>``,
a named type from the contract's ``types`` table, or ``enum:<label axis>``. A trailing
``?`` marks an optional field.

Two rules shape the result.

**Unknown fields are refused, not ignored.** A bundle cannot carry a status, a verdict
or an evidence level for the backend to decide whether to believe; such a field is a
violation of the record that carries it.

**A bundle is judged record by record.** A violation inside one element of a top-level
list refuses that element and nothing else. A violation of the bundle's own shape -- a
missing or unknown top-level field, a top-level field of the wrong type -- refuses the
bundle, because there is then no agreed place to find its records.

Nothing here knows what any field means. Whether a quote occurs in its source or a job
belongs to its path is the published validator's question, asked after this one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .. import contracts


@dataclass(frozen=True)
class Violation:
    pointer: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"pointer": self.pointer, "message": self.message}


@dataclass
class SchemaReport:
    """What the shape check found, and the bundle with its refused elements removed."""

    kind: str
    #: Violations of the bundle's own shape. Any of these refuses the whole bundle.
    bundle_violations: list[Violation] = field(default_factory=list)
    #: ``field -> original index -> violations`` for refused elements of top-level lists.
    refused: dict[str, dict[int, list[Violation]]] = field(default_factory=dict)
    #: The bundle with refused elements dropped and null optionals removed.
    clean: dict[str, Any] = field(default_factory=dict)
    #: ``field -> [original index of each element kept in ``clean``]``.
    kept: dict[str, list[int]] = field(default_factory=dict)

    @property
    def bundle_ok(self) -> bool:
        return not self.bundle_violations

    @property
    def refused_count(self) -> int:
        return sum(len(items) for items in self.refused.values())

    def original_index(self, field_name: str, clean_index: int) -> int | None:
        kept = self.kept.get(field_name)
        if kept is None or not 0 <= clean_index < len(kept):
            return None
        return kept[clean_index]


# ----------------------------------------------------------------------------- notation

def split_optional(decl: str) -> tuple[str, bool]:
    text = decl.strip()
    if text.endswith("?"):
        return text[:-1].strip(), True
    return text, False


def is_list_decl(decl: str) -> bool:
    base, _ = split_optional(decl)
    return base == "array" or (base.startswith("array<") and base.endswith(">"))


def item_decl(decl: str) -> str | None:
    """The element type of ``array<T>``, or None for a bare ``array``."""
    base, _ = split_optional(decl)
    if base.startswith("array<") and base.endswith(">"):
        return base[len("array<"):-1].strip()
    return None


def list_fields(kind: str) -> list[str]:
    """The top-level list fields of a bundle kind, in the order the contract declares them."""
    fields = contracts.bundle_spec(kind)["fields"]
    return [name for name, decl in fields.items() if is_list_decl(decl)]


def primary_list(kind: str) -> str | None:
    """The list a record index refers to when a verdict does not name one.

    It is the first list field the contract declares for the kind: ``records`` of an
    evidence bundle, ``paths`` of a path bundle. A validator judging a kind with several
    lists names the list in ``RecordVerdict.details["field"]``.
    """
    lists = list_fields(kind)
    return lists[0] if lists else None


# ---------------------------------------------------------------------------- validation

def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "num"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def check_value(value: Any, decl: str, pointer: str) -> tuple[Any, list[Violation]]:
    """``(normalised value, violations)`` for one value against one declaration.

    Normalising removes optional fields given as ``null``: an emitter that writes every
    key writes ``"note": null`` for a note it does not have, and that is absence, not a
    value of the wrong type.
    """
    base, _optional = split_optional(decl)
    types = contracts.bundle_types()

    if base == "str":
        ok = isinstance(value, str)
    elif base == "int":
        ok = isinstance(value, int) and not isinstance(value, bool)
    elif base == "num":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif base == "bool":
        ok = isinstance(value, bool)
    elif base == "object":
        ok = isinstance(value, dict)
    elif base == "array":
        ok = isinstance(value, list)
    elif base.startswith("enum:"):
        axis = base[len("enum:"):].strip()
        allowed = contracts.label_values(axis)
        # ``True == 1`` in Python: without the type test a boolean would pass as layer 1.
        same_kind = [item for item in allowed if type(item) is type(value)]
        if not isinstance(value, (str, int, float, bool)) or value not in same_kind:
            return value, [Violation(pointer, f"{value!r} is not a value of {axis}: {allowed}")]
        return value, []
    elif base.startswith("array<") and base.endswith(">"):
        if not isinstance(value, list):
            return value, [Violation(pointer, f"expected a list, got {_json_type(value)}")]
        inner = item_decl(base) or "object"
        out, violations = [], []
        for index, item in enumerate(value):
            cleaned, found = check_value(item, inner, f"{pointer}[{index}]")
            out.append(cleaned)
            violations.extend(found)
        return out, violations
    elif base in types:
        return check_object(value, types[base], pointer, type_name=base)
    else:
        return value, [Violation(pointer, f"the contract declares type {base!r}, which is not defined")]

    if not ok:
        return value, [Violation(pointer, f"expected {base}, got {_json_type(value)}")]
    return value, []


def check_object(
    value: Any, fields: dict[str, str], pointer: str, *, type_name: str
) -> tuple[Any, list[Violation]]:
    """Check one object against a declared field table: required, optional, nothing else."""
    if not isinstance(value, dict):
        return value, [Violation(pointer, f"expected {type_name} (an object), got {_json_type(value)}")]
    violations: list[Violation] = []
    out: dict[str, Any] = {}
    for name in value:
        if name not in fields:
            violations.append(Violation(
                f"{pointer}.{name}",
                f"{name!r} is not a field of {type_name}; a bundle states content and the "
                "backend computes status, verdicts and levels itself",
            ))
    for name, decl in fields.items():
        _base, optional = split_optional(decl)
        if name not in value or (optional and value[name] is None):
            if not optional:
                violations.append(Violation(f"{pointer}.{name}", f"required field {name!r} is missing"))
            continue
        cleaned, found = check_value(value[name], decl, f"{pointer}.{name}")
        out[name] = cleaned
        violations.extend(found)
    return out, violations


def check_bundle(kind: str, bundle: Any) -> SchemaReport:
    """Check a whole bundle and separate what refuses a record from what refuses it all."""
    spec = contracts.bundle_spec(kind)
    fields: dict[str, str] = spec["fields"]
    report = SchemaReport(kind=kind)
    if not isinstance(bundle, dict):
        report.bundle_violations.append(
            Violation("$", f"a {kind} is a JSON object, got {_json_type(bundle)}")
        )
        return report

    max_records = int(contracts.bundle_limits()["max_records"])
    for name in bundle:
        if name not in fields:
            report.bundle_violations.append(Violation(
                f"$.{name}",
                f"{name!r} is not a field of {kind}; a bundle states content and the "
                "backend computes status, verdicts and levels itself",
            ))

    for name, decl in fields.items():
        _base, optional = split_optional(decl)
        if name not in bundle or (optional and bundle[name] is None):
            if not optional:
                report.bundle_violations.append(
                    Violation(f"$.{name}", f"required field {name!r} is missing")
                )
            continue
        value = bundle[name]
        if not is_list_decl(decl):
            cleaned, found = check_value(value, decl, f"$.{name}")
            report.bundle_violations.extend(found)
            report.clean[name] = cleaned
            continue

        if not isinstance(value, list):
            report.bundle_violations.append(
                Violation(f"$.{name}", f"expected a list, got {_json_type(value)}")
            )
            continue
        if len(value) > max_records:
            report.bundle_violations.append(Violation(
                f"$.{name}", f"{len(value)} entries exceed the limit of {max_records} per bundle"
            ))
            continue
        inner = item_decl(decl)
        kept_values: list[Any] = []
        kept_index: list[int] = []
        for index, item in enumerate(value):
            if inner is None:
                cleaned, found = item, []
            else:
                cleaned, found = check_value(item, inner, f"$.{name}[{index}]")
            if found:
                report.refused.setdefault(name, {})[index] = found
            else:
                kept_values.append(cleaned)
                kept_index.append(index)
        report.clean[name] = kept_values
        report.kept[name] = kept_index
    return report


def undefined_types() -> list[str]:
    """Type names the bundle table uses and the ``types`` table does not define.

    A contract smell test: with one of these the notation cannot be read at all, and the
    failure would otherwise appear as every record of that kind being refused.
    """
    types = contracts.bundle_types()
    scalars = {"str", "int", "num", "bool", "object", "array"}
    missing: set[str] = set()

    def visit(decl: str) -> None:
        base, _ = split_optional(decl)
        inner = item_decl(base)
        if inner is not None:
            visit(inner)
            return
        if base in scalars or base.startswith("enum:"):
            if base.startswith("enum:"):
                try:
                    contracts.label_values(base[len("enum:"):].strip())
                except KeyError:
                    missing.add(base)
            return
        if base not in types:
            missing.add(base)

    for kind in contracts.bundle_kinds():
        for decl in contracts.bundle_spec(kind)["fields"].values():
            visit(decl)
    for table in types.values():
        for decl in table.values():
            visit(decl)
    return sorted(missing)
