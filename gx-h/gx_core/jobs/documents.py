"""Attach Job provenance to parsed adapter output."""
from typing import Any


def stamp(document: dict[str, Any], *, job_id: str, capability_version: str,
          manifest_sha256: str) -> dict[str, Any]:
    stamped = dict(document)
    stamped.update(job_id=job_id, capability_version=capability_version,
                   manifest_sha256=manifest_sha256)
    return stamped
