"""Workflow definitions by ``(workflow_id, version)``.

The rule this enforces is the asset lifecycle's: *modifying an ACTIVE workflow creates
a new version; running tasks stay pinned to the version they started with*
(``contracts/states.json`` → ``asset.rules.versioning``). Registering changed content
under a version that already exists is refused, because a run that pinned ``v1`` would
otherwise silently resume into different code and its checkpoints would describe a
graph that no longer exists.

Identical re-registration is allowed and is a no-op: importing the same definition
twice in one process must not be an error.
"""

from __future__ import annotations

from typing import Any, Iterable

from ..errors import ArtifactNotFound, SchemaInvalid
from .engine import Workflow


class WorkflowRegistry:
    def __init__(self, workflows: Iterable[Workflow] = ()) -> None:
        self._by_key: dict[tuple[str, str], Workflow] = {}
        for workflow in workflows:
            self.register(workflow)

    # ------------------------------------------------------------------ mutation
    def register(self, workflow: Workflow) -> Workflow:
        key = (workflow.workflow_id, workflow.version)
        existing = self._by_key.get(key)
        if existing is not None:
            if existing.digest() == workflow.digest():
                return existing
            raise SchemaInvalid(
                f"{workflow.workflow_id}@{workflow.version} is already registered with "
                "different content; register a new version instead of changing a pinned one",
                details={
                    "workflow_id": workflow.workflow_id,
                    "version": workflow.version,
                    "registered_digest": existing.digest(),
                    "supplied_digest": workflow.digest(),
                },
            )
        self._by_key[key] = workflow
        return workflow

    # --------------------------------------------------------------------- query
    def get(self, workflow_id: str, version: str) -> Workflow:
        workflow = self._by_key.get((workflow_id, str(version)))
        if workflow is None:
            raise ArtifactNotFound(
                f"no workflow {workflow_id}@{version} is registered",
                details={"workflow_id": workflow_id, "version": version,
                         "known": self.versions(workflow_id)},
            )
        return workflow

    def has(self, workflow_id: str, version: str) -> bool:
        return (workflow_id, str(version)) in self._by_key

    def versions(self, workflow_id: str) -> list[str]:
        return sorted(v for (wid, v) in self._by_key if wid == workflow_id)

    def ids(self) -> list[str]:
        return sorted({wid for (wid, _) in self._by_key})

    def describe(self) -> list[dict[str, Any]]:
        return [
            {"workflow_id": wid, "version": version, "digest": workflow.digest(),
             "nodes": list(workflow.order), "description": workflow.description}
            for (wid, version), workflow in sorted(self._by_key.items())
        ]

    def __len__(self) -> int:
        return len(self._by_key)
