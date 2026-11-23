"""Crash a public GX process at the SQLite commit of an official, namespaced product call."""

import argparse
from contextlib import closing
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
B1 = "0db83d7eca05c7503afd2ff7f894a5758b38590d"
EXIT_AFTER_COMMIT = 79


def now():
    return datetime.now(timezone.utc).isoformat()


def sha(path):
    with path.open("rb") as src:
        return hashlib.file_digest(src, "sha256").hexdigest()


def rows(conn, sql, *args):
    return [dict(r) for r in conn.execute(sql, args)]


def snapshot(state, thread=None):
    with closing(sqlite3.connect((state / "gx.db").as_uri() + "?mode=ro", uri=True)) as c:
        c.row_factory = sqlite3.Row
        c.execute("BEGIN")
        terminal = rows(
            c,
            "SELECT e.*,r.task_id FROM p0_root_terminals e JOIN p0_roots r USING(thread_id) "
            "WHERE e.thread_id=coalesce(?,e.thread_id) ORDER BY e.rowid",
            thread,
        )
        assert len(terminal) == 1, terminal
        end = terminal[0]
        (sub,) = rows(
            c,
            "SELECT submission_id,result,lease_id,attempt,content_sha256,artifact_refs_json,"
            "records_artifact_ref,validation_json FROM submissions WHERE task_id=?",
            end["task_id"],
        )
        (task,) = rows(
            c,
            "SELECT task_id,status,attempt,attempts_consumed,lease_id FROM tasks WHERE task_id=?",
            end["task_id"],
        )
        (lease,) = rows(
            c,
            "SELECT lease_id,attempt,principal,released_at FROM leases WHERE lease_id=?",
            sub["lease_id"],
        )
        (call,) = rows(
            c,
            "SELECT call_id,turn_id,request_sha,result_json FROM p0_root_calls WHERE thread_id=? AND call_id=?",
            end["thread_id"],
            end["call_id"],
        )
        assert json.loads(call["result_json"])["submission_id"] == sub["submission_id"]
        assets = []
        for ref in [*json.loads(sub["artifact_refs_json"]), sub["records_artifact_ref"]]:
            (art,) = rows(
                c,
                "SELECT artifact_ref,namespace,rel_path,kind,bytes,sha256 FROM artifacts WHERE artifact_ref=?",
                ref,
            )
            path = state / "artifacts" / art["namespace"] / art["rel_path"]
            assert path.stat().st_size == art["bytes"] and sha(path) == art["sha256"]
            assets.append(art)
        (meter,) = rows(
            c,
            "SELECT reservation_ref,budget_status,terminal_status,measured_tokens,settled_tokens,confidence,"
            "estimated_at,metering_reason FROM p0_meter_native_turns WHERE thread_id=? AND turn_id=?",
            end["thread_id"],
            end["turn_id"],
        )
        charges = rows(
            c,
            "SELECT event_id,json_extract(payload_json,'$.actual.tokens') AS tokens FROM events "
            "WHERE kind='budget.settled' AND json_extract(payload_json,'$.reservation_ref')=?",
            meter["reservation_ref"],
        )
        return {
            "terminal": end,
            "task": task,
            "lease": lease,
            "submission": sub,
            "call": call,
            "artifacts": assets,
            "meter": meter,
            "settlement_events": charges,
            "jobs": rows(c, "SELECT job_id,status,scheduler_ref FROM jobs ORDER BY job_id"),
            "acceptance_outbox": rows(
                c,
                "SELECT outbox_id FROM outbox WHERE topic='submission.accepted' AND "
                "json_extract(payload_json,'$.task_id')=?",
                end["task_id"],
            ),
            "root_failures": rows(
                c,
                "SELECT settlement_id,kind FROM science_settlements WHERE task_id=? AND "
                "submission_id IS NULL AND kind IN ('agent_execution_failure','infrastructure_failure')",
                end["task_id"],
            ),
            "metered_responses": rows(
                c,
                "SELECT * FROM p0_meter_responses WHERE thread_id=? AND turn_id=? ORDER BY rowid",
                end["thread_id"],
                end["turn_id"],
            ),
        }


def recorder(trace, phase):
    def save(kind, **fields):
        with trace.open("a") as file:
            file.write(
                json.dumps({"at": now(), "phase": phase, "type": kind, **fields}, sort_keys=True) + "\n"
            )

    return save


def observe_wire(save):
    from gx_core.hostobs.app_server_client import StdioTransport

    outgoing, incoming = StdioTransport.write_line, StdioTransport.read_line
    requests, callback_ids = {}, {}
    methods = {
        "thread/read",
        "thread/resume",
        "thread/start",
        "turn/start",
        "turn/interrupt",
        "thread/goal/get",
        "thread/goal/set",
        "thread/turns/list",
        "thread/items/list",
    }

    def select(p):
        return {
            k: p[k] for k in ("threadId", "turnId", "callId", "namespace", "tool", "status") if k in p
        }

    def write(transport, line, timeout):
        m = json.loads(line)
        method = m.get("method")
        p = m.get("params") or {}
        if method in methods:
            requests[m["id"]] = method
            params = select(p)
            if method == "thread/resume":
                params["goals_enabled"] = (p.get("config") or {}).get("features.goals")
            save("rpc_sent", method=method, id=m["id"], pid=transport.process.pid, params=params)
        elif "method" not in m and m.get("id") in callback_ids:
            save("callback_reply_written", id=m["id"], call=callback_ids[m["id"]])
        return outgoing(transport, line, timeout)

    def read(transport, timeout):
        line = incoming(transport, timeout)
        m = json.loads(line)
        method = m.get("method")
        p = m.get("params") or {}
        if method == "item/tool/call":
            callback_ids[m["id"]] = p["callId"]
            save("dynamic_call", id=m["id"], params=select(p))
        elif method in (
            "turn/started",
            "turn/completed",
            "thread/tokenUsage/updated",
            "rawResponse/completed",
        ):
            turn = p.get("turn") or {}
            save(
                "observable",
                method=method,
                params=select(p),
                turn_status=turn.get("status"),
                turn=turn.get("id"),
                usage=p.get("tokenUsage") or p.get("usage"),
            )
        elif not method and m.get("id") in requests:
            target = requests.pop(m["id"])
            out = m.get("result") or {}
            fields = {}
            if target in ("thread/resume", "thread/read"):
                t = out.get("thread") or {}
                fields = {
                    "thread": t.get("id"),
                    "thread_status": t.get("status"),
                    "turns": [(v.get("id"), v.get("status")) for v in t.get("turns", [])],
                }
            elif target == "thread/turns/list":
                fields = {"turns": [(v["id"], v["status"]) for v in out.get("data", [])]}
            elif target in ("thread/goal/get", "thread/goal/set"):
                fields = {"goal_status": (out.get("goal") or {}).get("status")}
            if m.get("error"):
                fields["error"] = (m["error"].get("code"), m["error"].get("message"))
            save("rpc_returned", method=target, id=m["id"], **fields)
        return line

    return patch.object(StdioTransport, "write_line", write), patch.object(
        StdioTransport, "read_line", read
    )


def worker(args):
    sys.path.insert(0, str(args.install / "gx-h"))
    from gx_core.native_release import native_state
    from gx_core.p0.driver import Driver
    from gx_core.p0.terminal_products import pending
    from gx_core.persistence import store

    phase = args.worker
    save = recorder(args.trace, phase)
    traces = observe_wire(save)
    with traces[0], traces[1]:
        if phase == "cold":
            if args.public_only:
                sessions = str(Path(os.environ["CODEX_HOME"]).resolve() / "sessions") + os.sep

                def no_private_rollout(event, arguments):
                    if event == "open" and isinstance(arguments[0], (str, bytes)):
                        path = os.path.abspath(os.fsdecode(arguments[0]))
                        if path.startswith(sessions) and path.endswith(".jsonl"):
                            save("disallowed_gx_rollout_open", name=Path(path).name)
                            raise PermissionError(
                                "the public-only fixture denies private native rollout reads"
                            )

                sys.addaudithook(no_private_rollout)
            with native_state(args.state), closing(store.open_store(args.state / "gx.db")) as c:
                rid = c.execute("SELECT run_id FROM runs").fetchone()[0]
                d = Driver(c, args.state, rid)
                try:
                    d.reconcile_costs()
                    stop = time.monotonic() + 5
                    while time.monotonic() < stop:
                        d.native.poll(0.15)
                        if (
                            not pending(c, rid)
                            and not c.execute(
                                "SELECT 1 FROM p0_meter_native_turns WHERE budget_status='HELD'"
                            ).fetchone()
                        ):
                            break
                    d.reconcile_costs()
                finally:
                    d.close()
            return
        sqlite_connect = sqlite3.connect

        class KillCommitted(sqlite3.Connection):
            def execute(self, sql, *a):
                result = super().execute(sql, *a)
                if sql == "COMMIT" and not self.in_transaction:
                    if (
                        super()
                        .execute(
                            "SELECT 1 FROM sqlite_schema WHERE type='table' AND name='p0_root_terminals'"
                        )
                        .fetchone()
                    ):
                        terminal = (
                            super()
                            .execute(
                                "SELECT thread_id,turn_id,call_id FROM p0_root_terminals WHERE reason='GX_ACCEPTED' AND goal_checked_at IS NULL"
                            )
                            .fetchone()
                        )
                        if terminal:
                            save(
                                "sqlite_commit_returned",
                                thread=terminal[0],
                                turn=terminal[1],
                                call=terminal[2],
                                gx_pid=os.getpid(),
                            )
                            os._exit(EXIT_AFTER_COMMIT)
                return result

        def connect(*a, **kw):
            return sqlite_connect(*a, **kw, factory=KillCommitted)

        with (
            patch.object(sqlite3, "connect", connect)
            if phase == "crash"
            else patch.object(sqlite3, "connect", sqlite_connect)
        ):
            tool = args.install / "bin/gx-p0"
            sys.argv = [str(tool), "run", "--state", str(args.state), "--timeout", "75"]
            runpy.run_path(str(tool), run_name="__main__")


def run(args, result):
    sys.path[:0] = [str(ROOT / "gx-h"), str(ROOT / "acceptance"), str(ROOT / "acceptance/v31")]
    import public_grid as grid
    import e2e_responses as peer
    from run_workload import specification, OFFICIAL_BINARY_SHA256
    from v21_staging import stage_source

    work = args.work.resolve()
    work.mkdir(parents=True, exist_ok=False)
    install, state, trace = work / "install", work / "state", work / "wire.jsonl"
    commit = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.commit + "^{commit}"], cwd=ROOT, text=True
    ).strip()
    result["build"] = stage_source(work, install, commit)
    result["patches"] = []
    for overlay in args.patch:
        subprocess.run(["git", "apply", str(overlay.resolve())], cwd=install, check=True)
        result["patches"].append({"name": overlay.name, "sha256": sha(overlay)})
    audited = (
        "p0/roots.py",
        "p0/driver.py",
        "p0/terminal_products.py",
        "p0/root_delivery.py",
        "p0/metering.py",
        "hostobs/app_server_client.py",
    )
    result["audited_sources_sha256"] = {name: sha(install / "gx-h/gx_core" / name) for name in audited}
    binary = ROOT / "vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex"
    assert sha(binary) == OFFICIAL_BINARY_SHA256
    result.update(
        binary_sha256=OFFICIAL_BINARY_SHA256,
        version=subprocess.check_output([binary, "--version"], text=True).strip(),
        test_sha256=sha(Path(__file__)),
        setup="localhost scripted Responses; official, unmodified Codex binary and Git-exported GX",
        phase_commands=[],
    )
    # As a subreaper this test can collect the real orphaned stdio host (not signal or replace it).
    libc = ctypes.CDLL(None, use_errno=True)
    assert libc.prctl(36, 1, 0, 0, 0) == 0, ctypes.get_errno()
    spec = specification(install, 0, "east")
    spec.update(workflow_version="3", plugins=["org.gx.science-grid@3.0.0"])
    spec["proposal"]["requires_interpretation"] = True
    if args.public_only:
        spec["metering"] = {**spec.get("metering", {}), "rollout_diagnostics": False}
    result["public_only"] = args.public_only
    spec_file = work / "spec.json"
    spec_file.write_text(json.dumps(spec))
    model = grid.ReviewerPeer()
    phase_observed = ["start"]

    def provider(body, ident):
        if phase_observed[0] == "cold":
            # Any model contact during a reconcile-only phase is a regression. Return a valid
            # neutral response so a fixture error cannot conceal its eventual cost or history.
            event = {
                "type": "message",
                "id": "m-" + ident,
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "The existing assessment is already recorded."}
                ],
            }
            item = {
                "phase": "cold",
                "input_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
            }
            try:
                brief = peer.brief_from(body)
                item.update(task_id=brief["task_id"], output_kind=brief["input"]["output_kind"])
            except ValueError:
                pass
            return event, item
        event, detail = model(body, ident)
        return event, {**detail, "phase": phase_observed[0]}

    with peer.local_responses(work / "requests.jsonl") as server, patch.object(
        peer, "fixture_action", provider
    ):
        env = grid.fixture_env(work, server, str(binary))
        env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env["PATH"]

        def run_phase(phase):
            phase_observed[0] = phase
            if phase == "start":
                argv = [
                    sys.executable,
                    str(install / "bin/gx-p0"),
                    "start",
                    "--state",
                    str(state),
                    "--spec",
                    str(spec_file),
                    "--run-key",
                    "east",
                ]
            else:
                argv = [
                    sys.executable,
                    str(Path(__file__).resolve()),
                    "--worker",
                    phase,
                    "--state",
                    str(state),
                    "--install",
                    str(install),
                    "--trace",
                    str(trace),
                ]
            if args.public_only and phase != "start":
                argv.append("--public-only")
            before = server.request_count
            proc = subprocess.run(
                argv, env=env, cwd=install, capture_output=True, text=True, timeout=110
            )
            result["phase_commands"].append(
                {"phase": phase, "exit": proc.returncode, "requests": server.request_count - before}
            )
            assert proc.returncode == (EXIT_AFTER_COMMIT if phase == "crash" else 0), (
                phase,
                proc.returncode,
                proc.stdout[-2200:],
                proc.stderr[-2200:],
                server.rows[-1:],
            )
            return json.loads(proc.stdout) if proc.stdout else None

        def wire():
            return [json.loads(line) for line in trace.read_text().splitlines()]

        run_phase("start")
        run_phase("crash")
        cut = snapshot(state)
        result["after_crash"] = cut
        end = cut["terminal"]
        thread = end["thread_id"]
        turn = end["turn_id"]
        assert (
            end["reason"] == "GX_ACCEPTED"
            and end["goal_checked_at"] is None
            and end["interrupt_attempts"] == 0
            and not cut["root_failures"]
        )
        assert (
            cut["meter"]["budget_status"] == "HELD"
            and len(cut["acceptance_outbox"]) == 1
            and len(cut["jobs"]) == 2
        )
        assert len({r["job_id"] for r in cut["jobs"] if r["status"] == "SUCCEEDED"}) == 2
        assert (
            cut["lease"]["released_at"]
            and cut["lease"]["principal"] == thread
            and cut["task"]["status"] == "ACCEPTED"
        )
        transmitted = [r for r in wire() if r["type"] == "rpc_sent"]
        (pid,) = {r["pid"] for r in transmitted}
        until = time.monotonic() + 25
        sleep_start = time.monotonic()
        while True:
            actual, status = os.waitpid(pid, os.WNOHANG)
            if actual == pid:
                break
            if time.monotonic() > until:
                raise RuntimeError(f"original stdio host {pid} remained live after GX exit")
            time.sleep(0.03)
        result["orphan_stdio_exit"] = {
            "pid": pid,
            "exit_code": os.waitstatus_to_exitcode(status),
            "observed_after_s": round(time.monotonic() - sleep_start, 5),
        }
        assert result["orphan_stdio_exit"]["exit_code"] == 0
        run_phase("cold")
        cold = snapshot(state, thread)
        result["after_cold_owner"] = cold
        cold_posts = result["phase_commands"][-1]["requests"]
        result["cold_model_requests"] = cold_posts
        for k in (
            "task",
            "lease",
            "call",
            "artifacts",
            "submission",
            "jobs",
            "acceptance_outbox",
            "root_failures",
        ):
            assert cut[k] == cold[k], k
        assert cold["terminal"]["goal_checked_at"] is not None and cold["meter"]["terminal_status"] in (
            "interrupted",
            "completed",
        )
        result["cold_history"] = [
            r
            for r in wire()
            if r["phase"] == "cold"
            and r["type"] == "rpc_returned"
            and r["method"]
            in (
                "thread/resume",
                "thread/read",
                "thread/turns/list",
                "thread/goal/get",
                "thread/goal/set",
            )
        ]
        assert any(
            (turn, cold["meter"]["terminal_status"]) in map(tuple, r.get("turns", []))
            for r in result["cold_history"]
        )
        if args.public_only:
            evidence = [r for r in wire() if r["phase"] == "cold"]
            assert not any(r["type"] == "disallowed_gx_rollout_open" for r in evidence)
            confirmed_goal = False
            quiet_resumes = []
            for record in evidence:
                if record["type"] == "rpc_returned" and record.get("method") in (
                    "thread/goal/get",
                    "thread/goal/set",
                ):
                    confirmed_goal = record.get("goal_status") in ("complete", "budgetLimited")
                if record["type"] == "rpc_sent" and record.get("method") == "thread/resume":
                    assert confirmed_goal and record["params"]["goals_enabled"] is False, record
                    quiet_resumes.append(record["id"])
            snapshots = [
                r["usage"]["total"]["totalTokens"]
                for r in evidence
                if r["type"] == "observable"
                and r.get("method") == "thread/tokenUsage/updated"
                and r["params"].get("turnId") == turn
            ]
            assert quiet_resumes and snapshots
            result["cold_public_evidence"] = {
                "guarded_resume_ids": quiet_resumes,
                "exact_turn_cumulative": snapshots,
                "gx_private_rollout_opens": 0,
            }
        final = run_phase("finish")
        suffix = snapshot(state, thread)
        result["after_full_project"] = suffix
        result["final_status"] = final["status"]
        result["report_ref"] = final["report_ref"]
        assert final["status"] == "COMPLETED" and final["report_ref"] and not suffix["root_failures"]
        for k in ("task", "lease", "call", "artifacts", "submission", "jobs", "acceptance_outbox"):
            assert cut[k] == suffix[k], k
        assert not [
            r for r in wire() if r["type"] == "callback_reply_written" and r["call"] == end["call_id"]
        ]
        own = [r for r in server.rows if r.get("task_id") == end["task_id"]]
        assert not any("error" in r for r in server.rows)
        original = [r for r in own if r["phase"] == "crash"]
        assert original[-1]["id"] == end["call_id"].removeprefix("call-")
        finish_same_task = [r for r in own if r["phase"] == "finish"]
        assert not finish_same_task, finish_same_task
        result.update(
            unexpected_finish_contacts_for_original_task=finish_same_task,
            original_task_requests=len(original),
            original_provider_tokens=35 * len(original),
            original_postcommit_requests=cold_posts,
            original_terminal_callback_replies=0,
            provider=server.rows,
            wire=wire(),
        )
        if suffix["meter"]["confidence"] == "PROVIDER_CONFIRMED":
            assert (
                suffix["meter"]["settled_tokens"] == 35 * len(original)
                and len(suffix["settlement_events"]) == 1
            )
        else:
            assert suffix["meter"]["confidence"] in ("ESTIMATED", "RESERVED")
        result["zero_postcommit_requests"] = cold_posts == 0
        result["exact_original_settlement"] = (
            suffix["meter"]["confidence"] == "PROVIDER_CONFIRMED"
            and suffix["meter"]["settled_tokens"] == 35 * len(original)
            and len(suffix["settlement_events"]) == 1
        )
        result["expectation"] = args.expect
        if args.expect == "regression":
            assert cold_posts > 0, "The frozen adverse path was not reproduced."
        elif args.expect == "passive":
            assert cold_posts == 0
        else:
            assert cold_posts == 0 and result["exact_original_settlement"], (cold_posts, suffix["meter"])
        result["success"] = True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", default=B1)
    parser.add_argument("--public-only", action="store_true")
    parser.add_argument("--patch", action="append", type=Path, default=[])
    parser.add_argument("--expect", choices=("corrected", "regression", "passive"), default="corrected")
    parser.add_argument("--worker", choices=("crash", "cold", "finish"))
    for name in ("install", "state", "trace", "work", "out"):
        parser.add_argument("--" + name, type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args)
    else:
        assert args.work and args.out
        result = {"started_utc": now(), "success": False}
        try:
            run(args, result)
        except BaseException as exc:
            result["error"] = repr(exc)
            raise
        finally:
            for key, filename in (("wire", "wire.jsonl"), ("provider", "requests.jsonl")):
                path = args.work / filename
                if path.is_file():
                    result[key] = [json.loads(line) for line in path.read_text().splitlines()]
            result["finished_utc"] = now()
            args.out.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
