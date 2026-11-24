"""Measure the selected profile in a child process, through the GX resource and host owners."""

from contextlib import closing
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gx-h"))
from examples.p01_fixture import ACTOR, CAPABILITY, activate_grid, prepare, tasks
from gx_core import config
from gx_core.capabilities import probe
from gx_core.context import Ctx, Principal
from gx_core.errors import ResourceDenied
from gx_core.hostobs import caller, detect
from gx_core.jobs import capacity
from gx_core.jobs.service import JobService, PURPOSE_WORKFLOW
from gx_core.orchestration import service
from gx_core.persistence import store


def measure_resources(conn, *, real):
    ctx = Ctx(conn=conn, principal=Principal(subject=ACTOR, scope="operator"), tool="profile-acceptance")
    activate_grid(ctx)
    run = prepare(ctx)
    spec = json.loads(tasks(conn, run)[0]["input_json"])
    jobs = JobService(conn, actor=ACTOR)

    def submit(key):
        return jobs.submit(run_id=run, capability_version=CAPABILITY, purpose=PURPOSE_WORKFLOW,
                           input_manifest_ref=spec["input_manifest_ref"], idempotency_key=key,
                           parameters={"operation": key})

    snapshot = capacity.snapshot(conn)
    cpu = snapshot["cpu"]
    reading = probe.read_gpu_state()
    result = {"allowed": cpu["threads_allowed_for_jobs"], "logical_cpus": cpu["logical_cpus"],
              "quota_cpus": cpu["cgroup_quota_cpus"], "cpu_source": cpu["source"],
              "configured_limits": config.cpu_limits(), "free": cpu["threads_free"],
              "gpu_source": reading["source"], "gpu_devices": reading["devices"], "gpu_problems": reading["problems"],
              "affinity_count": len(os.sched_getaffinity(0))}
    if real:
        job = submit("real-admission")
        ended = jobs.wait(job["job_id"], timeout_seconds=20)
        result.update(job_id=job["job_id"], job_status=ended["status"], output_count=len(ended["outputs"]))
        return result

    # Hold each admitted row before launch. The third submit sees both committed
    # reservations. Real admission, cancellation and re-admission do all writes.
    with patch.object(JobService, "_start", lambda self, row: dict(row)):
        first, second = submit("slot-a"), submit("slot-b")
        try:
            submit("slot-c")
        except ResourceDenied as err:
            refusal = {"code": err.code, "details": err.details}
        else:
            raise AssertionError("a third concurrent CPU thread was admitted")
        full = capacity.cpu_room(conn)
        jobs.cancel(first["job_id"], reason="release the first fixture slot")
        reopened = capacity.cpu_room(conn)
        third = submit("slot-c")
    result.update(held_when_full=full["held"], free_when_full=full["free"], refusal=refusal,
                  free_after_cancel=reopened["free"], admitted_job_ids=[row["job_id"] for row in (first, second, third)])
    return result


def measure_client(conn):
    ctx = Ctx(conn=conn, principal=Principal(subject="profile-reader", scope="operator"), tool="gx_run_start")
    record = service._host_record(ctx, None)
    observed = caller.facts()
    return {"kind": record["kind"], "basis": record["kind_basis"], "detected": detect.detected()["kind"],
            "marker_names": sorted(key for key in os.environ if key.startswith("CLAUDE")),
            "ambient_sentinel_present": "GX_UNLISTED_HOST_MARKER" in os.environ,
            "tool_shell_marker": observed["tool_shell_variable_set"], "hook_marker": observed["hook_variable_set"],
            "home": os.environ["HOME"], "codex_home": os.environ["CODEX_HOME"]}


if __name__ == "__main__":
    mode, database = sys.argv[1:3]
    if mode == "T01" and len(sys.argv) > 3:
        allowed = sorted(os.sched_getaffinity(0))
        os.sched_setaffinity(0, allowed[:int(sys.argv[3])])
    with closing(store.open_store(Path(database))) as db:
        sample = measure_client(db) if mode == "T02" else measure_resources(db, real=mode == "REAL_RESOURCES")
    print(json.dumps(sample, sort_keys=True))
