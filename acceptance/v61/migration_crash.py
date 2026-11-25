"""Crash the old CLI process after a real Job submit returns, before driver reconciliation."""

import json
import os
from pathlib import Path
import runpy
import sys

deploy, state, rid, receipt = sys.argv[1:]
sys.path.insert(0, str(Path(deploy) / "gx-h"))
from gx_core.jobs.service import JobService

submit = JobService.submit


def exit_after_acceptance(self, **request):
    result = submit(self, **request)
    if request["run_id"] == rid:
        marker = Path(receipt)
        with marker.open("x") as out:
            json.dump(
                {
                    "job_id": result["job_id"],
                    "operation_key": request["idempotency_key"],
                    "crash_boundary": "old V6 JobService.submit returned, Driver has not reconciled",
                },
                out,
            )
            out.flush()
            os.fsync(out.fileno())
        os._exit(83)
    return result


JobService.submit = exit_after_acceptance
sys.argv = [
    str(Path(deploy) / "bin/gx-p0"),
    "resume",
    "--state",
    state,
    "--run-id",
    rid,
    "--timeout",
    "12",
]
runpy.run_path(sys.argv[0], run_name="__main__")
