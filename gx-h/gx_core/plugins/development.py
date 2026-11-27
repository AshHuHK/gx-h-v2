"""Install a plugin-declared analysis template and enable its exact method for one Goal."""
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory

from .. import config
from ..domain import ids
from .catalog import package_digest
from .cli import _install


def install(conn, run_id, owner, *, build_ref, capability, scripts, documents, expected_revision, authority):
    from ..p0 import capability_views

    with TemporaryDirectory(prefix="gx-derived-method-") as directory:
        root = Path(directory)
        for name in (*scripts, *documents):
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or path.as_posix() != name:
                raise ValueError("Expected a canonical package member name")
            (root / path).parent.mkdir(parents=True, exist_ok=True)
        # All executable bytes originate in the verified builder plugin. The author supplies JSON data only.
        for name, template in scripts.items():
            if not name.endswith(".py") or not template.endswith(".py"):
                raise ValueError("Expected an installed Python template")
            (root / name).write_bytes(owner.read_file(template))
        for name, document in documents.items():
            if not name.endswith(".json"):
                raise ValueError("Authored method documents must be JSON")
            (root / name).write_text(ids.canonical_json(document) + "\n", encoding="utf-8")
        installation = _install(root, package_digest(root), config.HARNESS_ROOT / "plugins/installed",
                                config.CONFIG_DIR / "workflow_packages.json")
    provider = installation["id"] + "@" + installation["version"]
    result = capability_views.change(conn, run_id, enable=capability, provider=provider,
                                     expected_revision=expected_revision, actor="derived-method-builder", authority=authority,
                                     request_id="build:" + ids.sha256_of_obj([build_ref, expected_revision]))
    return {"build_ref": build_ref, "capability_version": capability, "plugin": installation, "view": result}


def skills(conn, run_id):
    """Project each active source-grounded skill from the exact pinned analytical provider."""
    import json
    from . import goal
    cards = []
    for row in goal.catalog(conn, run_id):
        if (row.get("method") or {}).get("family") != "record_evidence_coverage":
            continue
        provider = goal._provider(row)
        if provider is None:
            continue
        spec = json.loads(provider.read_file("method-spec.json"))
        if spec.get("schema") != "gx.derived-record-method/1" or spec["method"] != row["capability_version"]:
            continue
        cards.append({"kind": "skill", "reference": spec["method"], "method": spec["method"],
                      "description": row["method"]["mechanism"], "plugin": row["plugin"], "source": spec["source"],
                      "limitations": row["limitations"], "calibration": spec["calibration"], "execution_granted": True})
    return cards
