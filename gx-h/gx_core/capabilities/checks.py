"""The eight adapter checks of ``contracts/gates.json``.

A check is a question the governance engine asks whenever a gate is read, so every one
of these recomputes its answer from files and rows the backend holds, and none of them
launches a tool or runs a line of a bundle:

* three look at the manifest and the files now (``method_card_present``,
  ``weights_digest``, ``probe_L0_files``);
* ``probe_L1_metadata`` reads the environment's own observation of its packages;
* the other four read what the probe ladder recorded (:mod:`.ladder`) and then look
  again at the rows behind the record — the job, its outputs, the evaluation — because a
  ``probe_records`` row saying PASS is a note, and the job row is the fact.

A rung that has not been climbed for these bytes on this host is ``ERROR``: the check
could not establish its answer, which never counts as a pass. ``FAIL`` means it looked
and the answer was no.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from typing import Any, Callable

from ..context import Ctx
from ..errors import ContentHashMismatch, GxError
from ..persistence import store
from ..registry import CheckOutcome, Subject
from . import environments, ladder, registry, storage
from . import manifest as manifest_mod

PASS, FAIL, ERROR = ladder.PASS, ladder.FAIL, ladder.ERROR
_CONTROL_RUNGS = {"probe_L3_smoke": ladder.L3, "probe_input_error": ladder.ERR,
                  "error_timeout_idempotency": ladder.ERR, "probe_L4_known_answer": ladder.L4}


def _view(ctx: Ctx, subject: Subject) -> dict[str, Any]:
    """The capability a check is about, or a GxError the runner turns into ERROR."""
    view = registry.candidate(ctx.conn, f"{subject.id}@{subject.version}")
    if view["manifest_sha256"] != subject.sha256:
        raise ContentHashMismatch(
            f"the check is about sha256 {subject.sha256[:16]}… and {view['capability_version']} is "
            f"registered at {view['manifest_sha256'][:16]}…",
            details={"subject_sha256": subject.sha256, "manifest_sha256": view["manifest_sha256"]},
        )
    return view


def _checked(fn: Callable[[Ctx, dict[str, Any]], CheckOutcome]) -> Callable[[Ctx, Subject], CheckOutcome]:
    def run(ctx: Ctx, subject: Subject) -> CheckOutcome:
        view = _view(ctx, subject)
        observed = view
        rung = _CONTROL_RUNGS.get(fn.__name__)
        source = (ladder.latest(ctx.conn, view).get(rung, {}).get("reused_from")
                  if rung and view["resources"].get("gpu") is None else None)
        if source:
            observed = registry.candidate(ctx.conn, source["capability_version"])
        outcome = fn(ctx, observed)
        if source:
            outcome.detail["reused_from"] = source
        if fn.__name__ != "method_card_present":
            outcome.detail["observation_inputs"] = ladder.observation_inputs(ctx.conn, view, fn.__name__)
        return outcome

    run.__name__ = fn.__name__
    run.__qualname__ = fn.__qualname__
    run.__doc__ = fn.__doc__
    run.__module__ = fn.__module__
    return run


# ---------------------------------------------------------------- the manifest and files

@_checked
def method_card_present(ctx: Ctx, view: dict[str, Any]) -> CheckOutcome:
    """Family, mechanism, objective, estimator and training-data lineage are all stated."""
    card = view["manifest"]["method_card"]
    empty = [name for name in ("family", "mechanism", "objective", "estimator",
                               "training_data_lineage") if not str(card.get(name) or "").strip()]
    detail = {"family": card.get("family"), "empty": empty,
              "citation_evidence": len(card.get("citation_evidence") or [])}
    return CheckOutcome(PASS if not empty else FAIL, detail=detail)


def _digests(path: str) -> tuple[str, str, int]:
    """sha256, git blob sha1 and size of a file, in one pass."""
    size = os.path.getsize(path)
    sha256 = hashlib.sha256()
    blob = hashlib.sha1(f"blob {size}\0".encode())  # noqa: S324 - git's object id, not security
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha256.update(block)
            blob.update(block)
    return sha256.hexdigest(), blob.hexdigest(), size


@_checked
def weights_digest(ctx: Ctx, view: dict[str, Any]) -> CheckOutcome:
    """Each weight file is the declared bytes; a FULL_PICKLE file also matches its upstream digest.

    An upstream digest is ``sha256:<hex>`` (a digest the upstream published) or
    ``git-blob:<sha1>`` (the object id of the file in the upstream repository at the
    recorded commit). Either is recomputed from the file here. Nothing is loaded.
    """
    weights = view["manifest"]["weights"]
    if not weights:
        return CheckOutcome(PASS, detail={"weights": 0})
    env = environments.get(ctx.conn, view["env_id"])
    env_view = manifest_mod.EnvView(env_id=env["env_id"], python=env["python_executable"],
                                    binaries=dict(env["binary_paths"]), prefix=env["prefix"])
    problems: list[str] = []
    files: list[dict[str, Any]] = []
    total = 0
    for index, weight in enumerate(weights):
        path = manifest_mod.resolve_path(weight["path"], env=env_view)
        entry: dict[str, Any] = {"path": weight["path"], "load_mode": weight["load_mode"]}
        files.append(entry)
        if not path.is_file():
            problems.append(f"weights[{index}] {path} does not exist")
            continue
        sha256, blob, size = _digests(str(path))
        total += size
        entry.update(sha256=sha256, bytes=size)
        if size != weight["bytes"]:
            problems.append(f"weights[{index}] is {size} bytes on disk, {weight['bytes']} declared")
        if sha256 != weight["sha256"]:
            problems.append(f"weights[{index}] hashes to {sha256[:16]}…, {weight['sha256'][:16]}… declared")
        upstream = weight.get("upstream_digest")
        if upstream:
            form, _, value = upstream.partition(":")
            observed = {"sha256": sha256, "git-blob": blob}.get(form)
            entry["upstream_digest"] = upstream
            if observed is None:
                problems.append(f"weights[{index}].upstream_digest {upstream!r} is neither "
                                "sha256:<hex> nor git-blob:<sha1>")
            elif observed != value.lower():
                problems.append(f"weights[{index}] does not match its upstream digest {upstream}: "
                                f"the file is {form}:{observed}")
            else:
                entry["upstream_matched"] = True
        elif weight["load_mode"] == "FULL_PICKLE":
            problems.append(f"weights[{index}] is FULL_PICKLE and records no upstream digest")
    detail = {"weights": len(weights), "bytes": total, "files": files, "problems": problems}
    return CheckOutcome(PASS if not problems else FAIL, detail=detail)


@_checked
def probe_L0_files(ctx: Ctx, view: dict[str, Any]) -> CheckOutcome:  # noqa: N802 - the contract's id
    """Code, weights and declared data exist where the manifest resolves to. Recomputed now."""
    result, detail = ladder.probe_files(ctx.conn, view)
    return CheckOutcome(result, detail=detail)


@_checked
def probe_L1_metadata(ctx: Ctx, view: dict[str, Any]) -> CheckOutcome:  # noqa: N802
    """The environment's package metadata meets its declared requirements, as observed here."""
    result, detail = ladder.probe_metadata(ctx.conn, view)
    return CheckOutcome(result, detail=detail)


# --------------------------------------------------------------- what the ladder recorded

def stale_checks(ctx: Ctx, subject: Subject, latest: dict[str, Any]) -> set[str]:
    """Checks whose recorded local source/environment facts have changed.

    Old evidence remains readable. New observations carry their existing owner's
    reuse key so unchanged files/environments require no repeat hashing or probes.
    """
    selected = {name: item for name, item in latest.items() if name in ALL and name != "method_card_present"}
    if not selected:
        return set()
    view = _view(ctx, subject)
    stale = set()
    observed: dict[str, str] = {}
    for name, item in selected.items():
        row = ctx.conn.execute("SELECT detail_json FROM check_runs WHERE check_run_id=?",
                               (item["check_run_id"],)).fetchone()
        detail = store.loads(row, "detail_json", {}) or {}
        scope = name if name in ("weights_digest", "probe_L0_files", "probe_L1_metadata") else "runtime"
        if scope not in observed:
            observed[scope] = ladder.observation_inputs(ctx.conn, view, scope)
        inputs = observed[scope]
        if detail.get("observation_inputs", inputs) != inputs:
            stale.add(name)
        elif detail.get("reused_from"):
            record = ladder.latest(ctx.conn, view).get(_CONTROL_RUNGS.get(name), {})
            if detail["reused_from"] != record.get("reused_from"):
                stale.add(name)
    return stale


def _record(ctx: Ctx, view: dict[str, Any], rung: str) -> tuple[dict[str, Any] | None, CheckOutcome | None]:
    record = ladder.observed_rung(ctx.conn, view, rung)
    if record is None:
        return None, CheckOutcome(ERROR, detail={
            "reason": f"{rung} has not been observed for these bytes on this host; the probe "
                      "ladder climbs it (gx_capability_register with probe) once the earlier "
                      "rungs, and the review they wait for, are on record",
            "rung": rung,
        })
    if record["result"] != PASS:
        return record, CheckOutcome(record["result"] if record["result"] in (FAIL, ERROR) else FAIL,
                                    detail={"probe_id": record["probe_id"], "at": record["created_at"],
                                            **{k: v for k, v in record["detail"].items()
                                               if k in ("reason", "passed", "status", "failure",
                                                        "problems", "verdict", "expected")}},
                                    job_id=record["job_id"])
    return record, None


def _job(conn: sqlite3.Connection, view: dict[str, Any], job_id: str | None) -> tuple[Any, list[str]]:
    """A probe job's row, and why it does not show what the record says, if it does not."""
    if not job_id:
        return None, ["the record names no job"]
    row = conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if row is None:
        return None, [f"job {job_id} is not in the store"]
    intent = store.loads(row, "submit_intent_json", {}) or {}
    problems = []
    if intent.get("manifest_sha256") != view["manifest_sha256"]:
        problems.append(f"job {job_id} ran other bytes ({str(intent.get('manifest_sha256'))[:16]}…)")
    return row, problems


@_checked
def probe_L2_entrypoint(ctx: Ctx, view: dict[str, Any]) -> CheckOutcome:  # noqa: N802
    """The entry point answered under the launch recipe, for these bytes, on this host."""
    record, refusal = _record(ctx, view, ladder.L2)
    if refusal is not None:
        return refusal
    detail = record["detail"]
    return CheckOutcome(PASS, detail={"probe_id": record["probe_id"], "at": record["created_at"],
                                      "argv": detail.get("argv"), "exit_code": detail.get("exit_code"),
                                      "seconds": detail.get("seconds"), "cwd": detail.get("cwd")})


@_checked
def probe_L3_smoke(ctx: Ctx, view: dict[str, Any]) -> CheckOutcome:  # noqa: N802
    """A smoke job finished through the job service and its output was accepted and still is."""
    record, refusal = _record(ctx, view, ladder.L3)
    if refusal is not None:
        return refusal
    return smoke_evidence(ctx.conn, view, record)


def smoke_evidence(conn: sqlite3.Connection, view: dict[str, Any], record: dict[str, Any]) -> CheckOutcome:
    """Verify a direct L3 observation against its owning Job and published outputs."""
    row, problems = _job(conn, view, record["job_id"])
    outputs: list[dict[str, Any]] = []
    if row is not None:
        if row["status"] != "SUCCEEDED":
            problems.append(f"job {row['job_id']} is {row['status']}, not SUCCEEDED")
        artifacts = storage.artifact_service(conn)
        outputs = list((store.loads(row, "outcome_json", {}) or {}).get("outputs") or [])
        absent = ladder._required_outputs_absent(
            conn, view["manifest"]["outputs"], {"job_id": row["job_id"], "outputs": outputs})
        if absent:
            problems.append(f"required output(s) {absent} were not published")
        for item in outputs:
            try:
                if not artifacts.verify(item["artifact_ref"])["ok"]:
                    problems.append(f"output {item['output_name']} no longer matches its hash")
            except GxError as exc:
                problems.append(f"output {item['output_name']}: {exc.message}")
    detail = {"probe_id": record["probe_id"], "at": record["created_at"],
              "outputs": [item["output_name"] for item in outputs],
              "measured": record["detail"].get("measured"), "problems": problems}
    return CheckOutcome(PASS if not problems else FAIL, detail=detail, job_id=record["job_id"])


@_checked
def probe_input_error(ctx: Ctx, view: dict[str, Any]) -> CheckOutcome:
    """The adapter rejects its malformed request as an input error."""
    record, refusal = _record(ctx, view, ladder.ERR)
    if refusal is not None:
        return refusal
    return input_error_evidence(ctx.conn, view, record)


def input_error_evidence(conn: sqlite3.Connection, view: dict[str, Any], record: dict[str, Any]) -> CheckOutcome:
    """Read the malformed-input result and its Job when the request reached execution."""
    detail = record["detail"]
    malformed = detail.get("malformed") or {}
    passed = bool((detail.get("passed") or {}).get("malformed"))
    problems = [] if passed else ["malformed input did not produce an input error"]
    if malformed.get("refused_before_launch"):
        if not malformed.get("input_error"):
            problems.append("the pre-launch refusal was not an input error")
    else:
        row, found = _job(conn, view, malformed.get("job_id"))
        problems.extend(found)
        if row is not None:
            failure = (store.loads(row, "outcome_json", {}) or {}).get("failure") or {}
            if row["status"] != "FAILED" or failure.get("failure_class") != "input_or_format":
                problems.append("the malformed-input Job was not an input/format failure")
    return CheckOutcome(PASS if not problems else FAIL,
                        detail={"probe_id": record["probe_id"], "at": record["created_at"],
                                "malformed": malformed, "problems": problems},
                        job_id=malformed.get("job_id"))


@_checked
def error_timeout_idempotency(ctx: Ctx, view: dict[str, Any]) -> CheckOutcome:
    """A malformed input was an input error; an overrunning job was ended and called a
    timeout; one idempotency key was one job. Each as the job service showed it."""
    record, refusal = _record(ctx, view, ladder.ERR)
    if refusal is not None:
        return refusal
    return legacy_error_evidence(ctx.conn, view, record)


def legacy_error_evidence(conn: sqlite3.Connection, view: dict[str, Any], record: dict[str, Any]) -> CheckOutcome:
    """Read all three observations made by the original probe."""
    detail = record["detail"]
    passed = detail.get("passed") or {}
    problems = input_error_evidence(conn, view, record).detail["problems"]
    problems.extend(f"{name} did not pass" for name in ("timeout", "idempotency") if not passed.get(name))
    timeout = detail.get("timeout") or {}
    row, found = _job(conn, view, timeout.get("job_id"))
    problems.extend(found)
    if row is not None:
        failure = (store.loads(row, "outcome_json", {}) or {}).get("failure") or {}
        if row["status"] != "FAILED" or not failure.get("timed_out"):
            problems.append(f"job {row['job_id']} is {row['status']} and is not recorded as timed out")
    key_rows = (detail.get("idempotency") or {}).get("rows_for_key")
    if key_rows != 1:
        problems.append(f"the smoke key has {key_rows} job row(s)")
    summary = {"probe_id": record["probe_id"], "at": record["created_at"],
               "malformed": detail.get("malformed"), "timeout": timeout,
               "idempotency": detail.get("idempotency"), "problems": problems}
    return CheckOutcome(PASS if not problems else FAIL, detail=summary, job_id=timeout.get("job_id"))


@_checked
def probe_L4_known_answer(ctx: Ctx, view: dict[str, Any]) -> CheckOutcome:  # noqa: N802
    """The declared control was scored under its frozen specification and gave the expected verdict.

    A manifest that declares no control has nothing to check: the gate does not require this
    check then (gates.json ``required_when``), and when it is asked anyway it never answers
    PASS on zero cases. It answers ERROR, which "never counts as a PASS", with
    ``required: false`` and a reason."""
    control = view["manifest"].get("known_answer")
    if control is None:
        return CheckOutcome(ERROR, detail={"required": False, "known_answer": 0,
                                           "reason": registry.L4_NOT_EXERCISED})
    record, refusal = _record(ctx, view, ladder.L4)
    if refusal is not None:
        return refusal
    return known_answer_evidence(ctx.conn, control, record)


def known_answer_evidence(conn: sqlite3.Connection, control: dict[str, Any], record: dict[str, Any]) -> CheckOutcome:
    """Report the absence of an evaluation provider for reserved L4 records."""
    return CheckOutcome(ERROR, detail={"problems": ["known-answer evaluation provider unavailable"]},
                        job_id=record.get("job_id"))


ALL = {
    "method_card_present": method_card_present,
    "weights_digest": weights_digest,
    "probe_L0_files": probe_L0_files,
    "probe_L1_metadata": probe_L1_metadata,
    "probe_L2_entrypoint": probe_L2_entrypoint,
    "probe_L3_smoke": probe_L3_smoke,
    "probe_input_error": probe_input_error,
    "probe_L4_known_answer": probe_L4_known_answer,
}

__all__ = ["ALL", *ALL]
