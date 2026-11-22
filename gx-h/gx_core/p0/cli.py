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
from ..orchestration import service
from ..persistence import store
from . import deployment, intent
from .driver import Driver, context, enqueue, project, snapshot


def demo_spec():
    folder = config.HARNESS_ROOT / "examples" / "p01_data"
    source = json.loads((folder / "project.json").read_text())
    predictions = {h["id"]: h["prediction"] for h in source["hypotheses"]}
    paths = [{"id": route["id"], "prediction": predictions[route["hypothesis_id"]],
              "grid": json.loads((folder / Path(route["input"]).name).read_text())}
             for route in source["paths"]]
    return {"project": "p0-grid", "objective": source["objective"]["description"], "max_rounds": 3,
            "proposal": {"threshold": -0.25, "paths": paths}}


def initial_action(spec):
    items = [
        {"item_id": "objective", "item_type": "objective", "expected_version": 0, "value": spec["objective"]},
        {"item_id": "evaluation", "item_type": "evaluation_spec", "expected_version": 0,
         "value": {"threshold": spec["proposal"]["threshold"]}},
        {"item_id": "methods", "item_type": "method_ban", "expected_version": 0,
         "value": {"forbidden_capabilities": []}},
    ]
    keyed = {item["item_id"]: item for item in items}
    for item in spec.get("intent_items", []):
        if item["item_id"] == "objective" and item["value"] != spec["objective"]:
            raise ValueError("Initial objective must match the project objective")
        if item["item_id"] in keyed and item["item_type"] != keyed[item["item_id"]]["item_type"]:
            raise ValueError("A predefined item must keep its item_type")
        keyed[item["item_id"]] = {**item, "expected_version": 0}
    return {"kind": "change", "items": list(keyed.values())}


def show(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


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
    return conn


def start(conn, directory, spec):
    if type(spec.get("max_rounds")) is not int or spec["max_rounds"] < 1:
        raise ValueError("Supply a positive max_rounds")
    if not isinstance(spec.get("objective"), str) or not spec["objective"].strip():
        raise ValueError("Supply an objective")
    allocation = spec.get("turn_token_budget", 65536)
    if type(allocation) is not int or allocation < 1:
        raise ValueError("turn_token_budget must be a positive integer")
    action = initial_action(spec)
    current = project(conn)
    if current and store.loads(current, "inputs_json")["inputs"]["p0_spec"] != spec:
        raise ValueError("This state already belongs to a different project specification")
    inputs = {"p0": True, "p0_spec": spec, "proposal": spec["proposal"], "max_rounds": spec["max_rounds"]}
    def bootstrap(rid):
        mid = str(uuid.uuid5(uuid.NAMESPACE_URL, "gx-p0-bootstrap/" + rid))
        enqueue(conn, rid, action, "Start this scientific project with the supplied initial Intent. "
                "GX owns durable execution. " + ids.canonical_json(action), mid)
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
        intent.bind_dependencies(conn, rid, "workflow", rid, {"objective": "context", "evaluation": "evaluation"})
    service.run_start(context(conn), {"objective": spec["objective"], "project_ref": spec["project"],
                      "budget_profile": "p0", "workflow": {"id": "science_grid", "version": "1"},
                      "inputs": inputs, "idempotency_key": "p0-start:" + spec["project"]}, prepare_p0=bootstrap)
    return snapshot(conn)


def change_state(conn, run, action):
    if run["status"] in machines.terminal_states("run") or (action == "pause") == (run["status"] == "PAUSED"):
        return
    if action == "continue":
        from .replan import prepare_replan
        prepare_replan(conn, run["run_id"])
    lease = store.coordinator_lease(conn, run["run_id"])
    service.run_control(context(conn), {"run_id": run["run_id"], "action": action,
                        "reason": "operator requested " + action, "coordinator_lease_ref": lease["lease_id"],
                        "idempotency_key": str(uuid.uuid4())})


def run_until_stopped(conn, directory, rid, timeout):
    deadline = time.monotonic() + timeout
    failures = 0
    while time.monotonic() < deadline:
        driver = Driver(conn, directory, rid)
        try:
            return driver.drive(deadline - time.monotonic())
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
    result = snapshot(conn)
    result["driver"] = "time_limit"
    return result


def execute(args):
    directory = args.state.expanduser().resolve()
    if args.command == "status":
        if not (directory / "gx.db").exists():
            return {"status": "not_started"}
        with closing(existing_db(directory, readonly=True)) as conn:
            return snapshot(conn)
    if args.command == "stop":
        with closing(existing_db(directory, readonly=False)) as conn:
            change_state(conn, project(conn), "pause")
            return snapshot(conn)
    if args.command == "message":
        if args.action:
            action = json.loads(args.action.read_text())
            text = "My requested action is " + ids.canonical_json(action)
        elif args.supplement:
            action, text = {"kind": "supplement", "text": args.supplement}, args.supplement
        else:
            action, text = {"kind": "proposal", "text": args.text}, args.text
        with closing(existing_db(directory, readonly=False)) as conn:
            rid = project(conn)["run_id"]
            mid = enqueue(conn, rid, action, text, args.message_id)
        if args.queue_only:
            return {"message_id": mid, "status": "queued"}
    try:
        with native_state(directory), closing(store.open_store(directory / "gx.db")) as conn:
            if args.command == "start":
                spec = demo_spec() if args.demo_grid else json.loads(args.spec.read_text())
                return start(conn, directory, spec)
            run = project(conn)
            if not run:
                raise ValueError("Start a project before using this command")
            if args.command == "resume" and run["status"] == "PAUSED":
                change_state(conn, run, "continue")
            if args.command != "message" and not store.latest_checkpoint(conn, run["run_id"]):
                start(conn, directory, store.loads(run, "inputs_json")["inputs"]["p0_spec"])
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
        if args.command == "message" and "another native client" in str(exc):
            return {"message_id": mid, "status": "queued_for_active_driver"}
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    deployment.add_commands(commands)
    for name in ("start", "status", "run", "resume", "stop", "message"):
        sub = commands.add_parser(name)
        sub.add_argument("--state", required=True, type=Path)
        if name == "start":
            selector = sub.add_mutually_exclusive_group(required=True)
            selector.add_argument("--spec", type=Path)
            selector.add_argument("--demo-grid", action="store_true")
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
        return 75 if result.get("driver") == "time_limit" else 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
