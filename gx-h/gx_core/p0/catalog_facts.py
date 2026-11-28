"""Facts about a deployment and the caller's own observed output. Catalogue reads never launch Jobs."""
import json
import os
import sys
from pathlib import Path

from ..capabilities import environments, manifest
from ..errors import GxError, SchemaInvalid


def dependencies(method):
    argv = method["entry"]["argv_template"]
    tokens = manifest.placeholders(argv[0]) if argv else []
    if len(tokens) != 1 or not (tokens[0] == "env.python" or tokens[0].startswith("env.bin.")):
        raise SchemaInvalid("Method entry must identify its declared environment executable")
    program = tokens[0]
    return {"env_id": method["env_id"], "program": program, "data": method.get("required_data", ()),
            "weights": [w["path"] for w in method.get("weights", ())]}


def _ready(conn, deps, environments_seen):
    if deps is None:
        return None
    env_id = deps["env_id"]
    if env_id not in environments_seen:
        view = environments.readiness_view(conn, env_id)
        env = None
        if view["registered"]:
            env = environments.view(conn, env_id)
        elif env_id == "control-plane-python":
            # Activation registers this process's interpreter when the environment is new.
            env = manifest.EnvView(env_id=env_id, python=sys.executable, prefix=sys.prefix, binaries={})
        ready = view["ready"] if view["fresh"] else (True if env and not view["registered"] else None)
        environments_seen[env_id] = env, ready
    env, ready = environments_seen[env_id]
    try:
        for template in deps["data"]:
            path = manifest.resolve_path(template, env=env)
            if not os.access(path, os.R_OK | (os.X_OK if path.is_dir() else 0)):
                return False
        for template in deps["weights"]:
            path = manifest.resolve_path(template, env=env)
            if not path.is_file() or not os.access(path, os.R_OK):
                return False
    except (GxError, OSError):
        return None
    if env:
        program = env.python if deps["program"] == "env.python" else env.binaries.get(deps["program"][8:])
        if not program or not Path(program).is_file() or not os.access(program, os.X_OK):
            return False
    return ready


def executions(conn, run, brief, candidates):
    """Keep prior successful output even after a failure; never substitute another Run's receipt."""
    if not candidates:
        return set()
    scope = brief["scope"]
    visible_refs = set(brief.get("evidence_refs", ()))
    visible_refs.update(i["artifact_ref"] for i in brief.get("catalog_discovery", {}).get("available_inputs", ())
                        if i.get("artifact_ref"))
    rows = conn.execute("""
        SELECT j.capability_id||'@'||j.capability_version AS cap,
               json_extract(m.admission_json,'$.execution_observation.provider_sha256') AS provider,
               coalesce(json_extract(m.admission_json,'$.module_capability.portable_sha256'),
                        json_extract(m.admission_json,'$.goal_capability.portable_sha256')) AS portable,
               json_extract(m.admission_json,'$.capability.sha256') AS manifest
          FROM v6_nodes n JOIN jobs j ON n.job_id=j.job_id AND j.run_id=n.run_id
          JOIN p0_execution_manifests m ON m.job_id=j.job_id JOIN artifacts a ON a.artifact_ref=n.result_ref
         WHERE n.run_id=? AND n.status='SUCCEEDED' AND j.status='SUCCEEDED'
           AND json_extract(m.completion_json,'$.status')='SUCCEEDED'
           AND j.capability_id||'@'||j.capability_version IN (SELECT value FROM json_each(?))
           AND ((?='run' AND (NOT EXISTS (SELECT 1 FROM v62_modules
                     WHERE run_id=n.run_id AND branch_id=n.branch_id)
               OR EXISTS (SELECT 1 FROM json_each(m.completion_json,'$.outputs') o
                           WHERE json_extract(o.value,'$.artifact_ref') IN (SELECT value FROM json_each(?)))))
               OR n.branch_id=(SELECT branch_id FROM v62_modules WHERE run_id=n.run_id AND scope=?))
    """, (run, json.dumps(candidates), scope, json.dumps(sorted(visible_refs)), scope))
    return set(map(tuple, rows))


def project(conn, row, seen, envs):
    if row.get("status") == "reference_only":
        return {"reference_only": True, "installed": False, "dependency_ready": None, "executed_in_run": False}
    installed = row.get("installation_status") == "installed"
    provider = row.get("_bound_provider_digest")
    digest = row.get("digest")
    cap = row["reference"]
    expected_manifest = row.get("_bound_manifest_digest")
    executed = any(old_cap == cap and old_provider == provider and (
        portable == digest if portable is not None else
        bool(provider or (expected_manifest and expected_manifest == old_manifest)))
        for old_cap, old_provider, portable, old_manifest in seen)
    return {"reference_only": False, "installed": installed,
            "dependency_ready": _ready(conn, row.get("_dependencies"), envs) if installed else None,
            "executed_in_run": executed}
