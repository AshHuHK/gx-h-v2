"""Jobs: a detached runner owns the tool; the service submits, observes and settles.

    runner      the process that owns one tool from launch to outcome (standalone, stdlib)
    executor    job directories and the runner: prepare, launch, observe, cancel. No store.
    adapter     the backend's side of an adapter module: literals read, functions called
                out of process
    documents   what the backend itself checks about a document an adapter produced
    service     ``JobService``: submit, status, wait, cancel, reconcile; ``jobs_for_path``
    leases      a job and the task lease it runs under: ``live_deadline``, ``earlier_jobs``,
                adoption, ``cancel_unadopted``
    outcomes    how the jobs of a run ended, path by path: a pure read for a scheduler
    needs       when a need of kind readiness, device_capacity or path_reachability is cleared
    tools       what this package publishes through ``gx_core.registry``

Other streams use ``JobService(conn, actor=…)`` (``submit``, ``status``, ``wait``, ``cancel``,
``reconcile(run_id)``, ``cancel_for_run``, ``cancel_unstarted_for_run``) and
``jobs_for_path(conn, path_id)``.
They are loaded on first use, so importing one module of the package does not load the
service and everything it depends on.

What a job leaves for a reaction: when it becomes SUCCEEDED, FAILED or CANCELLED, the same
transaction writes one outbox row, topic ``job.terminal``, idempotency key
``job.terminal:<job_id>``, payload ``{run_id, subject: <job_id>, status, event_id, path_id,
task_id, purpose}``. A job nobody is waiting for is ended, and so announced, by
``JobService(conn, actor=…).reconcile(run_id)``, called before a write lock is taken.
"""

from __future__ import annotations

from typing import Any

_LAZY = ("JobService", "jobs_for_path")
#: What core calls about a task's jobs (SCHEDULING_DESIGN r2.2 §7.2): ``jobs.live_deadline(conn,
#: task_id)`` in settle, ``jobs.cancel_unadopted(conn, task_id, lease_ref, actor=…)`` after a bind
#: has committed.
_LAZY_LEASES = ("cancel_unadopted", "earlier_jobs", "held_open", "lease_seconds_for", "live_deadline")


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from . import service

        return getattr(service, name)
    if name in _LAZY_LEASES:
        from . import leases

        return getattr(leases, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = list(_LAZY) + list(_LAZY_LEASES)
