"""Exercise native delegated artifact reads with the pinned App Server and loopback Responses."""

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from unittest.mock import patch

from native_probe import CHILD, PACKAGE, Peer, Probe, _proxy_preflight, find_binary
from native_probe_peer import declared_calls, tool, wire_catalog
from gx_core import budgets
from gx_core.artifacts.service import ArtifactService
from gx_core.p0 import schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


ORIGINAL = "Observation 7: μ = 0.25 ± 0.01.\nOriginal notebook entry."


class ChildProbeTimeout(AssertionError):
    pass


class ChildPeer(Peer):
    def __init__(self):
        super().__init__()
        self.expire = threading.Event()
        self.expired = threading.Event()
        self.driver_polled = threading.Event()
        self.model_reads = []

    def record(self, body):
        row, n = super().record(body)
        if getattr(self, "observe_catalogs", False):
            specs, exposed = wire_catalog(body)
            row["gx_catalog"] = sorted(name for name in exposed if name.startswith("gx_"))
            row["submit_namespace"] = next((namespace for namespace, name in declared_calls(specs)
                                            if name == "gx_submit_product"), None)
        phase = row["scenario"] + "-"
        replies = {}
        for item in body.get("input", []):
            key = item.get("call_id") or ""
            if item["type"] != "function_call_output" or not key.startswith(phase):
                continue
            value = item["output"]
            if isinstance(value, list):
                value = "".join(p["text"] for p in value if "text" in p)
            try:
                replies[key] = json.loads(value)
            except (TypeError, ValueError):
                continue
        row["replies"] = replies
        for key, reply in replies.items():
            if isinstance(reply, dict) and reply.get("content") == ORIGINAL:
                self.model_reads.append((row["scenario"], row["child"], key))
        return row, n

    def item(self, row, n):
        phase, replies = row["scenario"], row["replies"]
        def call(name, args, label, namespace=None):
            return tool(name, args, f"{phase}-{label}-{n}", namespace)
        def read(ref, label):
            return call("gx_read_artifact", {"ref": ref}, label)
        if not self.gate.wait(15) or n > 18:
            return self.message(row["id"], "Fixture deadline reached")
        if phase in ("fork", "other_root"):
            return read(self.source, "ordinary") if n <= 2 else self.message(row["id"], "Fork finished")
        if not row["child"]:
            if n == 1 and phase == "warm":
                return call("spawn_agent", {"task_name": CHILD, "fork_turns": "none",
                            "message": "Read the notebook artifact " + self.source}, "spawn", "collaboration")
            if n == 1:
                return call("followup_task", {"target": "/root/" + CHILD, "message": "Re-read " + self.source},
                            "follow", "collaboration")
            self.child_done.wait(15)
            if phase == "warm" and n == 2:
                return call("gx_wait_job", {"job_id": self.job}, "park")
            return self.message(row["id"], "Delegated read finished")
        successes = any(isinstance(reply, dict) and reply.get("content") == ORIGINAL for reply in replies.values())
        if not successes:
            if replies:
                # A model receiving the retry instruction waits for the outer
                # driver poll; a loopback peer could otherwise spin inside one RPC.
                self.driver_polled.clear()
                self.driver_polled.wait(3)
            return read(self.source, "source")
        if phase == "warm":
            if not any("-other-" in k for k in replies):
                return read(self.foreign, "other")
            if not any("-write-" in k for k in replies):
                return call("gx_submit_product", {"product": {"note": "child attempt"}}, "write",
                            row.get("submit_namespace"))
        elif not any("-expired-" in k for k in replies):
            self.expire.set()
            self.expired.wait(15)
            return read(self.source, "expired")
        self.child_done.set()
        return self.message(row["id"], "I read the original notebook.")


def run(directory, binary, *, drop_spawn=False, drop_cold_start=True, role_profile=None):
    version = subprocess.check_output([str(binary), "--version"], text=True).strip()
    if version != "codex-cli 0.169.0":
        raise RuntimeError("This probe requires Codex 0.169.0")
    with binary.open("rb") as f:
        binary_sha = hashlib.file_digest(f, "sha256").hexdigest()
    lock = json.loads((PACKAGE / "release-assets" / "lock.json").read_text())
    if binary_sha != lock["linux_x64"]["binary_sha256"]:
        raise RuntimeError("The Codex binary differs from the pinned Linux release")
    peer = ChildPeer()
    peer.observe_catalogs = role_profile is not None
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    probe = Probe(directory / "official", binary, peer)
    cfg = probe.home / "config.toml"
    cfg.write_text(cfg.read_text().replace("multi_agent_v2 = true", "multi_agent_v2 = false")
                   .replace("multi_agent_v2_dynamic_tools = true", "multi_agent_v2_dynamic_tools = false"))
    knobs = {"GX_P0_TEST": "1", "GX_P0_TEST_PROVIDER": probe.provider, "GX_P0_MODEL": "gpt-6.1-sol",
             "GX_P0_CODEX": str(binary), "CODEX_HOME": str(probe.home)}
    if role_profile is not None:
        profile_path = directory / "profiles.json"
        profile_path.write_text(json.dumps({"roles": {"evidence_reviewer": role_profile}}))
        knobs["GX_P0_PROFILE"] = str(profile_path)
    callbacks, dropped, cold_started, lost_spawn, tasks = [], [], [], [], {}
    completed = set()
    try:
        with patch.dict(os.environ, knobs), closing(store.open_store(directory / "gx.sqlite")) as conn:
            schema.install(conn)
            budget = budgets.create_root(conn, run_id=None, profile_name="p0", actor="probe")
            options = {}
            if role_profile is not None:
                from gx_core.hostobs.host_policy import p0_host
                document, digest, host = p0_host()
                options = {"workflow_id": "science_grid", "workflow_version": "3", "host": host,
                           "agent_policy": document, "pins": {"agent_policy_sha256": digest}}
                profile_path.unlink()  # Both connections must use the saved Run selection.
            rid = records.create_run(conn, objective="Review notebook entry", project_ref="read", actor="probe",
                                     budget_ref=budget, **options)
            other = records.create_run(conn, objective="Other notebook", project_ref="unrelated", actor="probe")
            for status in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(conn, machine="run", subject_id=rid, to_state=status, actor="probe")
            def put(run_id, value):
                return ArtifactService(conn).put(run_id=run_id, kind="notebook", media_type="text/plain",
                       content=value, origin="HOST_PUT", put_by="probe").artifact_ref
            peer.source, peer.foreign, peer.job = put(rid, ORIGINAL), put(other, "Unrelated note"), "wait-probe"
            task = records.create_task(conn, run_id=rid, kind="research_round", role="evidence_reviewer",
                                       task_input={"source": peer.source}, actor="probe")
            store.transition(conn, machine="task", subject_id=task, to_state="READY", actor="probe")
            def connect():
                # No command-line agent feature flags. Both start disabled in
                # config.toml so this exercises the research root's own settings.
                argv = [str(binary), "--enable", "respect_system_proxy", "--disable", "system_proxy_fallback",
                        "app-server", "--listen", "stdio://"]
                return Roots(conn, run_id=rid, cwd=probe.cwd, argv=argv, env=probe.env, token_budget=4000)
            def counts():
                return {t: conn.execute("SELECT count(*) FROM " + t).fetchone()[0] for t in ("tasks", "leases")}
            def trace(roots, cold_child=None):
                previous = roots.client.on_server_request
                def callback(frame):
                    p = frame.message["params"]
                    response = previous(frame)
                    body = json.loads(response["contentItems"][0]["text"])
                    callbacks.append({"phase": peer.plan, "thread": p["threadId"], "turn": p["turnId"],
                                      "call": p["callId"], "tool": p["tool"], "success": response["success"],
                                      "returned_text": body.get("content"), "error": body.get("error")})
                    return response
                roots.client.on_server_request = callback
                notify = roots.client.on_notification
                def observe(frame):
                    m = frame.message
                    item = (m.get("params") or {}).get("item") or {}
                    if (drop_spawn and peer.plan == "warm" and m["method"] in ("item/started", "item/completed")
                            and item.get("type") == "subAgentActivity" and item.get("kind") == "started"):
                        lost_spawn.append(m["method"])
                        return
                    if m["method"] == "turn/completed":
                        completed.add((m["params"]["threadId"], m["params"]["turn"]["id"]))
                    if cold_child and m["method"] == "turn/started" and m["params"]["threadId"] == cold_child:
                        cold_started.append(m["params"]["turn"]["id"])
                        if drop_cold_start:
                            dropped.append(m["params"]["turn"]["id"])
                            return
                    notify(frame)
                roots.client.on_notification = observe
            def until(roots, done):
                deadline = time.monotonic() + 25
                while not done():
                    if time.monotonic() >= deadline:
                        raise ChildProbeTimeout(f"{peer.plan} timed out: {callbacks!r}; requests {peer.scenario_count}")
                    roots.poll(.025)
                    peer.driver_polled.set()
                    if peer.expire.is_set() and not peer.expired.is_set():
                        conn.execute("UPDATE leases SET expires_at='2000-01-01T00:00:00Z' WHERE lease_id=?", (lease,))
                        peer.expired.set()
            with connect() as roots:
                trace(roots)
                lease = roots._claim(task)["lease_id"]
                conn.execute("INSERT INTO jobs(job_id,run_id,task_id,lease_ref,capability_id,capability_version,"
                             "status,idempotency_key,submit_intent_json,input_manifest_ref,created_at) "
                             "VALUES(?,?,?,?, 'fixture','1','RUNNING',?,'{}','ref','now')",
                             (peer.job, rid, task, lease, peer.job))
                tasks["before"] = counts()
                peer.scenario("warm")
                peer.gate.clear()
                root = roots.dispatch(task)
                tid = root["thread_id"]
                peer.gate.set()
                # A synchronous host request can pump early callbacks before the next driver poll.
                roots.client.run_until(lambda f: f.message.get("method") == "item/tool/call" and
                                       f.message["params"]["threadId"] != tid, timeout=15)
                until(roots, lambda: bool(roots._binding(tid)["wait_job_id"]) and tid not in roots.active_turns)
                child = conn.execute("SELECT child_id FROM p0_native_child_meters WHERE root_id=?", (tid,)).fetchone()[0]
                facts = roots._request("thread/read", {"threadId": child})["thread"]
                with Path(facts["path"]).open() as rollout:
                    first = json.loads(rollout.readline())
                if first["type"] != "session_meta" or first["payload"]["id"] != child:
                    raise AssertionError("The official child metadata did not match its rollout")
                child_meta = first["payload"]
                inherited_calls = list(declared_calls(child_meta["dynamic_tools"]))
                inherited = [name for namespace, name in inherited_calls]
                peer.scenario("fork")
                fork = roots._request("thread/fork", {"threadId": tid})["thread"]
                fork_turn = roots._request("turn/start", {"threadId": fork["id"],
                             "input": [{"type":"text", "text":"Read the source."}]})["turn"]["id"]
                until(roots, lambda: (fork["id"], fork_turn) in completed)
                peer.scenario("other_root")
                read_spec = next(spec for spec in roots.worker_tools if spec["name"] == "gx_read_artifact")
                other_roots = Roots(conn, run_id=other, cwd=probe.cwd, main_tools=(read_spec,))
                other_roots.client = roots.client
                other_id = other_roots.ensure_main()
                other_turn = roots._request("turn/start", {"threadId": other_id,
                              "input": [{"type":"text", "text":"Read the source."}]})["turn"]["id"]
                until(roots, lambda: (other_id, other_turn) in completed)
            peer.scenario("cold")
            peer.child_done.clear()
            conn.execute("UPDATE jobs SET status='SUCCEEDED' WHERE job_id=?", (peer.job,))
            with connect() as roots:
                trace(roots, cold_child=child)
                cold_facts = roots._request("thread/read", {"threadId": child})["thread"]
                next_reconcile = 0
                def settled():
                    nonlocal next_reconcile
                    if not budgets.state(conn, budget)["reserved"]:
                        return True
                    if time.monotonic() >= next_reconcile:
                        roots.reconcile_meter(tid)
                        next_reconcile = time.monotonic() + 0.25
                    return False
                until(roots, settled)
                # As in the warm dispatch, let the driver finish waking the root before the peer can issue calls.
                peer.gate.clear()
                restored = roots.dispatch(task)
                peer.gate.set()
                try:
                    until(roots, lambda: peer.child_done.is_set())
                except ChildProbeTimeout:
                    if role_profile is None:
                        raise
                    # Keep the actual wire catalogs and callbacks from an unsuccessful role probe.
                tasks["after"] = counts()
            has = lambda phase, label, success: any(c["phase"] == phase and c["thread"] == child
                and f"-{label}-" in c["call"] and c["success"] == success for c in callbacks)
            def denied(thread, phase, reason, label=""):
                matches = [c for c in callbacks if c["thread"] == thread and c["phase"] == phase
                           and label in c["call"]]
                return all(not c["success"] for c in matches) and any(reason in (c["error"] or "") for c in matches)
            verdict = {"warm_retry": has("warm", "source", False), "warm_success": has("warm", "source", True),
                       "native_child_inherited_read": child_meta.get("multi_agent_version") == "v2" and
                                                      "gx_read_artifact" in inherited,
                       "cold_success": has("cold", "source", True),
                       "cold_child_finished": peer.child_done.is_set(),
                       "original_in_child_input": all(any(p == phase and c for p,c,k in peer.model_reads)
                                                      for phase in ("warm", "cold")),
                       "unrelated_artifact_denied": denied(child, "warm", "not been cited", "-other-"),
                       "child_write_denied": denied(child, "warm", "only the assigned GX root", "-write-"),
                       "expired_parent_denied": denied(child, "cold", "LEASE_EXPIRED", "-expired-"),
                       "ordinary_fork_denied": denied(fork["id"], "fork", "ordinary Codex fork"),
                       "other_run_root_denied": denied(other_id, "other_root", "another GX run"),
                       "same_task_lease": tasks["before"] == tasks["after"] == {"tasks": 1, "leases": 1}
                                          and restored["lease_id"] == lease}
            if drop_cold_start:
                verdict["cold_retry"] = bool(dropped) and has("cold", "source", False)
            else:
                verdict["cold_start_delivered"] = bool(cold_started) and not dropped
            if drop_spawn:
                verdict["missed_spawn_recovered"] = bool(lost_spawn) and verdict["warm_success"]
            if role_profile is not None:
                verdict["child_inherited_assigned_catalog"] = set(inherited) == set(role_profile["tools"])
                verdict["child_inherited_submit_namespace"] = ("gx", "gx_submit_product") in inherited_calls
                for child_flag, label in ((False, "root"), (True, "child")):
                    first = next(row for row in peer.rows if row["scenario"] == "warm" and row["child"] == child_flag)
                    verdict[label + "_wire_catalog_matches_assignment"] = set(first["gx_catalog"]) == set(role_profile["tools"])
                    verdict[label + "_wire_submit_namespace"] = first["submit_namespace"] == "gx"
            return {"pass": all(verdict.values()), "checks": verdict, "codex_version": version, "binary_sha256": binary_sha,
                    "provider": "scripted loopback Responses", "run_id": rid, "task_id": task,
                    "root_id": tid, "lease_id": lease, "attempt": restored["attempt"], "counts": tasks,
                    "artifact_ref": peer.source, "other_run_id": other, "other_root_id": other_id,
                    "other_artifact_ref": peer.foreign,
                    "input_config": {"multi_agent_v2": False, "multi_agent_v2_dynamic_tools": False},
                    "child_session_meta": {"multi_agent_version": child_meta["multi_agent_version"],
                                           "dynamic_tools": inherited, "id": child_meta["id"],
                                           "parent_thread_id": child_meta["parent_thread_id"],
                                           "session_id": child_meta["session_id"]},
                    "child_metadata": {k: facts.get(k) for k in ("id","parentThreadId","sessionId","forkedFromId","source")},
                    "cold_child_metadata": {k: cold_facts.get(k) for k in ("id","parentThreadId","sessionId","forkedFromId","source")},
                    "fork_metadata": {k: fork.get(k) for k in ("id","parentThreadId","sessionId","forkedFromId","source")},
                    "spawn_notifications_dropped": lost_spawn, "cold_turn_started_notifications": cold_started,
                    "cold_turn_started_notification_dropped": dropped,
                    "callbacks": callbacks, "child_observations": peer.model_reads}
    finally:
        peer.gate.set()
        peer.expired.set()
        peer.child_done.set()
        peer.shutdown()
        peer.server_close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--drop-spawn", action="store_true", help="drop live spawn notifications to exercise saved parent history")
    args = parser.parse_args()
    _proxy_preflight()
    with tempfile.TemporaryDirectory(prefix="gx-child-access-") as tmp:
        result = run(Path(tmp), args.binary or find_binary(), drop_spawn=args.drop_spawn)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    print(json.dumps(result["checks"]))
    raise SystemExit(0 if result["pass"] else 1)
