"""Supported entry for this versioned release; no alternate agent runtime."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, closing
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import uuid

from . import config, contracts
from .errors import GxError
from .native_release import NativeState, ReleaseStateError, native_state, write_native_bytes


COLLABORATION_MODES = ("auto", "module-leads")


def validate_native_execution(value, state: NativeState, *, prepare_workspace: bool = False) -> dict | None:
    """Validate internal launch configuration, never a model/tool argument.

    This trusted profile is an explicit local execution permission. Its working
    directory is a scope instruction, not an OS filesystem isolation boundary.
    Only the supported entry prepares an omitted default owned workspace;
    explicit paths and the router's recheck never create directories.
    """
    if value is None:
        return None
    if (not isinstance(value, dict) or set(value) not in ({"profile"}, {"profile", "workspace"})
            or value.get("profile") != "trusted_local_test"
            or "workspace" in value and not isinstance(value["workspace"], str)):
        raise ReleaseStateError("native_execution requires trusted_local_test and an optional owned workspace")
    workspace_text = value.get("workspace", str(state.directory / "workspace"))
    workspace = Path(workspace_text)
    root = contracts.HARNESS_ROOT.resolve()
    try:
        if (not workspace.is_absolute() or str(workspace) != workspace_text
                or state.directory.resolve(strict=True) != state.directory
                or workspace != state.directory / "workspace"
                or workspace == root or workspace in root.parents
                or any(path == workspace or path in workspace.parents
                       for path in (Path.home() / ".codex", Path.home() / ".config"))):
            raise ReleaseStateError("native execution workspace must be the canonical owned state/workspace")
        info = state.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ReleaseStateError("native execution state must be an owned regular directory")
        if prepare_workspace and "workspace" not in value:
            workspace.mkdir(mode=0o700, exist_ok=True)
        if workspace.resolve(strict=True) != workspace:
            raise ReleaseStateError("native execution workspace must be the canonical owned state/workspace")
        info = workspace.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
            raise ReleaseStateError("native execution workspace must be an owned regular directory")
    except OSError as exc:
        raise ReleaseStateError("native execution workspace must already exist without symlinks") from exc
    return {"profile": "trusted_local_test", "workspace": str(workspace)}


def launch_limits(launch_id: str | None, project: str, state: NativeState) -> dict:
    """Read reusable bounds; session ownership and recovery stay with their owners.

    Older deployments may still select a legacy run entry by its receipt label.
    Its project/state constraints remain meaningful, but approval prose and a
    pre-entered prompt hash are not prerequisites for ordinary continuation.
    """
    if launch_id is not None and re.fullmatch(r"[a-z0-9_-]{1,100}", launch_id) is None:
        raise ReleaseStateError("invalid launch id")
    config.check_holds("orchestration")
    switch = config.checked_switch("headless_host_sessions", scope="acceptance_and_adapter_verification")
    entry = switch.get("native_launch")
    if entry is None:
        entry = switch.get("runs", {}).get(launch_id)
    if not isinstance(entry, dict):
        raise ReleaseStateError("a bounded native_launch profile is not installed")
    if entry.get("source_mode") != "codex_native_events_v1":
        raise ReleaseStateError("launch native source does not match")
    if "project_ref" in entry and entry["project_ref"] != project:
        raise ReleaseStateError("launch project does not match")
    configured_state = entry.get("state_dir")
    if "state_dir" in entry and (not isinstance(configured_state, str) or not Path(configured_state).is_absolute()
            or Path(configured_state).resolve() != state.directory):
        raise ReleaseStateError("launch profile is pinned to a different state directory")
    for key in ("max_wall_seconds", "max_turns", "max_workers", "max_callbacks"):
        if type(entry.get(key)) is not int or entry[key] < 1:
            raise ReleaseStateError(f"launch needs a positive {key}")
    if type(entry.get("allow_mutations")) is not bool:
        raise ReleaseStateError("launch must explicitly declare whether GX mutations are permitted")
    if type(entry.get("capture_public_output", False)) is not bool:
        raise ReleaseStateError("capture_public_output must be a boolean when supplied")
    allowed = entry.get("allowed_tools")
    if allowed is not None and (not isinstance(allowed, list) or not allowed
            or any(not isinstance(tool, str) or not tool for tool in allowed)
            or len(set(allowed)) != len(allowed)):
        raise ReleaseStateError("launch allowed_tools must be a nonempty unique name list")
    from .hostobs.native_app_server import TOOL_PROFILES
    profile = entry.get("tool_profile", "native_core")
    if not isinstance(profile, str) or profile not in TOOL_PROFILES:
        raise ReleaseStateError("launch tool_profile must name an installed native business profile")
    bounds = config.limits()
    if entry["max_workers"] > min(bounds["host_pool"]["max_slots"] - 1,
                                  bounds["run_budget"]["max_parallel_workers"]):
        raise ReleaseStateError("launch workers exceed the installed ceiling")
    if any(key in entry and entry[key] != 1 for key in ("sessions", "new_host_cli_sessions")):
        raise ReleaseStateError("this entry admits exactly one native client session")
    return entry


def _record_launch(state: NativeState, launch_id: str, entry: dict, prompt: bytes, *, max_wall_seconds: int) -> Path:
    """Record this actual attempt without consuming its reusable configuration."""
    path = state.directory / f"launch-{launch_id}.json"
    receipt = {
        "schema": "gx.native_launch/1", "launch_id": launch_id,
        "at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "release_id": state.release_id, "contract_sha256": state.contract_sha256,
        "launch_profile_sha256": hashlib.sha256(json.dumps(entry, sort_keys=True).encode()).hexdigest(),
        "installed_max_wall_seconds": entry["max_wall_seconds"], "max_wall_seconds": max_wall_seconds,
        "prompt_sha256": hashlib.sha256(prompt).hexdigest(), "status": "STARTING",
    }
    try:
        with path.open("x", encoding="utf-8") as fh:
            json.dump(receipt, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    except FileExistsError as exc:
        raise ReleaseStateError("this launch receipt already exists; inspect its result or use a new receipt label") from exc
    return path


def _open_store(state: NativeState):
    # These hooks must be registered before opening the first connection after a restart.
    from .hostobs import projection  # noqa: F401 - registers projection journal recovery
    from .persistence import store

    return closing(store.open_store(state.db_path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run GX through the official Codex native connection")
    parser.add_argument("--state-dir", type=Path, help="new release-owned directory; never an old GX database")
    parser.add_argument("--project", required=True)
    parser.add_argument("--continue-project", action="store_true",
                        help="continue settled Root turns while GX has new actionable work")
    parser.add_argument("--run-id", help="limit project continuation to this run and its children")
    parser.add_argument("--collaboration", choices=COLLABORATION_MODES,
                        help="project collaboration for this launch (module-leads delegates through native team leaders)")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--tool-profile", choices=("native_core", "native_v2"),
                        help="worker tool catalog for a new session; resumes use the saved catalog")
    parser.add_argument("--launch-id", help="optional unique receipt label; generated when omitted")
    parser.add_argument("--max-wall-seconds", type=int, help="shorten this session's installed wall limit")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="check release/state metadata only; no Host or model")
    mode.add_argument("--init-store", action="store_true", help="initialize an owned empty store; no Host, model or run")
    mode.add_argument("--decide-approval", metavar="ID", help="decide one existing worker or model hold in your terminal")
    parser.add_argument("--decision", choices=("grant", "deny"), help="operator decision for --decide-approval")
    parser.add_argument("--note", help="reason for this operator decision")
    args = parser.parse_args(argv)
    try:
        if not args.project.strip() or args.project == "*":
            raise ReleaseStateError("one explicit project is required")
        if args.run_id is not None and not args.continue_project:
            raise ReleaseStateError("--run-id requires --continue-project")
        if args.collaboration is not None and not args.continue_project:
            raise ReleaseStateError("--collaboration requires --continue-project")
        if args.decide_approval is not None:
            if not (args.state_dir and (args.state_dir / "gx.db").is_file() and args.decision
                    and args.note and 0 < len(args.note.strip()) <= 1000):
                raise ReleaseStateError("--decide-approval requires an existing --state-dir, --decision and --note (1–1000 characters)")
            if args.continue_project or args.prompt_file or args.launch_id or args.max_wall_seconds is not None:
                raise ReleaseStateError("a terminal decision is a separate operation from a model launch")
        elif args.decision is not None or args.note is not None:
            raise ReleaseStateError("--decision and --note require --decide-approval")
        with native_state(args.state_dir) as state, ExitStack() as resources:
            if args.decide_approval is not None:
                from .governance.native_operator import decide_in_terminal
                from .hostobs.native_app_server import SESSION_FILE
                from .hostobs.session_receipts import read_json
                descriptor = state.directory / SESSION_FILE
                if descriptor.exists():
                    session = read_json(descriptor)
                    if (not isinstance(session, dict) or session.get("project_ref") != args.project
                            or session.get("cleanup_pending") is not False
                            or session.get("pending_effects") != []):
                        raise ReleaseStateError("the owned native session must reconcile its project, cleanup or pending effects first")
                with _open_store(state) as conn:
                    decision = {"grant": "GRANTED", "deny": "DENIED"}[args.decision]
                    result = decide_in_terminal(conn, args.project, args.decide_approval, decision, args.note.strip())
                print(json.dumps(result, ensure_ascii=False))
                return 0
            if args.check or args.init_store:
                if args.init_store:
                    with _open_store(state):
                        pass
                print(json.dumps({"status": "STATE_INITIALIZED_NOT_HOST_PROOF" if args.init_store else "STATE_CHECK_PASS_NOT_HOST_PROOF", "release_id": state.release_id,
                                  "contract_sha256": state.contract_sha256, "state_dir": str(state.directory),
                                  "host_started": False, "model_turns": 0}))
                return 0
            if not args.prompt_file:
                raise ReleaseStateError("execution requires --prompt-file")
            entry = launch_limits(args.launch_id, args.project, state)
            wall_limit = entry["max_wall_seconds"] if args.max_wall_seconds is None else args.max_wall_seconds
            if not 1 <= wall_limit <= entry["max_wall_seconds"]:
                raise ReleaseStateError("session wall limit must be positive and within the installed native profile")
            launch_id = args.launch_id or datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:12]
            with args.prompt_file.open("rb") as fh:
                prompt_bytes = fh.read(256 * 1024 + 1)
            if len(prompt_bytes) > 256 * 1024:
                raise ReleaseStateError("native prompt exceeds the bounded input size")
            prompt = prompt_bytes.decode("utf-8")
            if not prompt.strip():
                raise ReleaseStateError("native prompt is empty")
            native_execution = validate_native_execution(entry.get("native_execution"), state, prepare_workspace=True)
            conn = None
            if args.run_id is not None:
                conn = resources.enter_context(_open_store(state))
                if not conn.execute("SELECT 1 FROM runs WHERE run_id=? AND project_ref=?",
                                    (args.run_id, args.project)).fetchone():
                    raise ReleaseStateError("the selected run is absent from this project")
            from .hostobs.native_app_server import SESSION_FILE
            session_path = state.directory / SESSION_FILE
            tool_profile = args.tool_profile or entry.get("tool_profile", "native_core")
            if session_path.is_file():
                with session_path.open("rb") as descriptor:
                    saved = json.loads(descriptor.read(65537))
                pinned_profile = saved.get("tool_profile", "native_core")
                if args.tool_profile and args.tool_profile != pinned_profile:
                    raise ReleaseStateError("a resumed thread requires its original tool profile")
                tool_profile = pinned_profile
            entry = {**entry, "tool_profile": tool_profile}
            launch_receipt = _record_launch(state, launch_id, entry, prompt_bytes, max_wall_seconds=wall_limit)
            from .hostobs.runtime import runtime_adapter
            event_path = state.directory / f"events-{launch_id}.jsonl"
            tool_policy = {"max_callbacks": entry["max_callbacks"],
                           "allow_mutations": entry["allow_mutations"],
                           "tool_profile": entry.get("tool_profile", "native_core")}
            if entry.get("allowed_tools") is not None:
                tool_policy["allowed_tools"] = entry["allowed_tools"]
            output_path = state.directory / f"outputs-{launch_id}.jsonl"
            output_log, output_error = None, None
            capture_output = entry.get("capture_public_output", False)
            if capture_output:
                try:
                    fd = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                    try:
                        os.fchmod(fd, 0o600)
                        output_log = os.fdopen(fd, "w", encoding="utf-8")
                    except Exception:
                        os.close(fd)
                        raise
                except Exception:
                    output_error = "OUTPUT_OPEN_FAILED"

            def record_public_output(record):
                # Completed public display only; the collector already screened
                # the whole body. Never send this through the metadata sink.
                output_log.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                output_log.flush()

            last_turn_by_thread = {}
            recovery_observed = False
            with event_path.open("x", encoding="utf-8") as event_log:
                def record_event(event):
                    nonlocal recovery_observed
                    # The router emits metadata/call hashes, not conversation,
                    # credentials or arbitrary tool argument bodies.
                    event_log.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
                    event_log.flush()
                    os.fsync(event_log.fileno())
                    if event.get("kind") == "turn_completed" and event.get("thread_id"):
                        last_turn_by_thread[event["thread_id"]] = event.get("status")
                    elif event.get("kind") == "native_protocol_recovery":
                        recovery_observed = True

                try:
                    if conn is None:
                        conn = resources.enter_context(_open_store(state))
                    display = {"public_output_sink": record_public_output} if output_log is not None else {}
                    follow = ({"project_continuation": True, "project_run_id": args.run_id}
                              if args.continue_project else {})
                    if args.collaboration is not None:
                        follow["collaboration"] = args.collaboration
                    result = runtime_adapter().run_session(prompt, state=state, conn=conn, project_ref=args.project,
                                         max_wall_seconds=wall_limit, max_turns=entry["max_turns"],
                                         max_workers=entry["max_workers"], event_sink=record_event,
                                         tool_policy=tool_policy, native_execution=native_execution,
                                         **display, **follow)
                except Exception as exc:
                    result = {"status": "OUTCOME_UNKNOWN", "error_type": type(exc).__name__,
                              "error_code": getattr(exc, "code", None), "product_accepted": False,
                              "recovery": "Inspect this launch's protocol and product state before any new side effect."}
                finally:
                    if output_log is not None:
                        try:
                            output_log.close()
                        except Exception:
                            output_error = "OUTPUT_CLOSE_FAILED"
            if capture_output:
                summary = result.get("public_output")
                if not isinstance(summary, dict):
                    summary = result["public_output"] = {}
                for key, value in {"captured": 0, "withheld": 0, "truncated": 0,
                        "scanned_bytes": 0, "published_bytes": 0, "duplicates": 0, "ignored": 0,
                        "stopped": False, "error": None, "stop_reason": None}.items():
                    summary.setdefault(key, value)
                summary["enabled"] = True
                if output_log is not None:
                    summary["output_ref"] = str(output_path)
                if output_error:
                    summary.update(stopped=True, error=output_error, stop_reason=output_error)
            result["events_ref"] = str(event_path)
            result["root_turn_status"] = last_turn_by_thread.get(result.get("root_thread_id"))
            result["recovery_observed"] = recovery_observed
            result["closed_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
            # A protocol-session result is not a product or scientific PASS.
            write_native_bytes(state.directory / f"result-{launch_id}.json",
                               (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                               mode=stat.S_IMODE(launch_receipt.stat().st_mode))
            print(json.dumps({"launch_receipt": str(launch_receipt), **result}, ensure_ascii=False))
            return 0 if result.get("status") == "COMPLETED" else 1
    except (ReleaseStateError, GxError, OSError, UnicodeError, ValueError) as exc:
        print(json.dumps({"status": "REFUSED", "error": type(exc).__name__, "message": str(exc),
                          "product_accepted": False}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
