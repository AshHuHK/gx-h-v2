"""A one-shot, task-scoped evidence handoff through the project's selected provider."""
from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import tempfile
import threading
import time
from contextlib import closing
from pathlib import Path

from .. import contracts
from ..artifacts.service import ArtifactService
from ..domain import ids
from ..errors import BudgetExceeded, GxError, LeaseExpired, SchemaInvalid
from ..persistence import store
from . import dsh_activity
from .chat_provider import CAPABILITIES, post_chat, validated_profile

SCOPE = "research.model_activity"
OUTPUT_KINDS = ("science_assessment", "science_decision")
_ACTIVE_DRIVERS = set()


def validated_profiles(document):
    if not isinstance(document, dict) or set(document) - set(OUTPUT_KINDS):
        raise SchemaInvalid("activity profiles must map scientific output kinds to execution configurations")
    return {kind: dsh_activity.validated_profile(profile)
            if isinstance(profile, dict) and profile.get("runtime") == "dsh_sdk" else validated_profile(profile)
            for kind, profile in document.items()}


def _rows(conn, task_id):
    task = conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
    if task is None:
        raise SchemaInvalid("unknown Model Activity task")
    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (task["run_id"],)).fetchone()
    return task, run


def _check_lease(conn, task, run, lease_id):
    lease = store.validate_lease(conn, lease_id, task_id=task["task_id"])
    if (lease["attempt"] != task["attempt"] or task["lease_id"] != lease_id
            or task["status"] not in ("CLAIMED", "RUNNING")
            or run["status"] in ("CANCELLED", "FAILED", "PAUSED", "STOPPED", "COMPLETED")):
        raise LeaseExpired("the activity no longer belongs to an active assignment")


def _key(task_id, lease_id):
    return f"{SCOPE}:{task_id}:{lease_id}"


def _event(conn, run_id, key, kind, payload):
    return store.append_event(conn, run_id=run_id, kind=f"model_activity.{kind}",
                              actor="gx:model_activity", subject_kind="model_activity",
                              subject_id=key, payload=payload, evidence_level="EXECUTION_OBSERVED")


def _events(conn, key):
    return conn.execute("SELECT kind,payload_json FROM events WHERE subject_kind='model_activity'"
                        " AND subject_id=? ORDER BY event_id", (key,)).fetchall()


def _driver_id(conn, key):
    return conn.execute("PRAGMA database_list").fetchone()["file"], key


def activity_status(conn, task_id, lease_id):
    key = _key(task_id, lease_id)
    completed = store.stored_response(conn, key)
    if completed:
        return completed
    row = conn.execute("SELECT side_effect_status FROM idempotency WHERE idempotency_key=?", (key,)).fetchone()
    if not row:
        return {"operation_id": key, "status": "not_started"}
    if _driver_id(conn, key) in _ACTIVE_DRIVERS and row["side_effect_status"] != "UNKNOWN":
        return {"operation_id": key, "status": "pending"}
    events = _events(conn, key)
    started = next((store.loads(e, "payload_json") for e in events if e["kind"] == "model_activity.started"), {})
    if started.get("session_id"):
        return {"operation_id": key, "status": "outcome_unknown", "session_id": started["session_id"],
                "reconciliation": "inspect saved DSH session history before retrying",
                "context_ref": started.get("context_ref")}
    return {"operation_id": key, "status": "outcome_unknown",
            "last_observation": events[-1]["kind"] if events else None,
            "reconciliation": "no provider operation lookup; do not resend"}


def cancel_activity(conn, task_id, lease_id):
    key = _key(task_id, lease_id)
    with store.transaction(conn):
        result = activity_status(conn, task_id, lease_id)
        if result["status"] not in ("pending", "outcome_unknown") or store.stored_response(conn, key):
            return result
        if not any(e["kind"] == "model_activity.cancel_requested" for e in _events(conn, key)):
            task, run = _rows(conn, task_id)
            _event(conn, run["run_id"], key, "cancel_requested", {"task_id": task_id, "lease_id": lease_id})
        return {"status": "cancel_requested", "operation_id": key}


def _put(service, task, kind, document):
    content = json.dumps(document, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    metadata = dict(run_id=task["run_id"], task_id=task["task_id"], origin="HOST_PUT",
                    kind=kind, put_by="gx:model_activity", media_type="application/json")
    if len(content) <= contracts.response_limits()["max_inline_put_bytes"]:
        return service.put(content=content, **metadata).artifact_ref
    with tempfile.NamedTemporaryFile(dir=service.inbox_dir(task["run_id"]), delete=False) as output:
        path = Path(output.name)
        try:
            output.write(content)
            output.flush()
            return service.put(inbox_path=path.name, **metadata).artifact_ref
        finally:
            path.unlink(missing_ok=True)


def _context(conn, task, run, profile, input_refs, instruction, acceptance, kind, attempt):
    if kind not in OUTPUT_KINDS:
        raise SchemaInvalid("unsupported Model Activity output kind")
    assigned = store.loads(task, "input_json", {}).get("output_kind")
    if kind != assigned and kind not in store.loads(task, "required_artifacts_json", []):
        raise SchemaInvalid("this task does not call for the selected activity output")
    if not isinstance(instruction, str) or not instruction.strip():
        raise SchemaInvalid("a task instruction is required")
    if not isinstance(input_refs, (tuple, list)) or not input_refs or not all(isinstance(r, str) for r in input_refs):
        raise SchemaInvalid("the task context needs a sequence of GX artifact references")
    service = ArtifactService(conn)
    documents = []
    remaining = profile["max_input_bytes"]
    for ref in dict.fromkeys(input_refs):
        row = conn.execute("SELECT sha256,kind,bytes,media_type,job_id,run_id FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone()
        if row is None or row["run_id"] != run["run_id"]:
            raise SchemaInvalid("a Model Activity input must be a registered artifact of this run")
        if row["bytes"] > remaining:
            raise BudgetExceeded("activity input originals exceed the profile's byte limit")
        raw = service.read_bytes(ref, max_bytes=max(1, remaining))
        try:
            content = raw.decode("utf-8")
        except UnicodeError as exc:
            raise SchemaInvalid("this profile takes UTF-8 evidence artifacts") from exc
        documents.append({"artifact_ref": ref, "sha256": row["sha256"], "job_id": row["job_id"],
                          "kind": row["kind"], "media_type": row["media_type"], "content": content})
        remaining -= len(raw)
    context = {"project_ref": run["project_ref"], "run_id": run["run_id"], "task_id": task["task_id"],
               "attempt": attempt, "objective": run["objective"], "instruction": instruction,
               "output_kind": kind, "task_input": store.loads(task, "input_json", {}),
               "acceptance": acceptance if acceptance is not None else store.loads(task, "acceptance_json", {}),
               "originals": documents,
               "output_token_budget": profile["max_output_tokens"]}
    if len(json.dumps(context, ensure_ascii=False).encode()) > profile["max_input_bytes"]:
        raise BudgetExceeded("complete Model Activity context exceeds the profile's byte limit")
    return context


def _invoke(conn, profile, request, task_id, lease_id, key):
    if profile.get("runtime") == "dsh_sdk":
        def cancelled():
            task, run = _rows(conn, task_id)
            try:
                _check_lease(conn, task, run, lease_id)
            except LeaseExpired:
                return "assignment_closed"
            if conn.execute("SELECT 1 FROM events WHERE subject_id=? AND kind='model_activity.cancel_requested'",
                            (key,)).fetchone():
                return "requested"
            return None
        return dsh_activity.invoke(profile, request, store.data_root(conn) / "dsh" / task_id, key, cancelled)
    mp = multiprocessing.get_context("spawn")
    reader, writer = mp.Pipe(duplex=False)
    process = mp.Process(target=post_chat, args=(profile, request, writer))
    try:
        process.start()
        writer.close()
        deadline = time.monotonic() + profile["timeout_seconds"]
        while not reader.poll(0.1):
            if not process.is_alive() or time.monotonic() >= deadline:
                return {"status": "outcome_unknown", "reason": "worker_exited_or_timeout"}
            task, run = _rows(conn, task_id)
            try:
                _check_lease(conn, task, run, lease_id)
            except LeaseExpired:
                return {"status": "cancelled_locally", "reason": "assignment_closed"}
            if conn.execute("SELECT 1 FROM events WHERE subject_id=? AND kind='model_activity.cancel_requested'",
                            (key,)).fetchone():
                return {"status": "cancelled_locally", "reason": "requested"}
        try:
            return reader.recv()
        except EOFError:
            return {"status": "outcome_unknown", "reason": "response_lost"}
    finally:
        writer.close()
        reader.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=2)
            if process.is_alive():
                process.kill()
                process.join()


def _receipt(service, task, context, profile, key, answer, eligible):
    receipt = {"operation_id": key, "status": answer["status"], "profile": profile,
               "provider_request_id": None, "usage": None, "cost_usd": None,
               "remote_stopped": None, "artifact_ref": None}
    if answer["status"] != "response_received":
        return {**receipt, **answer}
    body = answer["body"]
    if not isinstance(body, dict):
        return {**receipt, "status": "invalid_response", "reason": "provider_response_type"}
    receipt.update(response_ref=_put(service, task, "model_activity_response", body),
                   provider_request_id=body.get("id"), observed_model=body.get("model"),
                   usage=body.get("usage"), usage_source="chat_completions_response")
    sdk = profile.get("runtime") == "dsh_sdk"
    if sdk:
        receipt.update(session_id=body["session_id"], usage_source="messages_sse_response")
    if not eligible:
        return {**receipt, "status": "late_response"}
    if body.get("model") != profile["model"]:
        return {**receipt, "status": "model_mismatch"}
    try:
        if sdk:
            parsed = json.loads(body["final_response"])
            completed = body["finish_reason"] == "completed"
        else:
            choice = body["choices"][0]
            parsed = json.loads(choice["message"]["content"])
            completed = choice.get("finish_reason") == "stop"
        if not isinstance(parsed, dict) or not completed:
            raise ValueError("incomplete JSON object")
    except (ValueError, KeyError, IndexError, TypeError):
        return {**receipt, "status": "invalid_response", "reason": "incomplete_or_invalid_json"}
    receipt.update(status="completed", artifact_ref=_put(service, task, context["output_kind"], parsed))
    return receipt


def _remaining_requests(conn, run_id, profile_sha, limit):
    count = conn.execute(
        "SELECT count(*) FROM events WHERE run_id=? AND kind IN ('model_activity.started','model_activity.retry_started')"
        " AND json_extract(payload_json,'$.profile_sha256')=?", (run_id, profile_sha)).fetchone()[0]
    return limit - count


def run_activity(conn, *, task_id, lease_id, output_kind, input_refs, instruction, acceptance=None,
                 native_worker=None):
    """Return the original receipt on replay. Model output is submitted by the acquired GX worker."""
    if conn.in_transaction:
        raise ValueError("Model Activity needs its own short transactions around the network call")
    task, run = _rows(conn, task_id)
    inputs = store.loads(run, "inputs_json", {})
    config = validated_profiles(inputs.get("inputs", inputs).get("execution_profiles", {}))
    if output_kind not in config:
        raise SchemaInvalid("this task has no configured Model Activity profile")
    profile = config[output_kind]
    lease = conn.execute("SELECT attempt FROM leases WHERE task_id=? AND lease_id=?", (task_id, lease_id)).fetchone()
    if lease is None:
        raise LeaseExpired("this task has no such assignment")
    context = _context(conn, task, run, profile, input_refs, instruction, acceptance, output_kind, lease["attempt"])
    request = {"model": profile["model"], "max_tokens": profile["max_output_tokens"],
               "stream": False, "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": "Use the attached research evidence and return the requested JSON object."},
                            {"role": "user", "content": json.dumps(context, ensure_ascii=False)}]}
    key = _key(task_id, lease_id)
    with store.transaction(conn):
        if native_worker is not None and native_worker.activity_lease(task_id) != lease_id:
            raise LeaseExpired("the native acquisition changed while preparing evidence")
        record = store.begin_idempotent(conn, key, SCOPE, {"profile": profile, "request": request})
        if not record.fresh:
            return record.response if record.replay else activity_status(conn, task_id, lease_id)
        current_task, current_run = _rows(conn, task_id)
        _check_lease(conn, current_task, current_run, lease_id)
        if current_task["attempt"] != context["attempt"]:
            raise LeaseExpired("assignment changed while preparing the activity")
        # A replacement lease cannot turn a lost/uncancelled remote request into a new experiment.
        history = conn.execute(
            "SELECT e.payload_json,i.completed_at,i.response_json FROM events e JOIN idempotency i"
            " ON i.idempotency_key=e.subject_id WHERE e.kind='model_activity.started'"
            " AND e.run_id=? AND json_extract(e.payload_json,'$.task_id')=?",
            (run["run_id"], task_id)).fetchall()
        if any(not old["completed_at"] or store.loads(old, "response_json", {}).get("status") in
               ("outcome_unknown", "cancelled_locally") for old in history):
            raise SchemaInvalid("an earlier activity for this task has unresolved remote execution")
        profile_sha = ids.sha256_of_obj(profile)
        # When an owner restarts with a replacement lease, the same scientific inputs
        # may already have a usable answer. The new attempt sees the original attribution.
        scientific_input = {field: value for field, value in context.items() if field != "attempt"}
        reuse_sha = ids.sha256_of_obj({"context": scientific_input, "profile": profile})
        for old in reversed(history):
            previous = store.loads(old, "response_json", {})
            if previous.get("status") == "completed" and store.loads(old, "payload_json", {}).get("reuse_sha256") == reuse_sha:
                receipt = {**previous, "operation_id": key, "reused_operation_id": previous["operation_id"]}
                store.complete_idempotent(conn, key, receipt)
                _event(conn, run["run_id"], key, "reused", {"previous": previous["operation_id"]})
                return receipt
        if _remaining_requests(conn, run["run_id"], profile_sha, profile["max_requests"]) <= 0:
            raise BudgetExceeded("run reached its pinned Model Activity request limit")
        if profile["provider"] == "deepseek" and not os.environ.get("DEEPSEEK_API_KEY"):
            raise SchemaInvalid("DEEPSEEK_API_KEY is required by this profile")
        context_ref = _put(ArtifactService(conn), task, "model_activity_context", context)
        runtime = {"capabilities": CAPABILITIES}
        if profile.get("runtime") == "dsh_sdk":
            runtime = {"capabilities": dsh_activity.CAPABILITIES, "session_id": dsh_activity.session_for(key)}
        _event(conn, run["run_id"], key, "started", {"profile": profile, "profile_sha256": profile_sha,
               "task_id": task_id, "lease_id": lease_id, "attempt": task["attempt"],
               "context_ref": context_ref, "reuse_sha256": reuse_sha, **runtime})
    args = (profile, request, task, run, lease_id, context, context_ref)
    if native_worker is None:
        return _execute(conn, *args)
    driver_id = _driver_id(conn, key)
    if not driver_id[0]:
        return _execute(conn, *args)

    def drive():
        try:
            with closing(store.connect(driver_id[0])) as worker_conn:
                _execute(worker_conn, *args)
        finally:
            _ACTIVE_DRIVERS.discard(driver_id)

    # A lost parent process leaves its persisted request intent for reconciliation.
    # Network I/O never monopolizes the native callback pump or its SQLite connection.
    _ACTIVE_DRIVERS.add(driver_id)
    threading.Thread(target=drive, name="gx-model-activity", daemon=True).start()
    return {"status": "pending", "operation_id": key, "context_ref": context_ref}


def _execute(conn, profile, request, task, run, lease_id, context, context_ref):
    task_id = task["task_id"]
    key = _key(task_id, lease_id)
    profile_sha = ids.sha256_of_obj(profile)
    answer = _invoke(conn, profile, request, task_id, lease_id, key)
    if answer.get("http_status") == 429:
        # A rate-limit rejection is a known refusal. Admit one new call through the
        # same request budget; a transport error or a lost ACK can never enter here.
        with store.transaction(conn):
            current_task, current_run = _rows(conn, task_id)
            allow_retry = _remaining_requests(conn, run["run_id"], profile_sha, profile["max_requests"]) > 0
            try:
                _check_lease(conn, current_task, current_run, lease_id)
            except LeaseExpired:
                allow_retry = False
            if conn.execute("SELECT 1 FROM events WHERE subject_id=? AND kind='model_activity.cancel_requested'",
                            (key,)).fetchone():
                allow_retry = False
            if allow_retry:
                _event(conn, run["run_id"], key, "retry_started", {"task_id": task_id, "profile_sha256": profile_sha,
                       "prior_http_status": 429, "request_index": 2})
        if allow_retry:
            answer = _invoke(conn, profile, request, task_id, lease_id, key)
    with store.transaction(conn):
        current_task, current_run = _rows(conn, task_id)
        try:
            _check_lease(conn, current_task, current_run, lease_id)
            eligible = not conn.execute("SELECT 1 FROM events WHERE subject_id=? AND kind='model_activity.cancel_requested'",
                                        (key,)).fetchone()
        except LeaseExpired:
            eligible = False
        receipt = _receipt(ArtifactService(conn), task, context, profile, key, answer, eligible)
        receipt["context_ref"] = context_ref
        if receipt["status"] == "outcome_unknown":
            store.mark_idempotent_unknown(conn, key, receipt.get("reason", "unconfirmed provider request"))
        else:
            store.complete_idempotent(conn, key, receipt)
        _event(conn, run["run_id"], key, receipt["status"], receipt)
    return receipt


def require_profile_product(conn, run, task, lease_id, entries):
    inputs = store.loads(run, "inputs_json", {})
    profiles = inputs.get("inputs", inputs).get("execution_profiles", {})
    kind = store.loads(task, "input_json", {}).get("output_kind")
    if kind not in profiles:
        return
    receipt = store.stored_response(conn, _key(task["task_id"], lease_id))
    product_refs = [e["artifact_ref"] for e in entries if e["kind"] == kind]
    if not (receipt and receipt["status"] == "completed" and receipt["profile"] == profiles[kind]
            and product_refs == [receipt["artifact_ref"]]):
        raise SchemaInvalid("finish must include the acquired task's completed configured model product")


def native_activity(worker, task_id):
    """Derive model inputs from the acquired task. The worker supplies only its task ID."""
    conn = worker.conn
    lease_id = worker.activity_lease(task_id)
    task, run = _rows(conn, task_id)
    inputs = store.loads(task, "input_json", {})
    if inputs.get("output_kind") not in OUTPUT_KINDS or not inputs.get("evidence_refs"):
        raise SchemaInvalid("the acquired task has no configured scientific evidence handoff")
    result = activity_status(conn, task_id, lease_id)
    if result["status"] == "not_started":
        refs = [inputs["proposal_ref"], *inputs["evidence_refs"]]
        assets = ArtifactService(conn)
        for evidence in inputs["evidence_refs"]:
            for route in assets.read_json(evidence)["routes"]:
                refs.extend(route[field] for field in ("input_ref", "observations_ref", "evaluation_ref")
                            if route.get(field))
        if inputs.get("assessment_ref"):
            refs.append(inputs["assessment_ref"])
        result = run_activity(conn, task_id=task_id, lease_id=lease_id,
                              output_kind=inputs["output_kind"], input_refs=refs,
                              instruction=inputs["instruction"], native_worker=worker)
    return {"run_id": run["run_id"], "task_id": task_id, "operation_id": result["operation_id"],
            "status": result["status"], "context_ref": result.get("context_ref"),
            "artifact_ref": result.get("artifact_ref"), "response_ref": result.get("response_ref")}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status", "cancel"))
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--task", required=True)
    parser.add_argument("--lease", required=True)
    parser.add_argument("--handoff", type=Path, help="JSON with output_kind, input_refs, instruction and optional acceptance")
    args = parser.parse_args(argv)
    if not args.db.is_file() or (args.command == "run" and not args.handoff):
        parser.error("supply an existing GX database and, for run, a handoff file")
    try:
        with closing(store.connect(args.db)) as conn:
            if args.command == "run":
                document = json.loads(args.handoff.read_text())
                if not isinstance(document, dict) or set(document) - {"output_kind", "input_refs", "instruction", "acceptance"}:
                    raise SchemaInvalid("invalid standalone activity handoff")
                result = run_activity(conn, task_id=args.task, lease_id=args.lease, **document)
            elif args.command == "cancel":
                result = cancel_activity(conn, args.task, args.lease)
            else:
                result = activity_status(conn, args.task, args.lease)
    except (GxError, OSError, ValueError, TypeError) as exc:
        parser.exit(2, f"{exc}\n")
    print(json.dumps(result, ensure_ascii=False))
    return int(args.command == "run" and result["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(main())
