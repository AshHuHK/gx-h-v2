"""Public Goal admission and a controlled local transport for module interleaving experiments.

GX owns task claim, frozen briefs, leases, native dispatch, Jobs, artifacts and accounting.
Only the acknowledgment of a scripted local session is supplied at the native transport seam.
"""
from __future__ import annotations

from contextlib import ExitStack, closing
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

PRODUCT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PRODUCT / "gx-h"))

from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import GxError
from gx_core.native_release import native_state
from gx_core.p0 import v6_costs, v6_decisions, v6_goal, v6_jobs, v6_native
from gx_core.p0.roots import Roots
from gx_core.persistence import store

QUALITY = "tl1a-coordinate-quality@1"
GEOMETRY = "deposited-chain-geometry@1"
GOAL = "Inventory the supplied public deposited-coordinate records and report their measured coverage."


def save(path, body):
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def check(condition, message):
    if not condition:
        raise AssertionError(message)


@dataclass(frozen=True)
class Actor:
    task_id: str
    lease_id: str
    scope: str
    thread: str
    turn: str


def draft(*ops, evidence=(), watches=None):
    return {"rationale": "Record the next authorized work for the supplied inputs.",
            "evidence_refs": list(evidence), "ops": list(ops), "mode": "amend" if ops else "resolve",
            "reconsider_when": watches or [{"event": "evidence_added", "subject_ref": "run"}]}


def branch(alias):
    return {"op": "open_branch", "branch": "local:" + alias,
            "hypothesis": "The two source files may differ in recorded coverage.",
            "alternative": "Coverage may agree across the files.", "method_family": "public inventory",
            "discriminating_observation": "Read the source manifest and coordinate counts.",
            "budget_limits": {"job_submissions": 1, "cpu_seconds": 10},
            "stop_condition": "Stop when the measurement is available."}


class Scenario:
    def __init__(self, directory, *, spec_updates=None, reopen=False):
        self.directory = Path(directory).resolve()
        self.state = self.directory / "state"
        self.spec_updates = spec_updates or {}
        self.reopen = reopen
        self.actions = []
        self.actors = {}
        self.lead = None
        self.stack = ExitStack()

    def __enter__(self):
        if self.directory.is_relative_to(PRODUCT):
            raise ValueError("Use a new private work directory outside the product.")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            if not self.reopen:
                self._start_goal()
            self.stack.enter_context(native_state(self.state))
            self.conn = self.stack.enter_context(closing(store.open_store(self.state / "gx.db")))
            self.run_id, = self.conn.execute("SELECT run_id FROM v6_goals").fetchone()
            info = self.conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (self.run_id,)).fetchone()
            self.materials = [f["artifact_ref"] for f in json.loads(info[0])["files"]]
            self.stack.enter_context(patch("gx_core.p0.roots.tempfile.gettempdir", return_value=str(self.directory)))
            self.roots = Roots(self.conn, run_id=self.run_id, cwd=self.state, token_budget=10)
            self.roots.client = SimpleNamespace(transport=SimpleNamespace(process=SimpleNamespace(pid=os.getpid())))
            if not self.reopen:
                self.lead = self.next_actor("run")
            return self
        except BaseException:
            self.stack.close()
            raise

    def _start_goal(self):
        if self.state.exists():
            raise ValueError(f"Refusing to reuse existing experiment state: {self.state}")
        public = PRODUCT / "examples/v62a-data-inventory/grant.json"
        spec = json.loads(public.read_bytes())
        for item in spec["input_files"]:
            item["path"] = str((public.parent / item["path"]).resolve())
        marker = self.directory / "public-scope-marker.json"
        save(marker, {"marker": "PUBLIC-SCOPE-MARKER-62A"})
        spec["input_files"].append({"path": str(marker), "role": "material"})
        limits = {"job_submissions": 20, "worker_tasks": 48, "cpu_seconds": 1200,
                  "api_calls": 48, "tokens": 120000, "gpu_jobs": 0, "gpu_minutes": 0, "wall_hours": 2}
        spec.update(run_budget=limits, workspace_budget=limits, turn_token_budget=1000)
        spec.update(self.spec_updates)
        cfg = self.directory / "grant.json"
        save(cfg, spec)
        argv = [sys.executable, "-B", str(PRODUCT / "bin/gx-p0"), "start", "--state", str(self.state),
                "--run-key", "v62a-local-admission", "--goal", GOAL, "--goal-config", str(cfg)]
        outcome = subprocess.run(argv, cwd=self.directory, capture_output=True, text=True,
                                 env={**os.environ, "GX_CODEX_OFFLINE": "1"})
        save(self.directory / "start.json", {"argv": argv, "exit": outcome.returncode,
                                            "stdout": outcome.stdout, "stderr": outcome.stderr})
        if outcome.returncode:
            raise RuntimeError(f"Public start failed: {outcome.stderr} {outcome.stdout}")

    def __exit__(self, *args):
        self.stack.close()

    def rows(self, sql, *params):
        return [dict(r) for r in self.conn.execute(sql, params)]

    def head(self):
        return v6_decisions.head(self.conn, self.run_id)

    def advance(self):
        from gx_core.p0 import v62_modules
        v6_jobs.advance(self.conn, self.run_id)
        v62_modules.advance(self.conn, self.run_id)
        v6_goal.ensure_task(self.conn, self.run_id)

    def bind(self, task_id, *, metered=False):
        row = self.roots._claim(task_id)
        check(row is not None, f"Task could not be claimed: {task_id}")
        thread = row["thread_id"] or self.roots._bind(row, {
            "id": "script-" + row["lease_id"], "cwd": row["cwd"], "source": "appServer"})
        turn = "turn-" + row["lease_id"]
        self.roots.active_turns[thread] = turn
        exists = self.conn.execute("SELECT 1 FROM p0_root_deliveries WHERE thread_id=? AND turn_id=?",
                                   (thread, turn)).fetchone()
        if not exists and metered:
            receipt, fresh = self.roots.delivery._prepare(thread, "Inspect the assigned material", turn,
                                                         False, False, "scripted-meter:" + task_id, None)
            check(fresh, "Expected a fresh metered local turn")
            with store.transaction(self.conn, transaction_owner="V62AInterleavingFixture", operation="ack_metered_turn"):
                self.roots.delivery._ack(turn, turn)
        elif not exists:
            with store.transaction(self.conn, transaction_owner="V62AInterleavingFixture", operation="ack_scripted_turn"):
                self.conn.execute("""INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,
                    human,interrupt,state,method,turn_id) VALUES(?,?,?,?,?,0,0,'acknowledged','turn/start',?)""",
                                  (self.run_id, turn, thread, task_id, ids.sha256_of_obj(row["brief"]), turn))
        scope = self.conn.execute("SELECT scope FROM v6_decision_tasks WHERE task_id=?", (task_id,)).fetchone()[0]
        actor = Actor(task_id, row["lease_id"], scope, thread, turn)
        self.actors[actor.lease_id] = actor
        if scope == "run":
            self.lead = actor
        return actor

    def next_actor(self, scope):
        if scope == "run":
            v6_goal.ensure_task(self.conn, self.run_id)
        row = self.conn.execute("""SELECT d.task_id FROM v6_decision_tasks d JOIN tasks t USING(task_id)
             WHERE d.run_id=? AND d.scope=? AND t.status IN ('READY','CLAIMED','RUNNING')
             ORDER BY d.rowid DESC LIMIT 1""", (self.run_id, scope)).fetchone()
        check(row is not None, f"No active decision for {scope}")
        return self.bind(row[0])

    def brief(self, actor):
        (text,) = self.conn.execute("""SELECT snapshot_json FROM research_contexts c
              JOIN v6_decision_tasks d USING(context_ref) WHERE d.task_id=?""", (actor.task_id,)).fetchone()
        return json.loads(text)

    def call(self, actor, tool, args, key):
        payload = {"threadId": actor.thread, "turnId": actor.turn, "tool": tool, "arguments": args, "callId": key}
        if self.roots.terminal_products and tool in self.roots.mode.decision_tools:
            payload["namespace"] = "gx"
        action = {"key": key, "tool": tool, "scope": actor.scope, "lease_id": actor.lease_id,
                  "task_id": actor.task_id, "request_sha256": ids.sha256_of_obj(args), "head_before": self.head()}
        try:
            reply = self.roots._call(payload)
        except GxError as exc:
            action.update(error=exc.code, reason=str(exc), details=exc.details)
            raise
        else:
            action["reply_sha256"] = ids.sha256_of_obj(reply)
            if isinstance(reply, dict):
                action.update({k: reply[k] for k in ("module_instance_id", "scope", "base", "result_head",
                    "decision_ref", "report_ref", "artifact_ref", "operation_key") if k in reply})
            return reply
        finally:
            action["head_after"] = self.head()
            self.actions.append(action)

    def open_args(self, key, *, inputs=None, limits=None, round=1, wake_on=None, caps=None):
        grant = {"api_calls": 6, "tokens": 6000, "job_submissions": 2, "cpu_seconds": 120,
                 "worker_tasks": 6}
        grant.update(limits or {})
        payload = {"module_id": "data_inventory", "module_version": "1", "focus": "data",
                   "expected_delivery": "Report measured deposited-coordinate coverage with source references.",
                   "round": round, "input_refs": list(self.materials[:2] if inputs is None else inputs),
                   "capabilities": list((QUALITY, GEOMETRY) if caps is None else caps),
                   "sub_budget": grant, "operation_key": key}
        if wake_on is not None:
            payload["wake_on"] = wake_on
        return payload

    def open(self, key, **kwargs):
        return self.call(self.lead, "gx_open_module", self.open_args(key, **kwargs), key)

    def node(self, actor, name, input_ref=None, *, capability=QUALITY, parameters=None, branch_id=None):
        return {"op": "add_node", "node": "local:" + name, "branch": branch_id or self.brief(actor)["module"]["branch_id"],
                "capability": capability, "inputs": [input_ref or self.materials[0]], "parameters": parameters or {},
                "depends_on": [], "question": "What coverage is recorded in this source?"}

    def submit_node(self, actor, name, input_ref=None, **kwargs):
        op = self.node(actor, name, input_ref, **kwargs)
        decision = draft(op, watches=[{"event": "job_settled", "subject_ref": "local:" + name}])
        return self.call(actor, v6_native.DECIDE, {"decision": decision}, "measure-" + name)

    def settle(self, node_id):
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            self.advance()
            [row] = self.rows("SELECT * FROM v6_nodes WHERE node_id=?", node_id)
            if row["status"] in v6_jobs.NODE_TERMINAL:
                check(row["status"] in {"SUCCEEDED","REUSED"}, f"Job {node_id} reached {row['status']}")
                check(ArtifactService(self.conn).verify(row["result_ref"])["ok"], "Unverified Job artifact")
                return row
            time.sleep(.035)  # Wait for the real worker process; interleavings use explicit checkpoints.
        raise TimeoutError(f"Job did not settle: {node_id} {row}")

    def report(self, actor, source_ref, key="report", text="The inventoried input was read.", **kwargs):
        payload = {"status": "COMPLETE", "findings": [{"text": text, "source_refs": [source_ref]}],
                   "gaps": [], "artifact_refs": [source_ref], "next_steps": [], "operation_key": key, **kwargs}
        return self.call(actor, "gx_submit_module_report", payload, key)

    def trace(self):
        turns = self.rows("""SELECT d.scope,d.task_id,d.base_json,d.context_ref,p.lease_id,p.thread_id,
            p.tool_names_json,p.brief FROM v6_decision_tasks d JOIN p0_roots p USING(task_id)
            WHERE d.run_id=? ORDER BY d.rowid,p.attempt""", self.run_id)
        for turn in turns:
            brief = turn.pop("brief")
            turn["brief_utf8_bytes"] = len(brief.encode())
            turn["brief_sha256"] = sha256(brief.encode())
            turn["brief_tokens_observed"] = None
            turn["transport_turns"] = self.rows("""SELECT d.turn_id,d.state,d.reservation_ref,
                d.token_budget,u.total_tokens AS scripted_total_tokens FROM p0_root_deliveries d
                LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id)
                WHERE d.thread_id=? AND d.method='turn/start' ORDER BY d.rowid""", turn["thread_id"])
            turn["tool_receipts"] = self.rows("SELECT call_id,turn_id,request_sha FROM p0_root_calls "
                                               "WHERE thread_id=? ORDER BY rowid", turn["thread_id"])
            turn["token_source"] = "local scripted transport; paid provider tokens unobserved"
            turn["tool_names"] = json.loads(turn.pop("tool_names_json"))
            turn["tool_summary_count"] = len(turn["tool_names"])
        job_rows = self.rows("""SELECT job_id,idempotency_key,status,capability_id,capability_version,
            runner_pid,runner_started,input_manifest_ref,measured_json,reservation_ref,exit_code FROM jobs
            WHERE run_id=? ORDER BY rowid""", self.run_id)
        nodes = self.rows("""SELECT n.node_id,n.branch_id,n.status,n.result_ref,n.job_id,n.exec_key,
            a.request_sha256,a.reused_from FROM v6_nodes n LEFT JOIN v6_job_attempts a USING(node_id)
            WHERE n.run_id=? ORDER BY n.rowid""", self.run_id)
        events = self.rows("""SELECT event_id,kind,subject_id,new_state,reason FROM events WHERE run_id=?
            AND (kind LIKE 'v6%' OR kind LIKE 'v62%' OR subject_kind='job') ORDER BY event_id""", self.run_id)
        return {"transport": "scripted acknowledgments at the native Roots callback boundary",
                "run_id": self.run_id, "paid_model_calls": 0, "provider_tokens": None, "external_usd": None,
                "turns": turns, "native_requests_to_provider": 0,
                "scripted_native_turn_count": sum(len(t["transport_turns"]) for t in turns),
                "persisted_native_tool_call_count": sum(len(t["tool_receipts"]) for t in turns),
                "serial_callback_observations": len(self.actions),
                "decisions_and_tools": self.actions, "head": self.head(), "events": events, "jobs": job_rows,
                "nodes": nodes, "costs": v6_costs.resource_report(self.conn, self.run_id)}
