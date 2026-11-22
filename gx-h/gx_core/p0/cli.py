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
from ..orchestration import packages, service
from ..persistence import store
from . import deployment, intent
from .driver import Driver, context, enqueue, project, snapshot


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
    if type(spec.get("max_rounds")) is not int or spec["max_rounds"] < 1:
        raise ValueError("Supply a positive max_rounds")
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
    workflow, version = packages.research_ref(spec, current)
    flow = service.default_registry(conn, workflow_id=workflow, version=version).get(workflow, version)
    service.seams.workflow_admission(conn, workflow_id=workflow, version=version,
                                    sha256=flow.digest(), sources_complete=flow.sources_complete())
    start_spec = flow.hooks.research_start(spec)
    action = initial_action(spec, start_spec)
    inputs = {**start_spec.inputs, "p0": True, "p0_spec": spec, "max_rounds": spec["max_rounds"]}
    files, roles = list(start_spec.files), dict(start_spec.roles)

    def bootstrap(rid):
        flow.hooks.bootstrap_intent(conn, rid, action["items"], roles)
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
                raise ValueError("Initial Intent is pending: " + str(row["rejected"] or mid))
        # The workflow reads the objective and prospective criterion. Individual tasks and Jobs
        # bind their own narrower inputs where they are created.
        intent.bind_dependencies(conn, rid, "workflow", rid, roles)
    outcome = service.run_start(context(conn), {"objective": spec["objective"], "project_ref": spec["project"],
                      "budget_profile": budget_profile, "workflow": {"id": workflow, "version": version},
                      "inputs": inputs, "input_files": files, "idempotency_key": workspace.start_key(spec["project"], run_key)},
                      prepare_p0=bootstrap, workspace_key=run_key)
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
    deadline = time.monotonic() + timeout
    failures = 0
    while time.monotonic() < deadline:
        driver = Driver(conn, directory, rid)
        try:
            result = driver.drive(deadline - time.monotonic())
            if result["status"] in machines.terminal_states("run"):
                from .workspace import release_finished
                release_finished(conn)
                result.update(snapshot(conn, rid))
            return result
        except (AppServerDisconnected, AppServerTimeout, OutcomeUnknown) as exc:
            failures += 1
            # GX effects are already durable. The new client inspects history and
            # existing task leases before sending anything with the same event key.
            with store.transaction(conn) as tx:
                store.append_event(tx, kind="p0.transport_recovery", run_id=rid, actor="gx-p0",
                                   subject_kind="run", subject_id=rid, reason=type(exc).__name__,
                                   evidence_level="EXECUTION_OBSERVED", payload={"attempt": failures})
        finally:
            driver.close()
        time.sleep(min(5, 0.2 * 2 ** min(failures, 5), max(0, deadline - time.monotonic())))
    result = snapshot(conn, rid)
    result["driver"] = "time_limit"
    return result


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
        if args.supplement:
            action, text = {"kind": "supplement", "text": args.supplement}, args.supplement
        else:
            action, text = {"kind": "text", "text": args.text}, args.text
        with closing(existing_db(directory, readonly=False)) as conn:
            rid = selected_run(conn)["run_id"]
            if args.text and args.message_id:
                saved = conn.execute("SELECT action_json FROM p0_operator_messages WHERE run_id=? AND message_id=?",
                                     (rid, args.message_id)).fetchone()
                legacy = {"kind": "proposal", "text": text}
                if saved and json.loads(saved[0]) == legacy:
                    # Repeating a pre-upgrade --text message keeps the original outcome.
                    action = legacy
            mid = enqueue(conn, rid, action, text, args.message_id)
        if args.queue_only:
            return {"message_id": mid, "status": "queued"}
    try:
        with native_state(directory), closing(store.open_store(directory / "gx.db")) as conn:
            if args.command == "start":
                spec = packages.example(args.example) if args.example else json.loads(args.spec.read_text())
                operator.authorize_project(operator.local_context(conn), spec["project"])
                return start(conn, directory, spec, run_key=args.run_key)
            if args.command == "run" and selected is None:
                authorize_workspace(conn)
                return coordinator.drive_workspace(conn, directory, args.timeout)
            run = selected_run(conn)
            if args.command == "resume" and run["status"] == "PAUSED":
                change_state(conn, run, "continue")
            if args.command != "message" and not store.latest_checkpoint(conn, run["run_id"]):
                start(conn, directory, store.loads(run, "inputs_json")["inputs"]["p0_spec"], run_id=run["run_id"])
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
            spec = packages.example(args.example) if args.example else json.loads(args.spec.read_text())
            with closing(existing_db(directory, readonly=False)) as conn:
                operator.authorize_project(operator.local_context(conn), spec["project"])
                return coordinator.queue_start(conn, args.run_key, spec)
        if args.command == "message" and "another native client" in str(exc):
            return {"message_id": mid, "status": "queued_for_active_driver"}
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    deployment.add_commands(commands)
    from ..plugins.cli import add_commands as add_plugin_commands
    add_plugin_commands(commands)
    from .operator import add_commands
    add_commands(commands)
    for name in ("start", "status", "run", "resume", "stop", "message", "team"):
        sub = commands.add_parser(name)
        sub.add_argument("--state", required=True, type=Path)
        if name != "start":
            sub.add_argument("--run-id", help="Select a run within this workspace")
        if name == "team":
            sub.add_argument("--team-id")
            sub.add_argument("--operation", choices=("status", "pause", "resume"), default="status")
        if name == "start":
            sub.add_argument("--run-key", help="Idempotent name for a workflow run sharing this workspace")
            selector = sub.add_mutually_exclusive_group(required=True)
            selector.add_argument("--spec", type=Path)
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
            sub.add_argument("--queue-only", action="store_true")
    args = parser.parse_args(argv)
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
