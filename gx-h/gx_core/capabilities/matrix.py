"""The capability chain matrix, generated from the rows that are its evidence.

One line per registered capability version: what it is, where it runs, how far up the
probe ladder it has been observed, and the job and evaluation ids that observation rests
on. The first build kept such a matrix by hand, and a hand-kept matrix says what its
author believed on the day it was written.

Nothing in a line is copied from a status somebody stored. ``chain_status`` is derived
here, each time, and a level is reported only if the row it cites still exists and still
says what the level needs:

    UNTESTED                 registered; no bundled code has been observed to run
    ENTRYPOINT_ANSWERED      L2 passed for these bytes on this host
    SMOKE_TESTED             + a smoke job that is in the jobs table and SUCCEEDED
    ERRORS_PROBED            + the adapter's observed malformed-input result
    KNOWN_ANSWER_EVALUATED   + evaluations that are in the evaluations table with the
                             expected verdict

An error rejected before launch has no Job. Historical combined probes cite their
timeout Job. A missing Job or evaluation lowers the reported level.

``chain_status`` is a LOCAL vocabulary, not from ``contracts/``.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from typing import Any

from ..persistence import store
from . import checks, environments, ladder, registry

CHAIN_STATUSES = ("UNTESTED", "ENTRYPOINT_ANSWERED", "SMOKE_TESTED", "ERRORS_PROBED",
                  "KNOWN_ANSWER_EVALUATED")

COLUMNS = (
    "capability_id", "version", "kind", "method_family", "asset_status", "chain_status",
    "probe_rung", "test_status", "env_id", "env_kind", "env_status", "env_probed_at", "program",
    "smoke_job_id", "error_probe_job_id", "known_answer_eval_ids", "known_answer_spec",
    "declared_cpu_threads", "declared_wall_seconds", "declared_gpu_mib", "measured_jobs",
    "measured_wall_seconds_max", "measured_max_rss_mib_max", "measured_gpu_peak_delta_mib_max", "tool_peak_reserved_mib_max",
    "weights", "weight_load_modes", "manifest_sha256", "licence", "note",
)


def _job_succeeded(conn: sqlite3.Connection, job_id: str | None, manifest_sha256: str) -> str | None:
    """None when the job shows what a record says; otherwise why it does not."""
    if not job_id:
        return "the record names no job"
    row = conn.execute("SELECT status, submit_intent_json FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return f"job {job_id} is not in the store"
    if (store.loads(row, "submit_intent_json", {}) or {}).get("manifest_sha256") != manifest_sha256:
        return f"job {job_id} ran other bytes"
    return None if row["status"] == "SUCCEEDED" else f"job {job_id} is {row['status']}"


def row_for(conn: sqlite3.Connection, capability_version: str) -> dict[str, Any]:
    view = registry.candidate(conn, capability_version)
    manifest = view["manifest"]
    records = ladder.latest(conn, view)
    notes: list[str] = []

    def passed(rung: str) -> dict[str, Any] | None:
        record = records.get(rung)
        return record if record is not None and record["result"] == ladder.PASS else None

    status = "UNTESTED"
    smoke_job = error_job = None
    eval_ids: list[str] = []
    if passed(ladder.L2) is not None:
        status = "ENTRYPOINT_ANSWERED"
        smoke = passed(ladder.L3)
        if smoke is not None:
            source = smoke.get("reused_from")
            observed = registry.candidate(conn, source["capability_version"]) if source else view
            problem = _job_succeeded(conn, smoke["job_id"], observed["manifest_sha256"])
            if source:
                notes.append(f"software controls observed on {source['capability_version']} ({source['manifest_sha256']})")
            if problem is None:
                status, smoke_job = "SMOKE_TESTED", smoke["job_id"]
            else:
                notes.append(f"smoke record not counted: {problem}")
        errors = passed(ladder.ERR)
        if smoke_job is not None and errors is not None:
            if "timeout" in errors["detail"] or "idempotency" in errors["detail"]:
                evidence = checks.legacy_error_evidence(conn, observed, errors)
            else:
                evidence = checks.input_error_evidence(conn, observed, errors)
            if evidence.result == ladder.PASS:
                status, error_job = "ERRORS_PROBED", evidence.job_id
            else:
                notes.extend(evidence.detail["problems"])
        control = manifest.get("known_answer")
        answer = passed(ladder.L4)
        if status == "ERRORS_PROBED" and control is not None and answer is not None:
            evidence = checks.known_answer_evidence(conn, control, answer)
            if evidence.result == ladder.PASS:
                status, eval_ids = "KNOWN_ANSWER_EVALUATED", evidence.detail["eval_ids"]
            else:
                notes.extend(evidence.detail["problems"])
        elif control is None:
            notes.append("declares no known-answer control")

    try:
        env = environments.get(conn, view["env_id"])
    except Exception as exc:  # noqa: BLE001 - a missing environment is a line in the matrix, not a crash
        env = {"kind": None, "status": "NOT_REGISTERED", "probed_at": None, "python_executable": None,
               "binary_paths": {}}
        notes.append(str(exc)[:120])
    head = manifest["entry"]["argv_template"][0].strip("{}")
    program = env["python_executable"] if head == "env.python" else env["binary_paths"].get(head[len("env.bin."):])
    measured = view["measured"]
    gpu = manifest["resources"].get("gpu")

    def top(key: str) -> Any:
        return (measured.get(key) or {}).get("max")

    return {
        "capability_id": view["capability_id"], "version": view["version"], "kind": view["kind"],
        "method_family": manifest["method_card"]["family"], "asset_status": view["status"],
        "chain_status": status, "probe_rung": view["probe_rung"], "test_status": view["test_status"],
        "env_id": view["env_id"], "env_kind": env["kind"], "env_status": env["status"],
        "env_probed_at": env["probed_at"], "program": program,
        "smoke_job_id": smoke_job, "error_probe_job_id": error_job,
        "known_answer_eval_ids": " ".join(eval_ids),
        "known_answer_spec": (manifest.get("known_answer") or {}).get("eval_spec_ref"),
        "declared_cpu_threads": manifest["resources"]["cpu_threads"],
        "declared_wall_seconds": manifest["resources"]["wall_seconds"],
        "declared_gpu_mib": gpu["memory_mib"] if gpu else None,
        "measured_jobs": measured["jobs"], "measured_wall_seconds_max": top("wall_seconds"),
        "measured_max_rss_mib_max": top("max_rss_mib"),
        "measured_gpu_peak_delta_mib_max": top("gpu_peak_delta_mib"),  # shared device
        "tool_peak_reserved_mib_max": top("tool_peak_reserved_mib"),
        "weights": len(manifest["weights"]),
        "weight_load_modes": " ".join(sorted({weight["load_mode"] for weight in manifest["weights"]})),
        "manifest_sha256": view["manifest_sha256"], "licence": manifest["licence"],
        "note": "; ".join(notes),
    }


def rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One line per registered capability version, in id and registration order."""
    found = conn.execute(
        "SELECT capability_id, version FROM capabilities ORDER BY capability_id, created_at, version").fetchall()
    out = []
    for item in found:
        capability_version = f"{item['capability_id']}@{item['version']}"
        try:
            out.append(row_for(conn, capability_version))
        except Exception as exc:  # noqa: BLE001 - one unreadable version must not hide the others
            out.append({**{column: None for column in COLUMNS}, "capability_id": item["capability_id"],
                        "version": item["version"], "chain_status": "UNTESTED",
                        "note": f"could not be read: {type(exc).__name__}: {str(exc)[:160]}"})
    return out


def to_csv(lines: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(COLUMNS), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for line in lines:
        writer.writerow({column: ("" if line.get(column) is None else line[column]) for column in COLUMNS})
    return buffer.getvalue()


def generate(conn: sqlite3.Connection) -> str:
    """``MODEL_ENV_MCP_MATRIX.csv`` as text, from the store as it is now."""
    return to_csv(rows(conn))


__all__ = ["CHAIN_STATUSES", "COLUMNS", "generate", "row_for", "rows", "to_csv"]
