"""Operator entry for the standalone GX + upstream Codex P0 release."""

import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import sys
import time
import uuid

from .. import config
from ..domain import ids, machines
from ..hostobs.app_server_client import AppServerDisconnected, AppServerTimeout
from ..errors import OutcomeUnknown
from ..native_release import native_state, ReleaseStateError
from ..orchestration import native_worker, packages, run_mode, service
from ..persistence import store
from . import intent, profiles_cli
from .driver import Driver, context, enqueue, native_waits, project, snapshot
from .idle import WorkspaceWait


class InitialIntentPending(AppServerDisconnected):
    def __init__(self, run_id, message_id):
        super().__init__("Initial Intent is waiting for its native receipt: " + message_id)
        self.run_id = run_id


def demo_spec():
    """Historical Python entry; public operator examples come from the installation."""
    return packages.python_example("demo_spec")


def geometry_spec():
    """Reproduce the existing Python example on its originally released workflow."""
    return packages.python_example("geometry_spec")


def initial_action(spec, start_spec=None):
    if start_spec is None:
        workflow_id, version = packages.research_ref(spec)
        start_spec = service.default_registry().get(workflow_id, version).hooks.research_start(spec)
    objective = {"item_id": "objective", "item_type": "objective", "expected_version": 0, "value": spec["objective"]}
    items = [objective, *start_spec.items]
    keyed = {item["item_id"]: item for item in items}
    for item in spec.get("intent_items", []):
        if item["item_id"] == "objective" and item["value"] != spec["objective"]:
            raise ValueError("Initial objective must match the project objective")
        if item["item_id"] in keyed and item["item_type"] != keyed[item["item_id"]]["item_type"]:
            raise ValueError("A predefined item must keep its item_type")
        if (item["item_id"] in keyed and item["item_id"] in start_spec.fixed_items
                and item["value"] != keyed[item["item_id"]]["value"]):
            raise ValueError(f"Initial item {item['item_id']!r} must agree with the project specification")
        keyed[item["item_id"]] = {**item, "expected_version": 0}
    return {"kind": "change", "items": list(keyed.values())}


def show(data):
    print(json.dumps(data, ensure_ascii=False, indent=2), flush=True)


def record_displayed_previews(args, output):
    from .intent_text import mark_displayed_together
    from .operator_budget import mark_displayed
    groups = []
    if args.command in ("message", "operator"):
        result = output.get("result")
        results = [(output.get("message_id"), result)]
        if isinstance(result, dict):
            results.extend((row["message_id"], row["result"]) for row in result.get("pending_previews", []))
    elif args.command == "conversation":
        return
    elif "runs" in output:
        groups = [[(p["message_id"], p["result"]) for p in run["pending_previews"]] for run in output["runs"]]
        results = []
    elif "pending_previews" in output:
        results = [(row["message_id"], row["result"]) for row in output["pending_previews"]]
    else:
        return
    groups.append(results)
    shown_groups = [[(mid, result) for mid, result in group if isinstance(result, dict)
                    and result.get("status") == "awaiting_confirmation"] for group in groups]
    if any(shown_groups):
        with closing(existing_db(args.state.expanduser().resolve(), readonly=False)) as conn:
            for shown in shown_groups:
                mark_displayed_together(conn, shown)
                mark_displayed(conn, shown)


def existing_db(directory, *, readonly):
    filename = directory / "gx.db"
    if not filename.is_file():
        raise ValueError("No GX P0 state in this directory")
    uri = filename.as_uri() + ("?mode=ro" if readonly else "?mode=rw")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='p0_operator_messages'").fetchone():
        conn.close()
        raise ValueError("State belongs to a different deployment")
    if not readonly:
        from .schema import install
        try:
            install(conn)
        except BaseException:
            conn.close()
            raise
    return conn


def start(conn, directory, spec, *, run_key=None, run_id=None):
    from . import workspace
    if not isinstance(spec.get("objective"), str) or not spec["objective"].strip():
        raise ValueError("Supply an objective")
    allocation = spec.get("turn_token_budget", 65536)
    if type(allocation) is not int or allocation < 1:
        raise ValueError("turn_token_budget must be a positive integer")
    budget_profile = spec.get("budget_profile", "p0")
    if not isinstance(budget_profile, str) or not budget_profile.strip():
        raise ValueError("budget_profile must name a configured budget profile")
    current = workspace.find_run(conn, run_key=run_key, run_id=run_id)
    if current:
        if store.loads(current, "inputs_json")["inputs"]["p0_spec"] != spec:
            raise ValueError("This state already belongs to a different project specification")
        run_key = current["run_key"]
    mode = run_mode.for_run(current) if current is not None else run_mode.for_start(spec)
    return mode.start_project(conn, directory, spec, run_key, current)


def start_workflow(conn, directory, spec, run_key, current):
    from . import workspace
    budget_profile = spec.get("budget_profile", "p0")
    workflow, version = packages.research_ref(spec, current)
    native_worker.for_workflow(workflow, version).check_start(spec)
    flow = service.default_registry(conn, workflow_id=workflow, version=version).get(workflow, version)
    service.seams.workflow_admission(conn, workflow_id=workflow, version=version,
                                    sha256=flow.digest(), sources_complete=flow.sources_complete())
    bound = None
    if "plugins" in spec and spec["plugins"] != []:
        from ..plugins import runtime
        from . import operator
        operator.authorize_project(operator.local_context(conn), spec["project"])
        if current is None:
            bound = runtime.for_start(context(conn), flow, spec["plugins"])
        elif (saved := runtime.recorded(current)) is not None:
            bound = runtime.for_saved(flow, saved)
    hooks = bound.hooks if bound else flow.hooks
    start_spec = hooks.research_start(spec)
    action = initial_action(spec, start_spec)
    inputs = {**start_spec.inputs, "p0": True, "p0_spec": spec, "max_rounds": spec["max_rounds"]}
    files, roles = list(start_spec.files), dict(start_spec.roles)

    def bootstrap(rid):
        hooks.bootstrap_intent(conn, rid, action["items"], roles)
        initial = workspace.bootstrap_action(conn, rid, action)
        mid = str(uuid.uuid5(uuid.NAMESPACE_URL, "gx-p0-bootstrap/" + rid))
        if run_key is not None:
            from . import operator
            if initial["items"]:
                result = operator.apply_bootstrap(conn, rid, initial, mid)
                if result.get("status") != "committed":
                    raise ValueError("Initial Intent is pending: " + mid)
        else:
            enqueue(conn, rid, initial, "Start this scientific project with the supplied initial Intent. "
                    "GX owns durable execution. " + ids.canonical_json(initial), mid)
            row = conn.execute("SELECT result_json,rejected FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
            if not row["result_json"]:
                driver = Driver(conn, directory, rid)
                try:
                    driver.messages()
                    driver.finish_main(mid)
                finally:
                    driver.close()
                row = conn.execute("SELECT result_json,rejected FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
            if not row["result_json"] or json.loads(row["result_json"]).get("status") != "committed":
                if not row["rejected"] and any(w["owner"] == "message" and w["reference"] == mid
                                               for w in native_waits(conn, rid)):
                    raise InitialIntentPending(rid, mid)
                raise ValueError("Initial Intent is pending: " + str(row["rejected"] or mid))
        # The workflow reads the objective and prospective criterion. Individual tasks and Jobs
        # bind their own narrower inputs where they are created.
        intent.bind_dependencies(conn, rid, "workflow", rid, roles)
    outcome = service.run_start(context(conn), {"objective": spec["objective"], "project_ref": spec["project"],
                      "budget_profile": budget_profile, "workflow": {"id": workflow, "version": version},
                      "inputs": inputs, "input_files": files, "idempotency_key": workspace.start_key(spec["project"], run_key)},
                      prepare_p0=bootstrap, workspace_key=run_key, plugin_binding=bound)
    return snapshot(conn, outcome["run_id"])


def change_state(conn, run, action):
    if run["status"] in machines.terminal_states("run") or (action == "pause") == (run["status"] == "PAUSED"):
        return
    if action == "continue":
        from .replan import prepare_replan
        prepare_replan(conn, run["run_id"])
    service.run_control(context(conn), {"run_id": run["run_id"], "action": action,
                        "reason": "operator requested " + action,
                        "idempotency_key": str(uuid.uuid4())})


def run_until_stopped(conn, directory, rid, timeout):
    with WorkspaceWait(conn) as listener:
        return _run_until_stopped(conn, directory, rid, timeout, listener)


def _run_until_stopped(conn, directory, rid, timeout, listener):
    deadline = time.monotonic() + timeout
    failures = 0
    while time.monotonic() < deadline:
        driver = None
        try:
            run = project(conn, rid)
            if run_mode.for_run(run).needs_bootstrap(conn, rid):
                start(conn, directory, store.loads(run, "inputs_json")["inputs"]["p0_spec"], run_id=rid)
            driver = Driver(conn, directory, rid)
            result = driver.drive(deadline - time.monotonic(), waiter=listener)
            if result["status"] in machines.terminal_states("run"):
                from .workspace import release_finished
                release_finished(conn)
                result.update(snapshot(conn, rid))
            return result
        except InitialIntentPending:
            # No checkpoint can run before this specific original human input is authorized.
            failures += 1
            bootstrap = True
        except (AppServerDisconnected, AppServerTimeout, OutcomeUnknown) as exc:
            failures += 1
            bootstrap = False
            # GX effects are already durable. The new client inspects history and
            # existing task leases before sending anything with the same event key.
            with store.transaction(conn) as tx:
                store.append_event(tx, kind="p0.transport_recovery", run_id=rid, actor="gx-p0",
                                   subject_kind="run", subject_id=rid, reason=type(exc).__name__,
                                   evidence_level="EXECUTION_OBSERVED", payload={"attempt": failures})
        finally:
            if driver:
                driver.close()
        retry = min(deadline, time.monotonic() + min(5, 0.2 * 2 ** min(failures, 5)))
        while time.monotonic() < retry:
            ready = listener.wait(retry - time.monotonic())
            if bootstrap and ready is None:
                break
    result = snapshot(conn, rid)
    result["driver"] = "time_limit"
    return result


def start_specification(args):
    return run_mode.specification(args)


def execute(args):
    from . import coordinator, operator, workspace
    directory = args.state.expanduser().resolve()
    selected = getattr(args, "run_id", None)

    def selected_run(conn):
        row = project(conn, selected)
        if row is None:
            raise ValueError("Start a project before using this command")
        operator.authorize_project(operator.local_context(conn), row["project_ref"])
        return row

    def authorize_workspace(conn):
        ctx = operator.local_context(conn)
        operator.authorize_project(ctx, None)
        for run in workspace.list_runs(conn):
            operator.authorize_project(ctx, run["project_ref"])

    if args.command == "research":
        from . import research
        return research.execute(args, directory)
    if args.command == "operator":
        with closing(existing_db(directory, readonly=False)) as conn:
            rid = selected_run(conn)["run_id"]
            output = operator.submit(operator.local_context(conn), rid, operator.cli_action(args), args.message_id)
        if args.output:
            from ..native_release import STATE_MARKER, write_native_bytes
            target = args.output.expanduser().resolve()
            if target.parent == directory and target.name in {"gx.db", "gx.db-wal", "gx.db-shm", ".native.lock", STATE_MARKER}:
                raise ValueError("Choose an audit output file distinct from the workspace database and its state files")
            write_native_bytes(target, (json.dumps(output["result"], indent=2, ensure_ascii=False) + "\n").encode())
            return {"message_id": output["message_id"], "run_id": rid, "audit_file": str(args.output)}
        return output
    if args.command == "capabilities":
        from . import capability_views
        with closing(existing_db(directory, readonly=False)) as conn:
            rid = selected_run(conn)["run_id"]
            if not (args.enable or args.disable):
                return capability_views.current(conn, rid)
            if args.revision is None or not args.request_id:
                raise ValueError("Supply --revision and --request-id for a capability change")
            return capability_views.change(conn, rid, enable=args.enable, disable=args.disable,
                provider=args.provider, expected_digest=args.sha256, expected_revision=args.revision,
                request_id=args.request_id, actor=operator.local_context(conn).actor)
    if args.command == "assign-material":
        from . import v621_assignment
        with closing(existing_db(directory, readonly=False)) as conn:
            run = selected_run(conn)
            item = dict(source=args.source, path=args.input, sha256=args.sha256, role=args.role,
                        metadata_path=args.acquisition, module_instance=args.module_instance, media_type=args.media_type)
            actor = operator.local_context(conn).actor
            if args.companion:
                companions = [json.loads(path.read_bytes()) for path in args.companion]
                receipts = v621_assignment.assign_group(conn, run["run_id"], after_decision=args.after_decision,
                                                        actor=actor, items=[item, *companions], comparison_id=args.comparison_id)
                return {"assignments": receipts}
            if args.comparison_id:
                raise ValueError("--comparison-id applies to a committed material group")
            return v621_assignment.assign(conn, run["run_id"], after_decision=args.after_decision, actor=actor, **item)
    if args.command == "observe":
        from .v62_outputs import ingest_measurement
        with closing(existing_db(directory, readonly=False)) as conn:
            run = selected_run(conn)
            return ingest_measurement(conn, run["run_id"], source=args.source, scope=args.scope,
                                      path=args.input, metadata_path=args.acquisition,
                                      artifact_ref=getattr(args, "artifact_ref", None),
                                      expected_sha256=getattr(args, "sha256", None),
                                      media_type=getattr(args, "media_type", "application/json"),
                                      actor=operator.local_context(conn).actor)
    if args.command == "team":
        from .team_admission import team_control, team_status
        with closing(existing_db(directory, readonly=args.operation == "status")) as conn:
            rid = selected_run(conn)["run_id"]
            if args.team_id:
                return team_control(conn, rid, args.team_id, args.operation)
            if args.operation != "status":
                raise ValueError("Specify --team-id for a team change")
            return {"run_id":rid, "teams":team_status(conn, rid)}
    if args.command == "status":
        if not (directory / "gx.db").exists():
            return {"status": "not_started"}
        with closing(existing_db(directory, readonly=True)) as conn:
            if selected:
                return snapshot(conn, selected_run(conn)["run_id"])
            authorize_workspace(conn)
            return coordinator.overview(conn)
    if args.command == "conversation":
        from . import conversation
        with closing(existing_db(directory, readonly=True)) as conn:
            return conversation.timeline(conn, selected_run(conn)["run_id"])
    if args.command == "stop":
        with closing(existing_db(directory, readonly=False)) as conn:
            target = selected_run(conn)
            change_state(conn, target, "pause")
            return snapshot(conn, target["run_id"])
    if args.command == "message":
        if args.action:
            action = json.loads(args.action.read_text())
            with closing(existing_db(directory, readonly=False)) as conn:
                rid = selected_run(conn)["run_id"]
                return operator.submit(operator.local_context(conn), rid, action, args.message_id, queue_only=args.queue_only)
        if args.text is not None:
            from . import conversation
            successor = None
            if args.successor or args.successor_config:
                successor = json.loads(args.successor_config.read_text()) if args.successor_config else {}
            attachments = [{"name":p.name, "text":p.read_text(encoding="utf-8")} for p in args.attachment]
            attachments += [{"artifact_ref":ref} for ref in args.material_ref]
            with closing(existing_db(directory, readonly=False)) as conn:
                rid = selected_run(conn)["run_id"]
                receipt = conversation.send(conn, rid, args.text, args.message_id, reply_to=args.reply_to,
                    branch_id=args.branch_id, question_id=args.question_id, answer_option=args.answer_option,
                    conversation_id=args.conversation_id,
                    goal_version=args.goal_version, attachments=attachments, successor=successor)
                if args.queue_only:
                    return receipt
                return {**receipt, **conversation.process(conn, directory, rid, receipt["message_id"])}
        with closing(existing_db(directory, readonly=False)) as conn:
            rid = selected_run(conn)["run_id"]
            mid = enqueue(conn, rid, {"kind":"supplement","text":args.supplement}, args.supplement, args.message_id)
        if args.queue_only:
            return {"message_id": mid, "status": "queued"}
    try:
        with native_state(directory), closing(store.open_store(directory / "gx.db")) as conn:
            if args.command == "start":
                spec = start_specification(args)
                operator.authorize_project(operator.local_context(conn), spec["project"])
                try:
                    return start(conn, directory, spec, run_key=args.run_key)
                except InitialIntentPending as exc:
                    return {**snapshot(conn, exc.run_id), "driver": "initial_intent_pending"}
            if args.command == "run" and selected is None:
                authorize_workspace(conn)
                return coordinator.drive_workspace(conn, directory, args.timeout)
            run = selected_run(conn)
            if args.command == "resume" and run["status"] == "PAUSED":
                change_state(conn, run, "continue")
            if args.command != "message":
                return run_until_stopped(conn, directory, run["run_id"], args.timeout)
            driver = Driver(conn, directory, run["run_id"])
            try:
                driver.messages()
                driver.finish_main(mid)
                driver.deliveries()
                row = conn.execute("SELECT result_json,rejected FROM p0_operator_messages WHERE message_id=?", (mid,)).fetchone()
                if row["rejected"]:
                    raise ValueError(row["rejected"])
                return {"message_id": mid, "result": json.loads(row["result_json"]) if row["result_json"] else "pending"}
            finally:
                driver.close()
    except ReleaseStateError as exc:
        if args.command == "start" and args.run_key and "another native client" in str(exc):
            spec = start_specification(args)
            with closing(existing_db(directory, readonly=False)) as conn:
                operator.authorize_project(operator.local_context(conn), spec["project"])
                return coordinator.queue_start(conn, args.run_key, spec)
        if args.command == "message" and "another native client" in str(exc):
            return {"message_id": mid, "status": "queued_for_active_driver"}
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, epilog=
        "Set GX_CODEX_OFFLINE=1 to drive saved work with Codex and App Server stopped.")
    commands = parser.add_subparsers(dest="command", required=True)
    from . import research
    research.add_command(commands)
    profiles_cli.add_command(commands)
    from ..plugins.cli import add_commands as add_plugin_commands
    add_plugin_commands(commands)
    from .operator import add_commands
    add_commands(commands)
    for name in ("start", "status", "run", "resume", "stop", "message", "conversation", "team", "observe", "assign-material", "capabilities"):
        sub = commands.add_parser(name)
        sub.add_argument("--state", required=True, type=Path)
        if name != "start":
            sub.add_argument("--run-id", help="Select a run within this workspace")
        if name == "capabilities":
            selection = sub.add_mutually_exclusive_group()
            selection.add_argument("--enable", metavar="METHOD@VERSION")
            selection.add_argument("--disable", metavar="METHOD@VERSION")
            sub.add_argument("--provider", help="Installed plugin id@version")
            sub.add_argument("--sha256", help="Expected portable method digest")
            sub.add_argument("--revision", type=int, help="Base revision returned by capabilities")
            sub.add_argument("--request-id", help="Stable identity for replaying the same selection")
        if name == "assign-material":
            sub.add_argument("--after-decision", required=True, help="Accepted Run Lead decision receiving this assignment")
            sub.add_argument("--source", required=True, help="Stable operator citation and idempotency key")
            sub.add_argument("--input", required=True, type=Path, help="Original local material")
            sub.add_argument("--sha256", required=True, help="Expected SHA-256 of the original bytes")
            sub.add_argument("--role", required=True, help="Scientific input role in the recipient grant")
            sub.add_argument("--acquisition", required=True, type=Path, help="Acquisition and source metadata JSON")
            sub.add_argument("--module-instance", help="Also grant this original to this open scoped delegation")
            sub.add_argument("--comparison-id", help="Bind the group to an active comparison from this Goal version")
            sub.add_argument("--companion", type=Path, action="append", help="Assign this companion descriptor in the same transaction")
            sub.add_argument("--media-type", default="application/octet-stream")
        if name == "observe":
            sub.add_argument("--source", required=True, help="Stable citation for this original measurement")
            sub.add_argument("--scope", default="run", help="Run or branch receiving this observation")
            material = sub.add_mutually_exclusive_group(required=True)
            material.add_argument("--input", type=Path, help="Original local measurement or raw mmCIF")
            material.add_argument("--artifact-ref", help="Register acquisition of the exact existing run artifact")
            sub.add_argument("--sha256", help="Expected original digest (required with --artifact-ref)")
            sub.add_argument("--media-type", choices=("application/json", "chemical/x-mmcif", "text/plain"),
                             default="application/json", help="Format of a newly supplied --input")
            sub.add_argument("--acquisition", type=Path, required=True, help="JSON acquisition and sample provenance")
        if name == "team":
            sub.add_argument("--team-id")
            sub.add_argument("--operation", choices=("status", "pause", "resume"), default="status")
        if name == "start":
            sub.add_argument("--run-key", help="Idempotent name for a workflow run sharing this workspace")
            selector = sub.add_mutually_exclusive_group(required=True)
            selector.add_argument("--spec", type=Path)
            selector.add_argument("--goal", help="Start from a human scientific question with no selected workflow")
            sub.add_argument("--historical-replay", action="store_true",
                             help="Reproduce an explicitly selected older Goal evidence policy")
            sub.add_argument("--goal-config", type=Path, help="JSON project, resource caps, capabilities and input files")
            sub.add_argument("--research-effort", choices=("quick", "medium", "deep"),
                             help="Select the research delegation policy for a Goal")
            sub.add_argument("--input", type=Path, action="append", help="Grant this immutable input to the Lead")
            sub.add_argument("--capability", action="append", help="Allow this installed method id@version")
            for alias, example in packages.installation()["examples"].items():
                if not example.get("private"):
                    selector.add_argument("--" + alias, dest="example", action="store_const", const=alias,
                                          help=example["help"])
        if name in ("run", "resume"):
            sub.add_argument("--timeout", type=float, default=math.inf, help="Optional wall time in seconds")
        if name == "message":
            selector = sub.add_mutually_exclusive_group(required=True)
            selector.add_argument("--text")
            selector.add_argument("--supplement")
            selector.add_argument("--action", type=Path)
            sub.add_argument("--message-id")
            sub.add_argument("--conversation-id")
            sub.add_argument("--goal-version", type=int)
            sub.add_argument("--reply-to", help="Prior PI message ID")
            sub.add_argument("--branch-id", help="Run-scoped route or module")
            sub.add_argument("--question-id", help="Refer to this research question in the conversation")
            sub.add_argument("--answer-option", help="Select an exact offered choice for --question-id")
            sub.add_argument("--attachment", action="append", type=Path, default=[], help="Attach a UTF-8 source file")
            sub.add_argument("--material-ref", action="append", default=[], help="Attach an authorized artifact")
            sub.add_argument("--successor", action="store_true", help="Start a follow-up to this sealed Run")
            sub.add_argument("--successor-config", type=Path, help="Follow-up materials and configuration JSON")
            sub.add_argument("--queue-only", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "research" and not args.run_id and not (args.goal or args.goal_file or args.task_card):
        parser.error("research --run-key requires --goal, --goal-file or --task-card")
    try:
        if getattr(args, "deployment_handler", None):
            return args.deployment_handler(args)
        if not getattr(args, "timeout", 1) > 0:
            parser.error("--timeout must be positive")
        result = execute(args)
        show(result)
        record_displayed_previews(args, result)
        return 75 if result.get("driver") == "time_limit" else 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
