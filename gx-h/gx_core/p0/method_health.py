"""Recent Job execution observations for the current deployed method; queries never launch probes."""
from __future__ import annotations

import os

from .. import config
from ..capabilities import environments
from ..domain import clock, ids

WINDOW_SECONDS = 24 * 3600


def deployment_environment(env):
    """Identify the deployment path and the observed, locally registered environment at admission."""
    observed = env["probe"]
    if not env["probed_at"] or observed.get("probe_host") != os.uname().nodename:
        return None
    return ids.sha256_of_obj({
        "release_root": str(config.HARNESS_ROOT), "host": observed["probe_host"],
        "environment": env["env_id"], "spec": env["spec"], "paths": observed.get("resolved"),
        "interpreter": (observed.get("interpreter") or {}).get("reported"),
        "requirements": observed.get("requirements"),
        "binaries": {name: [value.get("bytes"), value.get("answered"),
                            (value.get("run") or {}).get("stdout")]
                     for name, value in observed.get("binaries", {}).items()},
    })


def recent(conn, run_id, methods):
    """Read only settled, matching Job receipts. Cross-Run observations disclose no Job or material refs."""
    if not methods:
        return {}
    lower, now = clock.iso_after(-WINDOW_SECONDS), clock.now_iso()
    cached_envs, found = {}, {}
    for method in methods:
        cap = method["capability_version"]
        cap_id, version = cap.rsplit("@", 1)
        registered = conn.execute("SELECT env_id FROM capabilities WHERE capability_id=? AND version=? "
                                  "AND manifest_sha256=?", (cap_id, version, method.get("manifest_sha256"))).fetchone()
        if not registered or not registered[0]:
            continue
        env_id = registered[0]
        if env_id not in cached_envs:
            cached_envs[env_id] = deployment_environment(environments.get(conn, env_id))
        env_key = cached_envs[env_id]
        if not env_key:
            continue
        row = conn.execute("""
            SELECT j.run_id,j.job_id,j.status,j.finished_at,
                   json_extract(j.outcome_json,'$.failure.observed'),
                   json_extract(j.outcome_json,'$.failure.failure_class'),
                   json_extract(j.outcome_json,'$.failure.decision_detail.kind')
            FROM jobs AS j JOIN p0_execution_manifests AS m USING(job_id)
            WHERE j.capability_id=? AND j.capability_version=? AND j.env_id=?
              AND j.status IN ('SUCCEEDED','FAILED') AND j.finished_at BETWEEN ? AND ?
              AND (j.run_id=? OR EXISTS (
                  SELECT 1 FROM p0_workspace_runs own JOIN p0_workspace_runs other USING(workspace_id)
                  WHERE own.run_id=? AND other.run_id=j.run_id))
              AND json_extract(m.admission_json,'$.capability.sha256')=?
              AND json_extract(m.admission_json,'$.execution_observation.deployment_environment')=?
              AND json_extract(m.admission_json,'$.execution_observation.provider_sha256') IS ?
              AND json_extract(m.completion_json,'$.status')=j.status
            ORDER BY j.finished_at DESC,j.rowid DESC LIMIT 1
        """, (cap_id, version, env_id, lower, now, run_id, run_id, method.get("manifest_sha256"), env_key,
              (method.get("plugin") or {}).get("sha256"))).fetchone()
        if row:
            evidence = {"status": row[2], "environment": env_id, "observed_at": row[3],
                        "scope": "this_run" if row[0] == run_id else "same_deployment",
                        "basis": "job_execution", "window_seconds": WINDOW_SECONDS}
            if row[0] == run_id:
                evidence["job_id"] = row[1]
            if row[2] == "FAILED":
                evidence.update(observed=row[4], failure_class=row[5], input_exclusion=row[6] == "archive_exclusion")
            found[cap] = evidence
    return found
