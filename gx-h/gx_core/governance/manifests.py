"""Which schema the content of each kind of asset must satisfy.

The schema of every asset kind is a bundle of ``contracts/bundles.json`` (since contract r5,
which gave BENCHMARK ``benchmark_bundle`` and ROLE_INTENT ``role_bundle``), and the check is
core's generic one (:mod:`gx_core.bundles.schema`): unknown fields refused, enums read from
the label axes, optionals honoured. ``gx_asset_submit`` of a kind goes through the validator
its bundle kind's owner publishes.

:func:`declare` is how a stream gave a kind its field table while the contract had no bundle
for it. A kind the contract now has a bundle for may still be declared, so that a module which
declares keeps importing, but only with the contract's own table: the declaration is then
recorded as superseded (:data:`SUPERSEDED`) and nothing reads it. One that differs from the
contract is refused when the module imports::

    from gx_core.governance import manifests
    manifests.declare("BENCHMARK", fields={"manifest": "object", ...},
                      manifest_fields={"benchmark_id": "str", "version": "str", ...})

Nothing here knows what a field means. That is the other checks' work.
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import contracts, registry
from ..bundles import schema

#: asset kind -> the bundle kind whose shape its content has.
KIND_BUNDLE: dict[str, str] = {
    "DECISION_SKILL": "skill_bundle",
    "EXECUTABLE_WORKFLOW": "workflow_bundle",
    "TOOL_ADAPTER": "capability_manifest_bundle",
    "STRATEGY": "strategy_bundle",
    "EVALUATOR": "evalspec_bundle",
    "BENCHMARK": "benchmark_bundle",      # contract r5
    "ROLE_INTENT": "role_bundle",         # contract r5
}
for _kind, _bundle in KIND_BUNDLE.items():
    contracts.require_label("asset_kind", _kind)
    contracts.bundle_spec(_bundle)

#: asset kind -> {"fields": top-level field table, "manifest_fields": field table of the manifest}.
LOCAL: dict[str, dict[str, Any]] = {}
_OWNERS: dict[str, str] = {}
#: asset kind -> the module that still declares the contract's own table for it; nothing reads the declaration.
SUPERSEDED: dict[str, str] = {}


def contract_table(kind: str) -> dict[str, dict[str, str]]:
    """The field tables of a kind's content in the contract: the bundle's fields and its manifest type's."""
    fields = dict(contracts.bundle_spec(KIND_BUNDLE[kind])["fields"])
    manifest_type = fields.get("manifest")
    manifest_fields = contracts.bundle_types().get(manifest_type) if isinstance(manifest_type, str) else None
    return {"fields": fields, "manifest_fields": dict(manifest_fields or {})}


def declare(kind: str, *, fields: Mapping[str, str], manifest_fields: Mapping[str, str], owner: str) -> None:
    """Declare the content schema of a kind; for a kind the contract has a bundle for, only its own table."""
    contracts.require_label("asset_kind", kind)
    if fields.get("manifest") != "object":
        raise ValueError("a content schema has a top-level 'manifest' declared as 'object'")
    if kind in KIND_BUNDLE:
        table = contract_table(kind)
        if ({**dict(fields), "manifest": table["fields"].get("manifest")} != table["fields"]
                or dict(manifest_fields) != table["manifest_fields"]):
            raise ValueError(f"{owner} declares a content schema for {kind} that is not the contract's "
                             f"{KIND_BUNDLE[kind]}; the contract's bundle is the schema of {kind}")
        SUPERSEDED[kind] = owner
        return
    entry = {"fields": dict(fields), "manifest_fields": dict(manifest_fields)}
    existing = LOCAL.get(kind)
    if existing is not None and (existing != entry or _OWNERS.get(kind) != owner):
        raise ValueError(f"the content schema of {kind} is already declared by {_OWNERS.get(kind)}")
    LOCAL[kind] = entry
    _OWNERS[kind] = owner


def schema_source(kind: str) -> str | None:
    """Where the schema of a kind's content comes from, or None when nobody declared one."""
    registry.load_providers()
    if kind in KIND_BUNDLE:
        return f"contracts/bundles.json#{KIND_BUNDLE[kind]}" + (
            f" ({SUPERSEDED[kind]} still declares the same table; it is not read)" if kind in SUPERSEDED else "")
    if kind in LOCAL:
        return f"{_OWNERS[kind]} (declared; the frozen contract has no bundle for {kind})"
    return None


def violations(kind: str, content: Any) -> list[dict[str, str]]:
    """Every way ``content`` departs from the schema of ``kind``. Empty means well formed.

    Raises ``KeyError`` when no schema is declared for the kind at all.
    """
    registry.load_providers()
    if kind in KIND_BUNDLE:
        report = schema.check_bundle(KIND_BUNDLE[kind], content)
        found = [v.as_dict() for v in report.bundle_violations]
        for field_name, by_index in report.refused.items():
            for index, items in sorted(by_index.items()):
                found.extend({"pointer": v.pointer, "message": v.message} for v in items)
        return found
    if kind not in LOCAL:
        raise KeyError(kind)
    entry = LOCAL[kind]
    _clean, top = schema.check_object(content, entry["fields"], "$", type_name=f"{kind} content")
    found = [v.as_dict() for v in top]
    if isinstance(content, dict) and isinstance(content.get("manifest"), dict):
        _clean, inner = schema.check_object(
            content["manifest"], entry["manifest_fields"], "$.manifest", type_name=f"{kind} manifest"
        )
        found.extend(v.as_dict() for v in inner)
    return found
