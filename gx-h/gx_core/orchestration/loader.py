"""Build a :class:`Workflow` from a versioned JSON definition plus bound callables.

The shape of the graph (node names, dependencies, which node needs external
intelligence) is data in ``workflows/<id>/<version>.json`` so it can be diffed and
version-pinned. The node bodies are Python, bound by name at load time.

The loader refuses a definition whose node set does not match the bindings exactly, in
either direction. A node declared in JSON with no implementation would otherwise fail
only when the graph reached it — after the run had already spent budget — and a bound
function missing from the JSON is a node that silently never runs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Mapping

from ..errors import SchemaInvalid
from .engine import CompletionEvidence, ObjectiveCoverage, Node, NodeResult, Workflow

NodeFn = Callable[[Any], NodeResult]


def load_definition(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    for required in ("workflow_id", "version", "nodes"):
        if required not in data:
            raise SchemaInvalid(
                f"workflow definition {path} is missing {required!r}",
                details={"path": str(path), "missing": required},
            )
    return data


def build(definition: Mapping[str, Any], bindings: Mapping[str, NodeFn], *,
          stage_rules: Mapping[str, Callable[..., Mapping[str, Any]]] | None = None,
          completion_evidence: CompletionEvidence | None = None,
          objective_coverage: ObjectiveCoverage | None = None,
          task_product_validator: Callable[..., None] | None = None) -> Workflow:
    declared = [n["name"] for n in definition["nodes"]]
    missing_impl = [name for name in declared if name not in bindings]
    if missing_impl:
        raise SchemaInvalid(
            f"{definition['workflow_id']}@{definition['version']} declares nodes with no "
            f"implementation: {missing_impl}",
            details={"missing_implementation": missing_impl},
        )
    unused = [name for name in bindings if name not in declared]
    if unused:
        raise SchemaInvalid(
            f"{definition['workflow_id']}@{definition['version']} has implementations for "
            f"nodes it does not declare: {unused}",
            details={"unbound_implementations": unused},
        )
    nodes = [
        Node(
            name=spec["name"],
            fn=bindings[spec["name"]],
            deps=tuple(spec.get("deps", ())),
            idempotent=bool(spec.get("idempotent", True)),
            work=spec.get("work"),
        )
        for spec in definition["nodes"]
    ]
    return Workflow(
        workflow_id=definition["workflow_id"],
        version=str(definition["version"]),
        nodes=nodes,
        entry_transitions=tuple(definition.get("entry_transitions", ("PLANNING", "ACTIVE"))),
        description=definition.get("description", ""),
        document=definition,
        stage_rules=stage_rules,
        completion_evidence=completion_evidence, objective_coverage=objective_coverage,
        task_product_validator=task_product_validator,
    )


def load(path: str | Path, bindings: Mapping[str, NodeFn]) -> Workflow:
    return build(load_definition(path), bindings)
