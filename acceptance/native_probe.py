"""Exercise pinned official Codex via stdio and a local deterministic Responses peer."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from native_probe_peer import BRIEF, CHILD, DYNAMIC, STEER, Peer

PACKAGE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE / "gx-h"))
from gx_core.hostobs.app_server_client import AppServerClient, AppServerResponseError
from gx_core.hostobs.native_app_server import _proxy_preflight

DYN_SPEC = {
    "name": DYNAMIC,
    "description": "Return a constant local identity for this protocol probe.",
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
}
INIT = {"clientInfo": {"name": "gx-acceptance", "version": "1"}, "capabilities": {"experimentalApi": True}}


def find_binary():
    entry = Path(shutil.which("codex")).resolve()
    if entry.name != "codex":
        bins = list(entry.parents[1].glob("node_modules/@openai/codex-*/vendor/*/bin/codex"))
        if len(bins) != 1:
            raise RuntimeError("Set --binary to the official ELF")
        entry = bins[0]
    return entry


def text(value):
    return [{"type": "text", "text": value}]


class Probe:
    def __init__(self, directory, binary, peer, openai=False):
        self.peer, self.directory, self.binary = peer, directory, binary
        directory.mkdir()
        self.home = directory / "codex-home"
        self.home.mkdir()
        self.cwd = directory / "work"
        self.cwd.mkdir()
        url = f"http://127.0.0.1:{peer.server_port}"
        self.provider = "local_openai" if openai else "local_probe"
        self.env = {
            k: v
            for k, v in os.environ.items()
            if k
            in (
                "PATH",
                "LANG",
                "LD_LIBRARY_PATH",
                "http_proxy",
                "https_proxy",
                "all_proxy",
                "no_proxy",
                "HTTP_PROXY",
                "HTTPS_PROXY",
                "ALL_PROXY",
                "NO_PROXY",
            )
        }
        self.env.update(HOME=str(directory), CODEX_HOME=str(self.home), CODEX_APP_SERVER_CHATGPT_BASE_URL=url)
        if openai:
            self.env["OPENAI_API_KEY"] = "loopback-test-fixture"
        (self.home / "config.toml").write_text(f"""model_provider = "{self.provider}"
model = "gpt-5.4"
model_reasoning_effort = "xhigh"
chatgpt_base_url = "{url}"
openai_base_url = "{url}/v1"
check_for_update_on_startup = false
suppress_unstable_features_warning = true
compact_prompt = "GX local compact request"
experimental_thread_store = {{type = "local"}}
[features]
respect_system_proxy = true
system_proxy_fallback = false
goals = true
multi_agent_v2 = true
multi_agent_v2_dynamic_tools = true
apps = false
hooks = false
plugins = false
[model_providers.{self.provider}]
name = "{'OpenAI' if openai else 'GX loopback'}"
base_url = "{url}/v1"
wire_api = "responses"
requires_openai_auth = {'true' if openai else 'false'}
request_max_retries = 0
stream_max_retries = 0
[analytics]
enabled = false
[feedback]
enabled = false
""")
        self.log, self.callbacks = [], []
        self.client = None
        self.gen = 0

    def record(self, **entry):
        self.log.append(dict(t=time.monotonic(), generation=self.gen, **entry))

    def notification(self, frame):
        m = frame.message
        p = m.get("params", {})
        turn, thread, item = p.get("turn", {}), p.get("thread", {}), p.get("item", {})
        rec = {"method": m["method"], "seq": frame.receive_seq}
        for k, v in {
            "thread": p.get("threadId") or thread.get("id"),
            "turn": p.get("turnId") or turn.get("id"),
            "status": turn.get("status"),
            "item_type": item.get("type"),
            "item_id": item.get("id"),
            "tool": item.get("tool"),
            "exitCode": item.get("exitCode"),
            "error": turn.get("error"),
            "kind": item.get("kind"),
            "agent_thread_id": item.get("agentThreadId"),
        }.items():
            if v is not None:
                rec[k] = v
        self.record(direction="notification", **rec)

    def callback(self, frame):
        m = frame.message
        p = m.get("params", {})
        row = {
            "method": m["method"],
            "thread": p.get("threadId"),
            "turn": p.get("turnId"),
            "tool": p.get("tool"),
        }
        self.callbacks.append(row)
        self.record(direction="callback", **row)
        if m["method"] != "item/tool/call" or p["tool"] != DYNAMIC:
            raise AssertionError(f"unexpected callback {row}")
        return {"success": True, "contentItems": [{"type": "inputText", "text": "GX_NATIVE_OK"}]}

    def connect(self, before_init=False):
        self.gen += 1
        self.client = AppServerClient(
            [str(self.binary), "app-server", "--listen", "stdio://"],
            env=self.env,
            cwd=self.cwd,
            on_notification=self.notification,
            on_server_request=self.callback,
            default_timeout=15,
        ).start()
        if before_init:
            err = self.error("thread/list", {})
            assert "initializ" in err["message"].lower(), err
        self.rpc("initialize", INIT)
        self.client.notify("initialized", {})
        self.record(direction="send", method="initialized")

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

    def kill(self):
        proc = self.client.transport.process
        proc.kill()
        self.record(direction="fault", method="SIGKILL", pid=proc.pid, exit=proc.wait(5))
        self.close()

    def rpc(self, method, params):
        self.record(
            direction="request",
            method=method,
            thread=params.get("threadId"),
            turn=params.get("expectedTurnId"),
            keys=list(params),
        )
        r = self.client.request(method, params)
        self.record(
            direction="result",
            method=method,
            seq=self.client.last_received.receive_seq,
            thread=(r.get("thread") or {}).get("id"),
            turn=(r.get("turn") or {}).get("id"),
        )
        return r

    def error(self, method, params):
        try:
            self.rpc(method, params)
        except AppServerResponseError as exc:
            details = {"code": exc.code, "message": exc.message, "data": exc.data}
            self.record(direction="error", method=method, **details)
            return details
        raise AssertionError(method + " unexpectedly succeeded")

    def thread(self, **extra):
        return self.rpc(
            "thread/start",
            {
                "cwd": str(self.cwd),
                "developerInstructions": BRIEF,
                "environments": [
                    {"environmentId": "local", "cwd": str(self.cwd), "runtimeWorkspaceRoots": [str(self.cwd)]}
                ],
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "dynamicTools": [DYN_SPEC],
                **extra,
            },
        )["thread"]["id"]

    def start_turn(self, tid, value):
        return self.rpc("turn/start", {"threadId": tid, "input": text(value)})["turn"]["id"]

    def event(self, method, tid=None, turn=None, item=None):
        def match(frame):
            m = frame.message
            p = m.get("params", {})
            return (
                m.get("method") == method
                and (tid is None or p.get("threadId") == tid)
                and (turn is None or p.get("turn", {}).get("id", p.get("turnId")) == turn)
                and (item is None or p.get("item", {}).get("type") == item)
            )

        return self.client.run_until(match, timeout=15).message["params"]

    def finish(self, tid, turn):
        result = self.event("turn/completed", tid, turn)["turn"]
        assert result["status"] == "completed", result.get("error", result["status"])
        return result

    def turn(self, tid, value):
        turn = self.start_turn(tid, value)
        self.finish(tid, turn)
        return turn

    def goal(self, tid, **updates):
        verb = "set" if updates else "get"
        return self.rpc("thread/goal/" + verb, {"threadId": tid, **updates})["goal"]


def handshake_and_restart(p, result):
    p.connect(before_init=True)
    duplicate = p.error("initialize", INIT)
    tid = p.thread()
    turn = p.turn(tid, "Seed durable root")
    methods = [e["method"] for e in p.log if e["direction"] == "notification"]
    for name in ("thread/started", "turn/started", "item/started", "item/completed", "turn/completed"):
        assert name in methods, name
    idle = p.error("turn/steer", {"threadId": tid, "expectedTurnId": turn, "input": text("idle")})
    p.peer.scenario("dynamic")
    fallback = p.turn(tid, "Idle fallback: execute dynamic tool")
    assert fallback != turn and p.callbacks[-1]["thread"] == tid
    p.kill()
    p.connect()
    r = p.rpc("thread/resume", {"threadId": tid})["thread"]
    assert r["id"] == tid and {turn, fallback} <= {t["id"] for t in r["turns"]}
    p.peer.scenario("dynamic")
    resumed = p.turn(tid, "Execute previously supplied dynamic tool")
    assert p.callbacks[-1]["turn"] == resumed
    result.update(
        thread=tid,
        turns=[turn, fallback, resumed],
        preexisting_turns_after_restart=len(r["turns"]),
        duplicate_initialize=duplicate,
        idle_steer=idle,
    )
    saved = p.rpc("thread/read", {"threadId": tid, "includeTurns": True})
    original = next(t for t in saved["thread"]["turns"] if t["id"] == turn)
    expected = sorted((item["type"], item["id"]) for item in original["items"])
    for method in ("item/started", "item/completed"):
        live = sorted(
            (e["item_type"], e["item_id"]) for e in p.log if e["method"] == method and e.get("turn") == turn
        )
        assert live == expected, (method, live, expected)
    observed = []
    page_params = {"threadId": tid, "turnId": turn, "limit": 1}
    while True:
        page = p.rpc("thread/items/list", page_params)
        observed.extend((entry["item"]["type"], entry["item"]["id"]) for entry in page["data"])
        if not page.get("nextCursor"):
            break
        page_params["cursor"] = page["nextCursor"]
    assert sorted(observed) == expected
    result["notification_items_reconciled"] = expected


def shell_steer(p, result):
    p.connect()
    tid = p.thread(sandbox="danger-full-access")
    result.update(thread=tid, samples=[], shell_profile="danger-full-access; fixed fixture sleep and echo")
    for i in range(3):
        p.peer.scenario("shell")
        turn = p.start_turn(tid, "Run sleeping POSIX command")
        p.event("item/started", tid, turn, "commandExecution")
        wrong = p.error("turn/steer", {"threadId": tid, "expectedTurnId": "incorrect", "input": text("bad")})
        t0 = time.monotonic()
        p.rpc("turn/steer", {"threadId": tid, "expectedTurnId": turn, "input": text(f"{STEER} sample {i}")})
        ack = time.monotonic()
        done = p.event("item/completed", tid, turn, "commandExecution")
        assert done["item"]["exitCode"] == 0 and "GX_SHELL_FINISHED" in done["item"]["aggregatedOutput"], done
        p.finish(tid, turn)
        wire = [r for r in p.peer.rows if r["t"] >= t0 and i in r["steer_samples"]]
        assert wire
        result["samples"].append(
            {
                "turn": turn,
                "rpc_ack_s": round(ack - t0, 4),
                "model_observation_s": round(wire[0]["t"] - t0, 4),
                "command_exit": done["item"]["exitCode"],
                "wrong_turn_rpc_code": wrong["code"],
            }
        )


def await_request(p, after):
    limit = time.monotonic() + 10
    while len(p.peer.rows) <= after and time.monotonic() < limit:
        time.sleep(0.02)
    assert len(p.peer.rows) > after, "no model request at barrier"


def goal_wait(p, result):
    p.connect()
    tid = p.thread()
    g = p.goal(tid, objective="GX_WAIT", status="paused", tokenBudget=9000)
    assert g["status"] == "paused" and g["tokensUsed"] == 0
    count = len(p.peer.rows)
    time.sleep(1)
    assert len(p.peer.rows) == count
    p.peer.gate.clear()
    turn = p.start_turn(tid, "Begin the explicit Job-completion wake turn")
    await_request(p, count)
    active = p.goal(tid, status="active")
    p.peer.scenario("finish_goal")
    p.peer.gate.set()
    p.finish(tid, turn)
    # Active goals can schedule a continuation in this version. Drive it through
    # the public event stream before reading settlement.
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        spent = p.goal(tid)
        if spent["status"] == "complete":
            break
        time.sleep(0.08)
    assert spent["tokensUsed"] > 0 and spent["status"] == "complete", spent
    replaced = p.goal(tid, objective="GX_GOAL_REPLACED", status="paused")
    assert replaced["tokensUsed"] >= spent["tokensUsed"] and replaced["objective"] == "GX_GOAL_REPLACED", (
        spent,
        replaced,
    )
    clear = p.rpc("thread/goal/clear", {"threadId": tid})
    assert p.goal(tid) is None
    fresh = p.goal(tid, objective="fresh", status="paused")
    assert fresh["tokensUsed"] == 0
    result.update(
        fresh=fresh,
        thread=tid,
        wake_turn=turn,
        paused=g,
        active=active,
        spent=spent,
        replaced=replaced,
        clear=clear,
        wait_seconds=1,
        paused_requests=0,
    )


def main_disabled(p, result):
    p.connect()
    tid = p.thread(config={"features.goals": False})
    p.peer.scenario("try_goal")
    turn = p.turn(tid, "Test if the model can create its own Goal")
    time.sleep(0.4)
    rows = p.peer.rows
    assert not any(r["goal_tools"] for r in rows), rows[0]["goal_tools"]
    assert any("unsupported call: create_goal" in output for r in rows for output in r["tool_outputs"])
    goal = p.goal(tid)
    assert not goal, goal
    before = len(rows)
    projected = p.goal(tid, objective="External projection on disabled runtime", status="active")
    time.sleep(1.0)
    assert len(rows) == before, "disabled main thread scheduled a continuation"
    p.rpc("thread/goal/clear", {"threadId": tid})
    result.update(
        thread=tid,
        turn=turn,
        externally_projected_status=projected["status"],
        automatic_requests_with_external_active_goal=len(rows) - before,
        observed_idle_seconds=1.0,
        model_attempt="create_goal",
        goal=goal,
        model_requests=len(rows),
        goal_advertised=rows[0]["goal_mutator_declared"],
    )


def goal_update(p, result):
    p.connect()
    tid = p.thread()
    p.goal(tid, objective="GX_FIRST", status="paused", tokenBudget=9000)
    p.peer.scenario("model_edit")
    p.peer.gate.clear()
    turn = p.start_turn(tid, "Test legitimate external update and model tool boundary")
    await_request(p, 0)
    p.goal(tid, objective="GX_GOAL_REPLACED", status="active")
    p.peer.gate.set()
    p.finish(tid, turn)
    goal = p.goal(tid)
    assert goal["objective"] == "GX_GOAL_REPLACED"
    injected = [r["id"] for r in p.peer.rows if r["objective_change_in_input"]]
    result.update(
        thread=tid,
        turn=turn,
        goal=goal,
        model_edit_attempt="GX_FORGED_OBJECTIVE",
        observed_updated_objective_in_model_inputs=injected,
    )
    assert injected


def shell_killed(p, result):
    p.connect()
    tid = p.thread(sandbox="danger-full-access")
    p.peer.scenario("kill_shell")
    turn = p.start_turn(tid, "Run bounded local sentinel")
    p.event("item/started", tid, turn, "commandExecution")
    deadline = time.monotonic() + 1.5
    while not (p.cwd / "started").exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert (p.cwd / "started").exists()
    p.kill()
    time.sleep(2.2)
    p.connect()
    history = p.rpc("thread/resume", {"threadId": tid})["thread"]["turns"]
    interrupted = next((t["status"] for t in history if t["id"] == turn), "absent")
    result.update(
        thread=tid,
        turn=turn,
        recovered=interrupted,
        started_before_kill=True,
        finished_file_after_kill=(p.cwd / "finished").exists(),
        observation_seconds_after_kill=2.2,
    )
    assert interrupted != "completed"


def interrupted(p, result):
    p.connect()
    tid = p.thread()
    p.turn(tid, "Seed known completed turn before process failure")
    p.peer.gate.clear()
    n = len(p.peer.rows)
    turn = p.start_turn(tid, "Hold model response until receiver dies")
    await_request(p, n)
    p.kill()
    p.peer.gate.set()
    p.connect()
    restored = p.rpc("thread/resume", {"threadId": tid})["thread"]
    assert restored["id"] == tid
    status = next((t["status"] for t in restored["turns"] if t["id"] == turn), "absent")
    completed = p.turn(tid, "Reconcile original and run a new identified wake turn")
    count = len(p.peer.rows)
    p.peer.gate.clear()
    to_interrupt = p.start_turn(tid, "Hold the explicit interrupt target")
    await_request(p, count)
    p.rpc("turn/interrupt", {"threadId":tid, "turnId":to_interrupt})
    p.peer.gate.set()
    interrupted=p.event("turn/completed",tid,to_interrupt)["turn"]
    assert interrupted["status"]=="interrupted", interrupted["status"]
    result.update(thread=tid, turn_at_sigkill=turn, recovered_status=status, recovery_turn=completed,
                  explicitly_interrupted=to_interrupt, explicit_interrupt_status=interrupted["status"])


def create_loss(p, result):
    p.connect()
    # The test controller sees the ACK to place the cut precisely. No ID is
    # committed to the simulated GX side. Reconciliation below consumes only
    # official thread/list with a unique workspace.
    oracle = p.thread()
    p.kill()
    p.connect()
    rows = p.rpc("thread/list", {"cwd": str(p.cwd), "modelProviders": []})["data"]
    ids = [r["id"] for r in rows]
    if len(ids) == 1:
        assert ids[0] == oracle
    else:
        assert not ids
    result.update(
        oracle_thread=oracle,
        list_candidates=ids,
        starts_sent=1,
        recovery="same ID visible" if ids else "unknown; no second create",
    )


def mismatched(p, result):
    p.connect()
    tid = p.thread()
    p.turn(tid, "Seed provider history")
    bad = p.error("thread/start", {"modelProvider": "gx_not_configured", "cwd": str(p.cwd)})
    try:
        mismatch = p.rpc("thread/resume", {"threadId": tid, "modelProvider": "openai"})
        observed = {
            "outcome": "accepted",
            "provider": mismatch.get("modelProvider"),
            "thread": mismatch["thread"]["id"],
        }
    except AppServerResponseError as exc:
        observed = {"outcome": "rejected", "code": exc.code, "data": exc.data, "message": exc.message}
    p.kill()
    p.connect()
    try:
        cold = p.rpc(
            "thread/resume", {"threadId": tid, "modelProvider": "openai", "allowProviderModelFallback": False}
        )
        cold_observed = {
            "outcome": "accepted",
            "provider": cold.get("modelProvider"),
            "thread": cold["thread"]["id"],
        }
    except AppServerResponseError as exc:
        cold_observed = {"outcome": "rejected", "code": exc.code, "data": exc.data, "message": exc.message}
    result.update(
        thread=tid, unconfigured=bad, loaded_resumed_with_openai=observed, cold_strict_resume=cold_observed
    )
    assert observed["outcome"] == "rejected" or observed["provider"] == p.provider, observed
    assert cold_observed["outcome"] == "rejected" and "provider" in cold_observed["message"], cold_observed


def compact(p, result):
    p.connect()
    tid = p.thread()
    p.turn(tid, "Seed conversation for compaction")
    p.peer.scenario("compact")
    p.rpc("thread/compact/start", {"threadId": tid})
    p.event("item/completed", tid, item="contextCompaction")
    p.event("turn/completed", tid)
    compact_wire = p.peer.rows[-1]
    p.peer.scenario("plain")
    p.turn(tid, "Continue after compaction")

    def fields(row):
        return {
            k: row[k]
            for k in (
                "brief_in_input",
                "brief_in_instructions",
                "dynamic_in_catalog",
                "local_summary_in_context",
                "encrypted_summary_in_context",
            )
        }

    after = fields(p.peer.rows[-1])
    p.kill()
    p.connect()
    p.rpc("thread/resume", {"threadId": tid})
    p.turn(tid, "Continue after process restart and durable resume")
    resumed = fields(p.peer.rows[-1])
    fork = p.rpc("thread/fork", {"threadId": tid})["thread"]["id"]
    p.turn(fork, "Continue in fork of compacted session")
    forked = fields(p.peer.rows[-1])
    result.update(
        thread=tid,
        fork=fork,
        remote_compaction_trigger=compact_wire["compact_trigger"],
        after_compaction=after,
        after_resume=resumed,
        after_fork=forked,
    )
    assert compact_wire["compact_trigger"] == (p.provider == "local_openai"), result
    for row in (after, resumed, forked):
        assert row["encrypted_summary_in_context"] or row["local_summary_in_context"], row
        assert row["brief_in_input"] or row["brief_in_instructions"], row
        assert row["dynamic_in_catalog"], row


def subagent(p, result):
    inherit = result["test"] != "child_default_tools"
    if not inherit:
        path = p.home / "config.toml"
        path.write_text(path.read_text().replace("multi_agent_v2_dynamic_tools = true\n", ""))
    p.connect()
    tid = p.thread()
    p.peer.scenario("spawn")
    p.peer.gate.clear()
    n = len(p.peer.rows)
    turn = p.start_turn(tid, "Spawn a temporary Codex child for the local sample")
    await_request(p, n)
    p.goal(tid, objective="Measure descendant accounting", status="active", tokenBudget=8000)
    p.peer.gate.set()
    p.finish(tid, turn)
    goal = p.goal(tid)
    child_callbacks = [r for r in p.callbacks if r["thread"] != tid]
    if inherit:
        assert child_callbacks, "no child dynamic callback"
        cid = child_callbacks[0]["thread"]
    else:
        assert not child_callbacks
        children = {
            e["agent_thread_id"]
            for e in p.log
            if e.get("item_type") == "subAgentActivity"
            and e.get("kind") == "started"
            and e.get("thread") == tid
        }
        assert len(children) == 1, children
        cid = children.pop()
    parent = p.rpc("thread/read", {"threadId": tid})["thread"]
    child = p.rpc("thread/read", {"threadId": cid})["thread"]
    child_config = p.rpc("thread/resume", {"threadId": cid})
    parent_config = p.rpc("thread/resume", {"threadId": tid})
    permissions = {
        k: {"root": parent_config.get(k), "child": child_config.get(k)} for k in ("approvalPolicy", "sandbox")
    }
    assert permissions["approvalPolicy"] == {"root": "never", "child": "never"}, permissions
    assert permissions["sandbox"]["child"] == permissions["sandbox"]["root"], permissions
    assert permissions["sandbox"]["root"]["type"] == "readOnly", permissions
    assert tid != cid
    rows = p.peer.rows
    parent_tokens = sum(r["usage"]["total_tokens"] for r in rows if not r["child"])
    child_tokens = sum(r["usage"]["total_tokens"] for r in rows if r["child"])
    result.update(
        parent=tid,
        child=cid,
        permissions=permissions,
        wake_turn=turn,
        parent_model_tokens=parent_tokens,
        child_model_tokens=child_tokens,
        goal=goal,
        child_source=child.get("source"),
        child_callback=child_callbacks,
        parent_cwd=parent.get("cwd"),
        child_cwd=child.get("cwd"),
    )
    assert child_tokens > 0 and goal["tokensUsed"] > parent_tokens
    if not inherit:
        assert all(not r["dynamic_in_catalog"] for r in rows if r["child"])
        assert any(
            "unsupported call: gx_probe_identity" in output
            for r in rows
            if r["child"]
            for output in r["tool_outputs"]
        )
        return
    p.kill()
    p.connect()
    p.rpc("thread/resume", {"threadId": tid})
    p.peer.scenario("follow")
    p.peer.child_done.clear()
    before = len(p.callbacks)
    follow = p.turn(tid, "Follow up with the original native child after restart")
    callbacks = p.callbacks[before:]
    result.update(followup_turn=follow, restored_child_callbacks=callbacks)
    assert any(c["thread"] == cid and c["tool"] == DYNAMIC for c in callbacks), callbacks


CASES = {
    "session": handshake_and_restart,
    "shell": shell_steer,
    "goal": goal_wait,
    "main": main_disabled,
    "goal_update": goal_update,
    "kill_shell": shell_killed,
    "kill": interrupted,
    "create_loss": create_loss,
    "provider": mismatched,
    "compact_local": compact,
    "compact_openai": compact,
    "child": subagent,
    "child_default_tools": subagent,
}


def gx_probe(p, result):
    from native_probe_roots import run

    run(p, result, result["test"])


CASES.update({name: gx_probe for name in ("gx_create_loss", "gx_delivery_loss", "gx_fence", "gx_job_wait")})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--out", type=Path, default=PACKAGE / "acceptance/runs/native")
    parser.add_argument("cases", nargs="*", metavar="CASE")
    args = parser.parse_args()
    for name in args.cases:
        if name not in CASES:
            parser.error(f"Unknown case {name}")
    _proxy_preflight()
    binary = args.binary or find_binary()
    with binary.open("rb") as handle:
        binary_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    args.out.mkdir(parents=True, exist_ok=True)
    errors = 0
    with tempfile.TemporaryDirectory(prefix="gx-native-p0-") as temp:
        directory = Path(temp)
        for name in args.cases or CASES:
            peer = Peer()
            threading.Thread(target=peer.serve_forever, daemon=True).start()
            p = Probe(directory / name, binary, peer, openai=name == "compact_openai")
            result = {
                "test": name,
                "started_utc": datetime.now(timezone.utc).isoformat(),
                "provider": p.provider,
                "binary_version": subprocess.check_output(
                    [binary, "--version"], env=p.env, text=True, stderr=subprocess.DEVNULL
                ).strip(),
                "binary_sha256": binary_hash,
                "model_service": "loopback scripted Responses",
                "gpu_seconds": 0,
                "external_api_cost": 0,
                "human_intervention": False,
            }
            try:
                assert result["binary_version"] == "codex-cli 0.169.0", result["binary_version"]
                CASES[name](p, result)
                assert all(r["wire_effort"] == "xhigh" for r in peer.rows), "unexpected wire reasoning effort"
                result["status"] = "PASS"
            except Exception as exc:
                errors += 1
                result.update(status="FAIL", error=str(exc), traceback=traceback.format_exc(limit=4))
            finally:
                peer.gate.set()
                p.close()
                peer.shutdown()
                peer.server_close()
                result.update(
                    log=p.log,
                    model_requests=peer.rows,
                    callbacks=p.callbacks,
                    finished_utc=datetime.now(timezone.utc).isoformat(),
                    fixture_tokens=sum(r["usage"]["total_tokens"] for r in peer.rows),
                )
                (args.out / (name + ".json")).write_text(
                    json.dumps(result, indent=2, ensure_ascii=False) + "\n"
                )
            print(name, result["status"], result.get("error", ""), flush=True)
    return bool(errors)


if __name__ == "__main__":
    sys.exit(main())
