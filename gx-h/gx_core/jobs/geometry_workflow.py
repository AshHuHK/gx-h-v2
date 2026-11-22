"""Stable Job operations for coordinate inventory and proper rigid superposition."""
from ..capabilities.shipped import GEOMETRY, QUALITY
from .workflow import observe_workflow_job, submit_workflow_job


def submit_quality(conn, *, run_id, operation_id, structure_ref, binding=None):
    return submit_workflow_job(conn, run_id=run_id, operation_id=operation_id, family="tl1a-coordinate-quality",
        capability=QUALITY, inputs={"structure": structure_ref}, parameters={}, binding=binding)


def observe_quality(conn, job_id):
    return observe_workflow_job(conn, job_id, "quality.json")


def submit_geometry(conn, *, run_id, operation_id, structure_ref, protocol, criteria_ref, binding=None,
                    quality_ref=None):
    # A direct fit selects atoms from the criteria and original coordinates. A composed
    # fit reads the first workflow's measured atom inventory as an explicit Job input.
    inputs = {"structure": structure_ref, "criteria": criteria_ref, "selection": quality_ref or criteria_ref}
    return submit_workflow_job(conn, run_id=run_id, operation_id=operation_id, family="tl1a-geometry",
        capability=GEOMETRY, inputs=inputs, parameters={"protocol": protocol}, binding=binding)


def observe_geometry(conn, job_id):
    return observe_workflow_job(conn, job_id, "geometry.json")
