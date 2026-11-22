"""The contract's shape of a bundle, as this package asks for it.

The reader of ``contracts/bundles.json`` is core's (:mod:`gx_core.bundles.schema`): one
implementation of the notation, so what ``gx_bundle_check`` reports about a capability
bundle and what the registry refuses at intake cannot drift apart. This module only turns
its report into the two things the registry needs: a flat list of problems, and the
canonical form (optional fields given as ``null`` dropped) whose bytes a version is
addressed by.
"""

from __future__ import annotations

from typing import Any

from ..bundles import schema as bundle_schema


def bundle_problems(kind: str, bundle: Any) -> list[str]:
    """Every way ``bundle`` departs from the contract's shape. Empty means it conforms."""
    report = bundle_schema.check_bundle(kind, bundle)
    found = [f"{item.as_dict()['pointer']}: {item.as_dict()['message']}"
             for item in report.bundle_violations]
    for field_name, items in sorted(report.refused.items()):
        for index in sorted(items):
            for item in items[index]:
                found.append(f"{item.as_dict()['pointer']}: {item.as_dict()['message']}")
    return found


def prune_bundle(kind: str, bundle: Any) -> dict[str, Any]:
    """The canonical form of a conforming bundle."""
    return bundle_schema.check_bundle(kind, bundle).clean


__all__ = ["bundle_problems", "prune_bundle"]
