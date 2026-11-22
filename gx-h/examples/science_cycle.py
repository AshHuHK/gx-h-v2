"""Drive a synthetic research cycle through public GX callbacks in a dedicated offline store."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import time
import uuid

from gx_core.artifacts import ArtifactService
from gx_core.artifacts.delivery import prepare_manifest
from gx_core.context import Ctx, Principal
from gx_core.domain import machines
from gx_core.hostobs import host_policy
from gx_core.hostobs.app_server_client import ReceivedMessage
from gx_core.hostobs.native_app_server import NativeRouter
from gx_core.native_release import native_state
from gx_core.orchestration import reactor
from gx_core.persistence import store

from examples.p01_fixture import DATA

PROJECT = "fixture-science-001"


def read_inputs(args):
    source = json.loads((DATA / "project.json").read_text())
    hypotheses = {h["id"]: h for h in source["hypotheses"]}
    paths = []
    for route in source["paths"]:
        grid = json.loads((DATA / Path(route["input"]).name).read_text())
        grid["delay_seconds"] = 0.5
        if route["id"] == args.fail_route:
            grid["candidates"][0]["x"] = "malformed fixture input"
        paths.append(
            {"id": route["id"], "prediction": hypotheses[route["hypothesis_id"]]["prediction"], "grid": grid}
        )
    inputs = {
        "fixture_instance": args.instance,
        "max_rounds": getattr(args, "max_rounds", None) or 2,
        "proposal": {"paths": paths, "threshold": -0.25},
    }
    if args.evaluator:
        from gx_core.plugins.evaluator import installed_plugin_ref

        inputs["evaluator"] = installed_plugin_ref(args.evaluator)
    if args.execution_profiles:
        from gx_core.research.model_activity import validated_profiles

        inputs["execution_profiles"] = validated_profiles(json.loads(args.execution_profiles.read_text()))
    return source["objective"]["description"], inputs


def find_run(conn, instance):
    return conn.execute(
        "SELECT * FROM runs WHERE project_ref=? AND workflow_id='science_grid' "
        "AND json_extract(inputs_json,'$.inputs.fixture_instance')=?",
        (PROJECT, instance),
    ).fetchone()


def snapshot(conn, instance):
    run = find_run(conn, instance)
    if not run:
        return {"instance": instance, "status": "not_started"}
    run_id = run["run_id"]
    checkpoint = store.latest_checkpoint(conn, run_id)
    state = store.loads(checkpoint, "state_json", {}) if checkpoint else {}
    return {
        "instance": instance,
        "project": PROJECT,
        "mode": "offline_native_callbacks",
        "run_id": run_id,
        "status": run["status"],
        "round": state.get("round"),
        "workflow_sha256": run["workflow_sha256"],
        "node": checkpoint["node"] if checkpoint else None,
        "rounds": state.get("scratch", {}).get("history", []),
        "report_ref": state.get("outputs", {}).get("report", {}).get("artifact_ref"),
        "jobs": [
            dict(row)
            for row in conn.execute(
                "SELECT job_id,status,scheduler_ref FROM jobs WHERE run_id=? " "ORDER BY rowid", (run_id,)
            )
        ],
        "tasks": [
            dict(row)
            for row in conn.execute(
                "SELECT task_id,stage,status,attempt FROM tasks WHERE run_id=? " "ORDER BY rowid", (run_id,)
            )
        ],
    }


class OfflinePeer:
    """Distinct, explicitly scripted host callback identities for this disposable example."""

    def __init__(self, conn, directory, instance):
        self.conn = conn
        self.policy = host_policy.native_codex_policy(directory)[0]
        self.router = NativeRouter(
            conn,
            project_ref=PROJECT,
            max_turns=128,
            max_workers=2,
            codex_policy=self.policy,
            tool_policy={"tool_profile": "native_v2", "allow_mutations": True, "max_callbacks": 256},
        )
        self.authority = self.router.authority
        self.root = str(uuid.uuid5(uuid.NAMESPACE_URL, f"gx/offline/{directory}/{instance}"))
        self.register(self.root, None, self.policy["native_coordinator_observation"]["default_effort"])

    def register(self, thread, parent, effort):
        auth = self.authority
        if thread not in auth.threads:
            auth.register_thread(
                {
                    "id": thread,
                    "sessionId": self.root,
                    "parentThreadId": parent,
                    "forkedFromId": None,
                    "model": self.policy["pinned_model"]["id"],
                    "reasoningEffort": effort,
                    "cliVersion": auth.host_version,
                },
                root=parent is None,
                sequence=auth.sequence + 1,
            )
            auth.start_turn(thread, str(uuid.uuid4()), sequence=auth.sequence + 1)

    def callback(self, tool, args, thread=None):
        thread = thread or self.root
        auth = self.authority
        params = {
            "threadId": thread,
            "turnId": auth.active_turns[thread],
            "callId": str(uuid.uuid4()),
            "tool": tool,
            "arguments": args,
        }
        reply = self.router.on_server_request(
            ReceivedMessage(auth.sequence + 1, {"method": "item/tool/call", "params": params})
        )
        payload = json.loads(reply["contentItems"][0]["text"])
        if not payload["ok"]:
            raise RuntimeError(f"{tool}: {payload}")
        return payload["data"]

    def assign(self, task):
        conn, run_id = self.conn, task["run_id"]
        thread = str(uuid.uuid5(uuid.UUID(self.root), task["task_id"]))
        role_class = self.policy["roles"][task["role"]]["task_class"]
        self.register(thread, self.root, self.policy["task_classes"][role_class]["default_effort"])
        subject = self.authority.principal_for(thread).subject
        if conn.execute("SELECT 1 FROM task_assignments WHERE task_id=?", (task["task_id"],)).fetchone():
            return thread
        lease = store.coordinator_lease(conn, run_id)
        if not store.coordinator_lease_live(lease):
            revision = conn.execute("SELECT revision FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
            self.callback(
                "gx_coordinator_claim",
                {
                    "run_id": run_id,
                    "action": "claim",
                    "host": "codex",
                    "session_ref": self.root,
                    "expected_revision": revision,
                    "idempotency_key": str(uuid.uuid4()),
                },
            )
            lease = store.coordinator_lease(conn, run_id)

        def manage(action, payload):
            revision = conn.execute("SELECT revision FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
            return self.callback(
                "gx_team_manage",
                {
                    "run_id": run_id,
                    "action": action,
                    "payload": payload,
                    "expected_revision": revision,
                    "coordinator_lease_ref": lease["lease_id"],
                    "idempotency_key": str(uuid.uuid4()),
                },
            )

        team = conn.execute(
            "SELECT team_id,status FROM teams WHERE run_id=? ORDER BY rowid LIMIT 1", (run_id,)
        ).fetchone()
        team_id = (
            team["team_id"]
            if team
            else manage("create", {"intent": {"objective": "Compare frozen predictions and evidence."}})[
                "team"
            ]["team_id"]
        )
        if not team or team["status"] != "ACTIVE":
            manage("transition", {"team_id": team_id, "status": "ACTIVE"})
        if not conn.execute(
            "SELECT 1 FROM team_members WHERE team_id=? AND subject=?", (team_id, subject)
        ).fetchone():
            manage("member", {"team_id": team_id, "subject": subject, "action": "add"})
        manage("assign", {"team_id": team_id, "task_id": task["task_id"], "subject": subject})
        return thread

    def work(self, task, profiles):
        conn = self.conn
        thread = self.assign(task)
        task_id, run_id = task["task_id"], task["run_id"]
        acquired = self.callback("gx_task_acquire", {"task_id": task_id}, thread)
        if acquired.get("eligible") is False:
            raise RuntimeError(f"The original assignment for {task_id} is no longer eligible")
        assets = ArtifactService(conn)
        inputs = store.loads(task, "input_json")
        kind = inputs["output_kind"]
        if kind in profiles:
            result = self.callback("gx_model_activity", {"task_id": task_id}, thread)
            while result["status"] == "pending":
                time.sleep(0.25)
                result = self.callback("gx_model_activity", {"task_id": task_id}, thread)
            if result["status"] != "completed":
                raise RuntimeError(f"Model Activity for {task_id} returned {result['status']}")
            entry = {"artifact_ref": result["artifact_ref"]}
        else:
            from examples.science_judgment import judge

            doc = judge(assets, inputs)
            path = assets.inbox_dir(run_id) / f"{task_id}-{kind}.json"
            path.write_text(json.dumps(doc, allow_nan=False), encoding="utf-8")
            entry = {"inbox_path": path.name, "kind": kind, "media_type": "application/json"}
        manifest = prepare_manifest(
            assets,
            run_id=run_id,
            task_id=task_id,
            attempt=acquired["attempt"],
            entries=[entry],
            provenance={"source": "offline_fixture", "thread_id": thread},
        )
        return self.callback("gx_task_finish", {"task_id": task_id, "manifest_ref": manifest}, thread)


def execute(args):
    directory = args.state.resolve() / "offline_native"
    if args.action == "status":
        db = directory / "gx.db"
        if not db.is_file():
            return {"instance": args.instance, "status": "not_started"}
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return snapshot(conn, args.instance)

    with native_state(directory) as state, closing(store.open_store(state.db_path)) as conn:
        ctx = Ctx(
            conn=conn,
            principal=Principal(subject="scripted-grid-fixture", scope="operator"),
            tool="offline_fixture",
        )
        peer = OfflinePeer(conn, directory, args.instance)
        try:
            run = find_run(conn, args.instance)
            if not run:
                if args.action == "resume":
                    raise ValueError("No saved instance to resume")
                objective, inputs = read_inputs(args)
                peer.callback(
                    "gx_run_start",
                    {
                        "objective": objective,
                        "project_ref": PROJECT,
                        "budget_profile": "cpu_only",
                        "workflow": {"id": "science_grid", "version": "1"},
                        "inputs": inputs,
                        "idempotency_key": f"science-grid:{args.instance}",
                    },
                )
                run = find_run(conn, args.instance)
            elif args.fail_route or args.evaluator or args.execution_profiles or args.max_rounds is not None:
                _, requested = read_inputs(args)
                saved_inputs = store.loads(run, "inputs_json")["inputs"]
                options = (
                    ("proposal", args.fail_route),
                    ("max_rounds", args.max_rounds is not None),
                    ("evaluator", args.evaluator),
                    ("execution_profiles", args.execution_profiles),
                )
                for key, explicit in options:
                    if explicit and requested[key] != saved_inputs.get(key):
                        raise ValueError(f"{key} conflicts with this instance's frozen specification")
            if args.action == "start":
                return snapshot(conn, args.instance)

            run_id = run["run_id"]
            profiles = store.loads(run, "inputs_json")["inputs"].get("execution_profiles", {})
            deadline = time.monotonic() + args.timeout
            poll_delay = 0.1
            stop_states = {*machines.terminal_states("run"), "PAUSED"}
            while time.monotonic() < deadline:
                status = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
                if status in stop_states:
                    return snapshot(conn, args.instance)
                reactor.lazy(ctx, run_id)
                reactor.drive_science(ctx, run_id)
                task = conn.execute(
                    "SELECT * FROM tasks WHERE run_id=? AND stage IN ('assessment','decision') "
                    "AND status IN ('READY','CLAIMED','RUNNING') ORDER BY rowid LIMIT 1",
                    (run_id,),
                ).fetchone()
                if task:
                    peer.work(task, profiles)
                    poll_delay = 0.1
                else:
                    time.sleep(poll_delay)
                    poll_delay = min(2, poll_delay * 2)
            raise TimeoutError(
                f"{run_id} remains {snapshot(conn,args.instance)['status']}; resume this same instance"
            )
        finally:
            peer.authority.close()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["start", "status", "resume", "run"])
    p.add_argument(
        "--state", type=Path, required=True, help="Parent directory for the disposable offline_native store"
    )
    p.add_argument("--instance", default="main")
    p.add_argument("--max-rounds", type=int)
    p.add_argument("--fail-route", choices=["path-a", "path-b"])
    p.add_argument("--evaluator", choices=["1", "2"])
    p.add_argument("--execution-profiles", type=Path)
    p.add_argument("--timeout", type=float, default=60)
    print(json.dumps(execute(p.parse_args()), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
