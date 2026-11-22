"""Project-scoped observations in, immutable evaluation artifact out."""
from __future__ import annotations

import json
import math
from pathlib import Path
import sqlite3
import subprocess

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import ArtifactNotFound, SchemaInvalid

HOST = Path(__file__).resolve().parents[2] / "runtime" / "plugin-host"


def installed_plugin_ref(version: str = "1") -> dict:
    """Resolve a new attempt's installed composition. Save this value in its workflow inputs."""
    if version not in ("1", "2"):
        raise ValueError(f"unsupported score evaluator version {version!r}")
    sources = ("run.ts", "host.ts", "scores.ts", f"score-v{version}.ts", "package-lock.json")
    digests = {name: ids.sha256_of_file(HOST / name) for name in sources}
    packages = json.loads((HOST / "package-lock.json").read_text())["packages"]
    return {
        "package_name": "gx-score-plugin-host", "package_version": version,
        "package_digest": "sha256:" + ids.sha256_of_obj(digests),
        "dependency_lock": "sha256:" + digests["package-lock.json"],
        "host": "cordis", "required_services": {name: packages[f"node_modules/{name}"]["version"]
                                           for name in ("@deepseek-ai/cordis", "@deepseek-ai/dsh-scope")},
        "input_contract": "gx.observation-scores.v1", "output_contract": "gx.sample-summary.v1",
    }


def evaluate_scores(conn: sqlite3.Connection, *, run_id: str, attempt_id: str, plugin_ref: dict,
                    observations_ref: str, threshold: float) -> dict:
    """An attempt's input, cutoff and version are immutable. Only GX publishes artifacts.

    Reopening after a lost reply reads the verified output even if the plugin is no longer
    installed. Before publication the evaluator is pure and safe to run again on recovery.
    """
    if not attempt_id or isinstance(threshold, bool) or not isinstance(threshold, (float, int)) or not math.isfinite(threshold):
        raise SchemaInvalid("an attempt identity and finite numeric threshold are required")
    artifacts = ArtifactService(conn)
    row = artifacts.describe(observations_ref)
    if row["run_id"] != run_id:
        raise ArtifactNotFound("score input is not registered in this run")
    config = {"threshold": float(threshold)}
    spec = {
        "run_id": run_id, "attempt_id": attempt_id, "plugin_ref": plugin_ref,
        "input_ref": observations_ref, "input_sha256": row["sha256"],
        "config": config, "config_digest": "sha256:" + ids.sha256_of_obj(config),
    }
    namespace = f"{run_id}/evaluations/{ids.sha256_of_obj(attempt_id)}"
    location = dict(namespace=namespace, media_type="application/json", run_id=run_id,
                    path_id=row["path_id"], job_id=row["job_id"], task_id=row["task_id"])
    stored = conn.execute("SELECT artifact_ref FROM artifacts WHERE namespace=? AND rel_path='plugin-lock.json'",
                          (namespace,)).fetchone()
    if stored:
        lock_ref = stored["artifact_ref"]
        if artifacts.read_json(lock_ref) != spec:
            raise SchemaInvalid("this evaluation attempt already has different pinned inputs")
    else:
        lock_ref = artifacts.publish_bytes(**location, rel_path="plugin-lock.json", kind="plugin_lock",
                                           data=ids.canonical_json(spec)).artifact_ref
    prior = conn.execute("SELECT artifact_ref FROM artifacts WHERE namespace=? AND rel_path='result.json'",
                         (namespace,)).fetchone()
    if prior:
        result = artifacts.read_json(prior["artifact_ref"])
        return {**result, "artifact_ref": prior["artifact_ref"]}
    version = plugin_ref.get("package_version")
    if plugin_ref != installed_plugin_ref(version):
        raise ValueError("pinned score evaluator unavailable; restore its original source and dependencies")
    document = artifacts.read_json(observations_ref)
    payload = {"run_id": run_id, "attempt_id": attempt_id, "version": version,
               "sample": {"threshold": config["threshold"], "observations": document["observations"]}}
    try:
        reply = subprocess.run(["node", "--experimental-strip-types", str(HOST / "run.ts")],
                               input=json.dumps(payload, allow_nan=False), text=True,
                               capture_output=True, timeout=60, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"score evaluator failed: {exc.stderr[-1500:]}") from exc
    result = {**json.loads(reply.stdout), **spec, "plugin_lock_ref": lock_ref}
    published = artifacts.publish_bytes(**location, rel_path="result.json", kind="score_evaluation",
                                        data=ids.canonical_json(result),
                                        provenance={"plugin_lock_ref": lock_ref, "input_ref": observations_ref})
    return {**result, "artifact_ref": published.artifact_ref}


plugin_ref = installed_plugin_ref
