"""Observe closures published by the native entry, including older event-only receipts."""
from datetime import datetime
import fcntl
import json
import os
import sqlite3
from pathlib import Path
import stat
from typing import Mapping

from ..domain import ids
from . import native_context as native


CALLBACK_LIMIT_MESSAGE = "the approved dynamic callback budget is exhausted; no further GX dispatch"
WORKER_CLOSE_EFFECT_MESSAGE = "worker closed with an unresolved business effect"


def _native_receipt_matches(event, previous, model):
    call = event.get("native_call")
    exact = {"root_thread_id": previous["root_thread_id"], "session_id": previous["session_id"],
             "host_version": previous["host_version"], "model": model, "model_authority": native.MODEL_AUTHORITY,
             "thread_id": event.get("thread_id"), "call_id": event.get("call_id"), "tool_name": event.get("tool"),
             "arguments_sha256": event.get("arguments_sha256"), "event_seq": event.get("event_seq"),
             "turn_id": event.get("turn_id"), "evidence_sha256": event.get("evidence_sha256")}
    return (isinstance(call, Mapping) and all(call.get(k) == v for k, v in exact.items())
            and ids.sha256_of_obj({k: v for k, v in call.items() if k != "evidence_sha256"}) == call.get("evidence_sha256"))


def _prior_resume_conflict(conn, previous, rejected, prior_resumes):
    from ..orchestration.service import proves_prior_resume_conflict

    call = rejected["native_call"]
    if (not isinstance(call.get("adapter_instance_id"), str) or not call["adapter_instance_id"]
            or rejected["thread_id"] != previous["root_thread_id"]):
        return False
    for owner in reversed(prior_resumes):
        if (owner["native_call"].get("adapter_instance_id") == call["adapter_instance_id"]
                and owner["thread_id"] == rejected["thread_id"] and owner["event_seq"] < rejected["event_seq"]
                and owner["call_id"] != rejected["call_id"] and owner["request_id"] != rejected["request_id"]
                and proves_prior_resume_conflict(conn, project_ref=previous["project_ref"],
                    rejected_run_id=rejected.get("run_id"), rejected_arguments_sha256=rejected["arguments_sha256"],
                    owner_run_id=owner["run_id"], owner_arguments_sha256=owner["arguments_sha256"])):
            return True
    return False


def _partial_start(item):
    return (isinstance(item, Mapping) and item.get("tool") == "gx_run_start"
            and item.get("side_effect_status") == "PARTIAL" and item.get("delivered") is True
            and not item.get("outcome_unknown"))


def _successful_native_receipt(event, previous, model):
    return (event.get("ok") is True and event.get("error_code") is None
            and event.get("side_effect_status", "APPLIED") == "APPLIED"
            and type(event.get("event_seq")) is int and event["event_seq"] > 0
            and all(isinstance(event.get(k), str) and event[k] for k in
                    ("run_id", "turn_id", "thread_id", "call_id", "request_id", "arguments_sha256"))
            and _native_receipt_matches(event, previous, model))


def _same_start_replayed(partial, replay):
    return (all(replay[key] == partial[key] for key in ("thread_id", "run_id", "arguments_sha256"))
            and replay["event_seq"] > partial["event_seq"] and replay["call_id"] != partial["call_id"]
            and replay["request_id"] != partial["request_id"]
            and replay["native_call"].get("adapter_instance_id") == partial["native_call"]["adapter_instance_id"])


FACADE_EVENTS = {"gx_task_acquire": "host.native_claim_verified",
                 "gx_task_finish": "submission.recorded"}


def _local_process_domain():
    return {"boot": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "pid_namespace": os.readlink("/proc/self/ns/pid")}


def host_process_group(transport):
    """Capture only the process group the owned stdio transport just created."""
    from .app_server_client import StdioTransport
    if isinstance(transport, StdioTransport):
        pid = transport.process.pid
        try:
            if os.getpgid(pid) == pid:
                return {"pgid": pid, **_local_process_domain()}
        except OSError:
            pass
    return None


def _host_group_gone(owner):
    if not isinstance(owner, dict) or type(owner.get("pgid")) is not int or owner["pgid"] <= 1:
        return False
    try:
        domain = _local_process_domain()
        if any(value != owner.get(key) for key, value in domain.items()):
            return False
        try:
            os.killpg(owner["pgid"], 0)
        except ProcessLookupError:
            return True
    except (OSError, OverflowError):
        pass
    return False


def committed_facades(directory, descriptor):
    """Resolve strict local transaction receipts, including an ACK lost before the journal write."""
    effects = descriptor.get("pending_effects")
    if not isinstance(effects, list) or not effects or len(effects) > 128:
        return {}
    candidates = [effect for effect in effects if isinstance(effect, Mapping)
                  and effect.get("tool") in FACADE_EVENTS and isinstance(effect.get("native_call_sha256"), str)]
    if not candidates:
        return {}
    receipts = {}
    conn = None
    try:
        conn = sqlite3.connect((directory / "gx.db").as_uri() + "?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        for effect in candidates:
            tool, digest = effect["tool"], effect["native_call_sha256"]
            rows = conn.execute(
                "SELECT e.run_id,e.payload_json,e.event_id FROM events e JOIN runs r ON r.run_id=e.run_id "
                "WHERE r.project_ref=? AND json_extract(r.host_json,'$.session_ref')=? AND e.kind=? "
                "AND e.subject_kind='task' AND e.subject_id=? "
                "AND json_extract(e.payload_json,'$.native_call.evidence_sha256')=? LIMIT 2",
                (descriptor["project_ref"], descriptor["session_id"], FACADE_EVENTS[tool],
                 effect["task_id"], digest)).fetchall()
            if len(rows) != 1:
                continue
            body = json.loads(rows[0]["payload_json"])
            call = body["native_call"]
            if (call["root_thread_id"] != descriptor["root_thread_id"] or call["tool_name"] != tool
                    or call["thread_id"] != effect["thread_id"] or call["call_id"] != effect["call_id"]
                    or call["arguments_sha256"] != effect["arguments_sha256"]):
                continue
            if tool == "gx_task_acquire":
                result = {"lease_ref": body["lease_id"], "attempt": body["attempt"]}
            else:
                result = {"submission_id": body["submission_id"], "lease_ref": body["lease_ref"],
                          "operation_id": body["operation_id"]}
            receipts[(effect["thread_id"], effect["call_id"])] = {
                "disposition": "COMMITTED_GX_TRANSACTION", "event_id": rows[0]["event_id"],
                "run_id": rows[0]["run_id"], "task_id": effect["task_id"], **result}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
        return {}
    finally:
        if conn is not None:
            conn.close()
    return receipts


def recoverable_facade_crash(directory, descriptor):
    if descriptor.get("cleanup_pending") is True and _host_group_gone(descriptor.get("host_process_group")):
        receipts = committed_facades(directory, descriptor)
        if receipts and len(receipts) == len(descriptor["pending_effects"]):
            return receipts
    return {}


def known_effect_receipts(directory: Path, previous: Mapping, model: str, *, completed_launch=None) -> dict[tuple[str, str], dict]:
    """Read only this source owner's exact prior GX completion receipts.

    A successful GX_call settles legacy Host-delivery ambiguity. The historical
    team and resume conflict envelopes also used UNKNOWN for rejected requests.
    These receipts never authorize a replay or settle a Job's outcome.
    """
    def legacy_conflict(item):
        return (item.get("tool") in ("gx_team_manage", "gx_run_resume") and item.get("outcome_unknown") is True
                and item.get("side_effect_status") == "UNKNOWN")

    effects = previous.get("pending_effects")
    if (not isinstance(effects, list) or len(effects) > 128 or
            any(not isinstance(previous.get(key), str) or not previous[key]
                for key in ("root_thread_id", "session_id", "host_version", "project_ref")) or
            any(not isinstance(item, Mapping) or
                any(not isinstance(item.get(key), str) or not item[key]
                    for key in ("thread_id", "call_id", "tool", "arguments_sha256"))
                for item in effects)):
        return {}
    pending = {(item["thread_id"], item["call_id"]): item for item in effects
        if (legacy_conflict(item) or _partial_start(item) or
            not item.get("outcome_unknown") and item.get("side_effect_status") not in ("UNKNOWN", "PARTIAL"))
        and isinstance(item.get("request_id"), str) and item["request_id"]}
    committed = committed_facades(directory, previous) if not completed_launch else {}
    if not pending:
        return committed
    matches = {key: {} for key in pending}
    has_resume = any(legacy_conflict(item) and item["tool"] == "gx_run_resume" for item in pending.values())
    start_inputs = {item["arguments_sha256"] for item in pending.values() if _partial_start(item)}
    conn = None
    try:
        if has_resume or start_inputs:
            conn = sqlite3.connect((directory / "gx.db").as_uri() + "?mode=ro", uri=True)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN")
        paths = []
        journals = ([directory / f"events-{completed_launch['launch_id']}.jsonl"] if completed_launch
                    else directory.glob("events-*.jsonl"))
        for path in journals:
            try:
                paths.append((path.lstat().st_mtime_ns, path))
            except OSError:
                continue
        scanned = 0
        for _, path in sorted(paths, reverse=True)[:128]:
            try:
                fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            except OSError:
                continue
            try:
                info = os.fstat(fd)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                        or info.st_size > 16 * 1024 * 1024 or scanned + info.st_size > 64 * 1024 * 1024):
                    continue
                scanned += info.st_size
                prior_resumes = []
                starts = []
                total_calls = 0
                with os.fdopen(fd, "rb", closefd=False) as stream:
                    for line_number, line in enumerate(stream, 1):
                        if len(line) > 64 * 1024:
                            continue
                        try:
                            event = json.loads(line)
                        except (ValueError, UnicodeError, RecursionError):
                            continue
                        if not isinstance(event, Mapping) or event.get("kind") != "GX_call":
                            continue
                        total_calls += 1
                        key = (event.get("thread_id"), event.get("call_id"))
                        if (completed_launch and event.get("side_effect_status") not in ("APPLIED", "NONE", "ROLLED_BACK")
                                and not (event.get("side_effect_status") == "PARTIAL" and key in pending)):
                            return {}
                        replay = event.get("tool") == "gx_run_start" and event.get("arguments_sha256") in start_inputs
                        if ((replay or has_resume and event.get("tool") == "gx_run_resume")
                                and _successful_native_receipt(event, previous, model)):
                            if replay:
                                for partial, receipt in starts:
                                    if _same_start_replayed(partial, event):
                                        receipt.update(valid=True, replay_request_id=event["request_id"])
                            else:
                                prior_resumes.append(event)
                        item = pending.get(key)
                        if item is None:
                            continue
                        invalid_key = (path.name, line_number)
                        conflict = legacy_conflict(item)
                        if any(event.get(name) != item.get(name) for name in ("tool", "arguments_sha256", "request_id")):
                            if conflict or _partial_start(item):
                                matches[key][invalid_key] = {"valid": False}
                            continue
                        matches[key][invalid_key] = {"valid": False}
                        call = event.get("native_call")
                        valid = _native_receipt_matches(event, previous, model)
                        pending_start = _partial_start(item)
                        if pending_start:
                            from ..orchestration.service import proves_reconciled_start
                            valid = (valid and event.get("ok") is False and event.get("error_code") == "RESOURCE_DENIED"
                                     and event.get("side_effect_status") == "PARTIAL"
                                     and event["thread_id"] == previous["root_thread_id"]
                                     and type(event.get("event_seq")) is int and event["event_seq"] > 0
                                     and isinstance(call.get("adapter_instance_id"), str) and bool(call["adapter_instance_id"])
                                     and proves_reconciled_start(conn, project_ref=previous["project_ref"],
                                                                 run_id=event.get("run_id"), native_call=call))
                            disposition = "PARTIAL_START_DEFERRED_JOB_STEP_SETTLED"
                        elif conflict:
                            valid = (valid and event.get("ok") is False and event.get("error_code") == "IDEMPOTENCY_CONFLICT"
                                and event.get("side_effect_status") == "UNKNOWN"
                                and isinstance(event.get("turn_id"), str) and bool(event["turn_id"])
                                and type(event.get("event_seq")) is int and event["event_seq"] > 0)
                            if item["tool"] == "gx_run_resume":
                                valid = valid and _prior_resume_conflict(conn, previous, event, prior_resumes)
                                disposition = "RUN_RESUME_CONFLICT_NO_NEW_CHANGE"
                            else:
                                # TeamManager rolls back a rejected transaction.
                                disposition = "TEAM_MANAGEMENT_CONFLICT_NO_NEW_CHANGE"
                        else:
                            valid = (valid and event.get("ok") is True and event.get("error_code") is None
                                and event.get("side_effect_status", "APPLIED") == "APPLIED")
                            disposition = "KNOWN_GX_RESULT_HOST_DELIVERY_UNCONFIRMED"
                        signature = ids.sha256_of_obj({"native_call": call, "request_id": event["request_id"],
                            "run_id": event.get("run_id"),
                            "ok": event.get("ok"), "error_code": event.get("error_code"),
                            "side_effect_status": event.get("side_effect_status", "APPLIED"),
                            "turn_id": event.get("turn_id"), "event_seq": event.get("event_seq"),
                            "evidence_sha256": event.get("evidence_sha256")})
                        matches[key].pop(invalid_key)
                        if signature not in matches[key] or valid and not matches[key][signature]["valid"]:
                            matches[key][signature] = {"valid": valid, "disposition": disposition, "journal": path.name,
                                "line": line_number, "request_id": event["request_id"]}
                            if pending_start and valid:
                                receipt = matches[key][signature]
                                receipt.update(valid=False, run_id=event["run_id"])
                                starts.append((event, receipt))
                if completed_launch and total_calls != completed_launch.get("GX_calls"):
                    return {}
            except (OSError, ValueError, TypeError, RecursionError):
                if completed_launch:
                    return {}
                continue
            finally:
                os.close(fd)
        settled = {key: next(iter(rows.values())) for key, rows in matches.items() if len(rows) == 1}
        journal = {key: {k: v for k, v in receipt.items() if k != "valid"}
                   for key, receipt in settled.items() if receipt["valid"]}
        return {**committed, **journal}
    except (OSError, sqlite3.Error, ValueError, TypeError, RecursionError):
        # Missing/malformed/ambiguous receipts preserve the existing barrier.
        return {}
    finally:
        if conn is not None:
            conn.close()


def _callback_refusal(failure, methods, message):
    """Recognize an exact refusal by the native callback handler."""
    if not failure or failure.get("type") != "AppServerCallbackError":
        return False
    chain = (failure.get("diagnostic") or {}).get("chain") or []
    if len(chain) != 2:
        return False
    callback = chain[0].get("callback") or {}
    cause = chain[1]
    return (callback.get("stage") == "handler" and (callback.get("method") or {}).get("message") in methods
            and cause.get("type") == "ScopeDenied"
            and (cause.get("message") or {}).get("message") == f"SCOPE_DENIED: {message}")


def read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _recoverable_frame_read(result, events_path):
    failure = result.get("failure") or {}
    chain = (failure.get("diagnostic") or {}).get("chain") or []
    typed = (failure.get("type") == "AppServerFrameTooLarge"
             and failure.get("failure_code") == "INBOUND_FRAME_LIMIT_BEFORE_DISPATCH")
    if not typed and (failure.get("type") != "AppServerProtocolError" or not chain or
                      (chain[0].get("message") or {}).get("message") != "App Server message exceeded bound"):
        return False
    pending = result.get("pending_requests")
    if typed and failure.get("pending_requests") != pending:
        return False
    if pending == []:
        if typed:
            return True
        # Older receipts identify this pre-dispatch read by its captured stack.
        if len(chain) != 1 or chain[0].get("type") != "AppServerProtocolError":
            return False
        locations = [(row.get("file"), row.get("function")) for row in chain[0].get("locations") or []]
        client = "gx_core/hostobs/app_server_client.py"
        read = [("gx_core/hostobs/native_app_server.py", "wait_for_turn"), (client, "run_until"),
                (client, "_pump"), (client, "_receive"), (client, "read_line")]
        return locations[-len(read):] == read or locations[-6:] == [*read, (client, "_io")]
    if not isinstance(pending, list) or len(pending) != 1 or type(pending[0]) is not int or not events_path.is_file():
        return False
    target = None
    with events_path.open(encoding="utf-8") as stream:
        for line in stream:
            event = json.loads(line)
            if event.get("request_id") != pending[0]:
                continue
            if event.get("kind") == "metadata_materialization_requested":
                target = event.get("thread_id")
            elif event.get("kind") == "metadata_lookup_refused":
                return bool(target and event.get("thread_id") == target and
                            event.get("failure_type") == failure["type"])
    return False


def native_sessions(state, *, latest_events_only=False, at=None):
    """Summarize launches in order, or just the launch covering a staged publication."""
    launches = []
    for path in state.glob("launch-*.json"):
        receipt = read_json(path)
        label = receipt["launch_id"]
        if path.name != f"launch-{label}.json":
            raise ValueError("native launch label does not match its filename")
        started = datetime.fromisoformat(receipt["at"])
        if at is None or started <= at:
            launches.append((started, receipt))
    launches.sort(key=lambda item: item[0])
    # Each state directory has one exclusive native session. An open launch at publication
    # time cannot inherit a previous launch's completed wait or interruption.
    if at is not None:
        launches = launches[-1:]
    sessions = []
    for index, (_, receipt) in enumerate(launches):
        label = receipt["launch_id"]
        result_path = state / f"result-{label}.json"
        if at is not None and not result_path.is_file():
            continue
        result = read_json(result_path) if result_path.is_file() else {}
        session = {k: result.get(k) for k in ("status", "resumed", "root_thread_id", "session_id",
                   "elapsed_seconds", "turns", "workers", "GX_calls", "pending_requests", "recovery_only",
                   "unresolved_previous_effects", "cleanup_failure", "project_continuation", "closed_at")}
        session.update(launch_id=label, started_at=receipt["at"], max_wall_seconds=receipt.get("max_wall_seconds"),
                       result_ref=str(result_path),
                       failure_type=(result.get("failure") or {}).get("type") or result.get("error_type"),
                       callback_limit_before_dispatch=_callback_refusal(result.get("failure"), ("item/tool/call",), CALLBACK_LIMIT_MESSAGE),
                       worker_closed_with_effect=_callback_refusal(result.get("failure"), ("thread/status/changed", "thread/closed"), WORKER_CLOSE_EFFECT_MESSAGE),
                       root_turn_status=None, recovery_observed=False)
        events_path = state / f"events-{label}.jsonl"
        if at is None and index == len(launches) - 1 and result.get("status") == "OUTCOME_UNKNOWN":
            session["recoverable_frame_read"] = _recoverable_frame_read(result, events_path)
        if "root_turn_status" in result and "recovery_observed" in result:
            turn, recovered = result["root_turn_status"], result["recovery_observed"]
            if (turn is not None and not isinstance(turn, str)) or type(recovered) is not bool:
                raise ValueError(f"invalid native closure summary for {label}")
            session.update(root_turn_status=turn, recovery_observed=recovered)
        elif result and latest_events_only and index < len(launches) - 1:
            # The project driver still checks historical Root identity and counts this launch.
            session["recovery_observed"] = None
        elif result or events_path.is_file():
            with events_path.open(encoding="utf-8") as events:
                for line in events:
                    if not result and not line.endswith("\n"):  # an active append
                        break
                    event = json.loads(line)
                    if not result and event.get("kind") == "root_registered":
                        session.update(root_thread_id=event.get("thread_id"), session_id=event.get("session_id"))
                        if latest_events_only:
                            break
                    if event.get("kind") == "turn_completed" and event.get("thread_id") == session["root_thread_id"]:
                        session["root_turn_status"] = event.get("status")
                    if event.get("kind") == "native_protocol_recovery":
                        session["recovery_observed"] = True
        sessions.append(session)
    return sessions


def closure_obstacle(sessions, descriptor, project, *, state=None, model=None, settled_effects=None):
    if not sessions:
        return "session descriptor has no native launch receipt" if descriptor is not None else None
    last = sessions[-1]
    if not descriptor or descriptor.get("project_ref") != project:
        return "native session descriptor missing or for another project"
    root = descriptor.get("root_thread_id")
    if (not root or descriptor.get("session_id") != root
            or any(s["root_thread_id"] != root or s["session_id"] != root for s in sessions)):
        return "native Root/session identity is missing or changed"
    if not last["status"]:
        receipts = recoverable_facade_crash(state, descriptor) if state is not None else {}
        if receipts:
            if settled_effects is not None:
                settled_effects.extend(receipts.values())
            return None
        return "native launch is in progress or closed without a result"
    if len(sessions) > 1 and last["resumed"] is not True:
        return "latest native session did not resume its owned Root"
    if (descriptor.get("cleanup_pending") is not False
            or last["cleanup_failure"] is not None or last["recovery_only"] is not False
            or last["unresolved_previous_effects"] != []):
        return "native owner has unconfirmed cleanup or unresolved effects"
    if last["root_turn_status"] in ("interrupted", "failed", "cancelled"):
        return f"official Root turn ended {last['root_turn_status']}"
    effects = descriptor.get("pending_effects")
    if effects != []:
        # Resume only when the existing native owner can reconcile every write
        # from retained receipts. It settles them after admitting the same Root.
        if (last["status"] == "OUTCOME_UNKNOWN"
                and descriptor.get("uncertain") is True and last["pending_requests"] == []
                and state is not None and model and isinstance(effects, list) and effects):
            if last.get("worker_closed_with_effect") and len(known_effect_receipts(state, descriptor, model)) == len(effects):
                return None
            if last["root_turn_status"] == "completed" and not last["failure_type"] and all(_partial_start(row) for row in effects):
                receipts = known_effect_receipts(state, descriptor, model, completed_launch=last)
                if len(receipts) == len(effects):
                    if settled_effects is not None:
                        settled_effects.extend(receipts.values())
                    return None
        return "native owner has unconfirmed cleanup or unresolved effects"
    handoff = last.get("project_continuation") or {}
    inbox_before_turn = (handoff.get("reason") == "INBOX_UNAVAILABLE" and handoff.get("root_turns") == 0
                         and last.get("turns") == 0 and last["root_turn_status"] is None)
    if (last["status"] == "COMPLETED" and not last["failure_type"] and last["pending_requests"] == []
            and descriptor.get("uncertain") is False and (last["root_turn_status"] == "completed" or inbox_before_turn)):
        return None
    if (last["status"] == "OUTCOME_UNKNOWN" and descriptor.get("uncertain") is True
            and (last["failure_type"] in ("AppServerDisconnected", "AppServerTimeout")
                 or last.get("callback_limit_before_dispatch") and last["pending_requests"] == []
                 or last.get("recoverable_frame_read"))):
        # Outstanding Host protocol request IDs can include reads. Only the native
        # owner can resume this Root and verify its current model/effort and effects.
        return None
    return "native closure requires inspection by its owner"


def native_active(state):
    path = state / ".native.lock"
    if not path.is_file():
        return False
    with path.open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False
