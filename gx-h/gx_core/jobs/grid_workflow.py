"""The synthetic grid workflow uses project-owned Jobs and their original outputs."""
import sqlite3

from ..capabilities.shipped import GRID
from .workflow import observe_workflow_job, submit_workflow_job


def submit_grid(conn: sqlite3.Connection, *, run_id: str, operation_id: str, input_ref: str,
                binding: dict | None = None) -> dict:
    """Start or recover the original frozen grid operation."""
    return submit_workflow_job(conn, run_id=run_id, operation_id=operation_id, family="grid",
        capability=GRID, inputs={"grid": input_ref}, parameters={"operation": operation_id}, binding=binding)


def observe_grid(conn: sqlite3.Connection, job_id: str) -> dict:
    return observe_workflow_job(conn, job_id, "observations.json")
