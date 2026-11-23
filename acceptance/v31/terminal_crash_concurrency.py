"""Exercise v3.1's submission durability and competing repair writers on independent SQLite clients.

The native transport here records frames in memory. The pinned-binary cancellation,
response and child-usage evidence is collected separately by terminal_protocol_probe.py.
"""

import argparse
from contextlib import closing
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "gx-h"), str(ROOT / "acceptance/v3/performance")]
from gx_core.artifacts import ArtifactService
from gx_core.hostobs.app_server_client import AppServerClient, ReceivedMessage
from gx_core.jobs.service import JobService
from gx_core.native_release import native_state
from gx_core.orchestration import reactor, service
from gx_core.p0 import cli, driver, intent, roots, terminal_products
from gx_core.persistence import records, store
from gx_core.teams.service import TeamService, backend_ctx
from run_workload import specification


class Wire:
    def __init__(self):
        self.frames = []

    def start(self):
        pass

    def close(self):
        pass

    def write_line(self, line, timeout):
        self.frames.append(json.loads(line))


def owner(conn, db_path, run_id):
    tools = service.hooks_for_run(conn, run_id).build_worker_tools(conn, run_id)
    return roots.Roots(
        conn,
        run_id=run_id,
        cwd=db_path.parent,
        token_budget=180000,
        worker_tools=tools.specs,
        worker_call=tools.call,
        worker_prepare=tools.prepare,
    )


def make_seed(directory):
    spec = specification(ROOT, 0, "east")
    spec.update(workflow_version="3", plugins=["org.gx.science-grid@3.0.0"])
    spec["proposal"]["requires_interpretation"] = True
    with native_state(directory) as state, closing(store.open_store(state.db_path)) as conn:
        rid = cli.start(conn, state.directory, spec, run_key="terminal-crash-audit")["run_id"]
        jobs = [r[0] for r in conn.execute("SELECT job_id FROM jobs ORDER BY job_id")]
        assert len(jobs) == 2
        for jid in jobs:
            assert JobService(conn).wait(jid, 40)["terminal"]
        reactor.lazy(backend_ctx(conn), rid)
        reactor.drive_science(backend_ctx(conn), rid)
        (task,) = conn.execute(
            "SELECT * FROM tasks WHERE run_id=? AND status='READY' AND role='evidence_reviewer'", (rid,)
        )
        native = owner(conn, state.db_path, rid)
        with patch("gx_core.p0.roots.tempfile.gettempdir", return_value=str(directory)):
            claimed = native._claim(task["task_id"])
        assert claimed["attempt"] == 1
        thread, turn = "crash-probe-reviewer", "crash-probe-turn"
        native._bind(
            claimed, {"id": thread, "sessionId": thread, "cwd": claimed["cwd"], "source": "appServer"}
        )
        inputs = json.loads(task["input_json"])
        evidence = ArtifactService(conn).read_json(inputs["evidence_refs"][0])
        product = {"findings_by_path": {}}
        for route in evidence["routes"]:
            evaluation = ArtifactService(conn).read_json(route["evaluation_ref"])
            product["findings_by_path"][route["path_id"]] = {
                "reason": f"The measured maximum score {evaluation['max_score']} tests this finite coordinate set.",
                "risk": "A finite grid cannot support extrapolation to unmeasured coordinates.",
            }
        return {
            "run_id": rid,
            "task": task["task_id"],
            "lease": claimed["lease_id"],
            "thread": thread,
            "turn": turn,
            "product": product,
            "job_ids": jobs,
            "workflow_sha256": native.run["workflow_sha256"],
        }


def envelope(f, call, valid=True, *, thread=None, turn=None):
    product = f["product"] if valid else {"findings_by_path": {}}
    return ReceivedMessage(
        1,
        {
            "id": 123,
            "method": "item/tool/call",
            "params": {
                "threadId": thread or f["thread"],
                "turnId": turn or f["turn"],
                "callId": call,
                "namespace": "gx",
                "tool": "gx_submit_product",
                "arguments": {"product": product},
            },
        },
    )


def callback(native, received):
    wire = Wire()
    client = AppServerClient(transport=wire, on_server_request=native._callback).start()
    client._deliver_callback(received, time.monotonic() + 10)
    if wire.frames:
        (frame,) = wire.frames
        reply = frame["result"]
        return {
            "sent": True,
            "success": reply["success"],
            "body": json.loads(reply["contentItems"][0]["text"]),
        }
    return {"sent": False}


def facts(db, f):
    with closing(store.open_store(db)) as c:

        def rows(q, *args):
            return [dict(r) for r in c.execute(q, args)]

        artifacts = rows(
            "SELECT artifact_ref,namespace,rel_path,sha256,kind FROM artifacts WHERE task_id=? ORDER BY rowid",
            f["task"],
        )
        for art in artifacts:
            assert (
                hashlib.sha256(
                    (db.parent / "artifacts" / art["namespace"] / art["rel_path"]).read_bytes()
                ).hexdigest()
                == art["sha256"]
            )
        assert [
            r[0] for r in c.execute("SELECT job_id FROM jobs WHERE status='SUCCEEDED' ORDER BY job_id")
        ] == f["job_ids"]
        return {
            "task": rows(
                "SELECT status,attempt,attempts_consumed,lease_id FROM tasks WHERE task_id=?", f["task"]
            )[0],
            "repairs": rows(
                "SELECT call_id,request_sha,category,contract_digest,response_json FROM p0_product_repairs WHERE thread_id=? ORDER BY rowid",
                f["thread"],
            ),
            "terminals": rows(
                "SELECT thread_id,turn_id,call_id,reason,goal_checked_at,interrupt_ack_at FROM p0_root_terminals WHERE thread_id=?",
                f["thread"],
            ),
            "submissions": rows(
                "SELECT submission_id,result,attempt,lease_id,content_sha256,records_artifact_ref,artifact_refs_json FROM submissions WHERE task_id=? ORDER BY rowid",
                f["task"],
            ),
            "calls": rows(
                "SELECT call_id,turn_id,request_sha FROM p0_root_calls WHERE thread_id=? ORDER BY rowid",
                f["thread"],
            ),
            "artifacts": artifacts,
            "lease": rows("SELECT principal,released_at FROM leases WHERE lease_id=?", f["lease"])[0],
            "outbox_count": c.execute(
                "SELECT count(*) FROM outbox WHERE topic='submission.accepted' AND json_extract(payload_json,'$.task_id')=?",
                (f["task"],),
            ).fetchone()[0],
            "formal_settlements": rows(
                "SELECT settlement_id,kind,submission_id FROM science_settlements WHERE task_id=? AND submission_id IS NOT NULL ORDER BY rowid",
                f["task"],
            ),
            "unwanted_failures": c.execute(
                "SELECT count(*) FROM science_settlements WHERE task_id=? AND submission_id IS NULL AND kind IN ('infrastructure_failure','agent_execution_failure')",
                (f["task"],),
            ).fetchone()[0],
        }


def attempt(path, f, call, valid, send, at_rollback=None, release=None, die_after_commit=False):
    from gx_core.p0.terminal_products import TerminalProducts

    class DieAtCommit(sqlite3.Connection):
        def execute(self, sql, *args):
            result = super().execute(sql, *args)
            if (
                sql == "COMMIT"
                and not self.in_transaction
                and super()
                .execute(
                    "SELECT 1 FROM p0_root_terminals WHERE thread_id=? AND call_id=?",
                    (f["thread"], call),
                )
                .fetchone()
            ):
                # SQLite finished, but the context manager has not run its after-commit hooks or returned a native frame.
                os._exit(79)
            return result

    real_connect = sqlite3.connect
    try:
        if die_after_commit:
            sqlite3.connect = lambda *a, **kw: real_connect(*a, **kw, factory=DieAtCommit)
        with closing(store.open_store(path)) as conn:
            n = owner(conn, path, f["run_id"])
            n.active_turns[f["thread"]] = f["turn"]
            if at_rollback is not None:
                original = TerminalProducts.failed_call

                def after_rollback(this, params, error):
                    assert not conn.in_transaction
                    at_rollback.set()
                    assert release.wait(30), "writer release timed out"
                    return original(this, params, error)

                TerminalProducts.failed_call = after_rollback
            send.send(callback(n, envelope(f, call, valid)))
    except BaseException as exc:
        send.send({"exception": type(exc).__name__, "message": str(exc)})
        raise
    finally:
        send.close()
        sqlite3.connect = real_connect


def start(ctx, path, f, call, valid, *, wait=False, die=False):
    receiver, sender = ctx.Pipe(duplex=False)
    reached, release = (ctx.Event(), ctx.Event()) if wait else (None, None)
    process = ctx.Process(target=attempt, args=(path, f, call, valid, sender, reached, release, die))
    process.start()
    sender.close()
    return process, receiver, reached, release


def finish(worker):
    proc, receiver = worker[:2]
    proc.join(40)
    if proc.is_alive():
        proc.kill()
        proc.join(10)
        raise RuntimeError("bounded test writer exceeded 40 seconds")
    try:
        result = receiver.recv() if receiver.poll() else None
    except EOFError:
        result = None
    finally:
        receiver.close()
    return proc.exitcode, result


def direct(path, f, call, valid=True, **kw):
    with closing(store.open_store(path)) as c:
        n = owner(c, path, f["run_id"])
        n.active_turns[f["thread"]] = f["turn"]
        return callback(n, envelope(f, call, valid, **kw))


def run():
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="gx-v31-terminal-audit-") as temp:
        work = Path(temp)
        seed = work / "seed"
        f = make_seed(seed)

        def clone(label):
            directory = work / label
            shutil.copytree(seed, directory)
            return directory / "gx.db"

        base = facts(seed / "gx.db", f)
        db = clone("post-commit")
        code, reply = finish(start(ctx, db, f, "accepted", True, die=True))
        assert (code, reply) == (79, None), (code, reply)
        cut = facts(db, f)
        assert cut["task"]["status"] == "ACCEPTED" and cut["lease"]["released_at"]
        assert (
            len(cut["submissions"])
            == len(cut["calls"])
            == len(cut["terminals"])
            == cut["outbox_count"]
            == 1
        )
        assert (
            cut["terminals"][0]["reason"] == "GX_ACCEPTED"
            and cut["terminals"][0]["goal_checked_at"] is None
        )
        assert cut["unwanted_failures"] == 0
        first = direct(db, f, "accepted")
        assert first == {"sent": False} and facts(db, f) == cut
        with closing(store.open_store(db)) as c:
            dispatch = driver.Driver(c, db.parent, f["run_id"])
            n = owner(c, db, f["run_id"])
            dispatch._native = n
            assert not c.execute(
                "SELECT 1 FROM p0_root_deliveries d JOIN budget_reservations b USING(reservation_ref) WHERE d.run_id=? AND b.status='HELD'",
                (f["run_id"],),
            ).fetchone()
            terminal = n.terminal_products
            assert terminal.due == {(f["thread"], f["turn"]): 0}
            calls = []
            status = "inProgress"

            def scripted(method, params, **kwargs):
                assert not c.in_transaction and params["threadId"] == f["thread"]
                calls.append(
                    {
                        "method": method,
                        "thread_id": params["threadId"],
                        **({"turn_id": params["turnId"]} if "turnId" in params else {}),
                    }
                )
                if method == "thread/resume":
                    b = n._binding(f["thread"])
                    return {
                        "modelProvider": b["provider"],
                        "model": b["model"],
                        "reasoningEffort": b["effort"],
                        "sandbox": {"type": "readOnly", "networkAccess": False},
                        "approvalPolicy": "never",
                        "thread": {
                            "id": f["thread"],
                            "sessionId": f["thread"],
                            "source": "appServer",
                            "cwd": b["cwd"],
                            "turns": [{"id": f["turn"], "status": status}],
                        },
                    }
                if method == "thread/goal/get":
                    return {"goal": {"status": "active"}}
                if method == "thread/goal/set":
                    return {"goal": {"status": "complete"}}
                if method == "thread/turns/list":
                    return {"data": [{"id": f["turn"], "status": status}], "nextCursor": None}
                if method == "turn/interrupt":
                    assert params["turnId"] == f["turn"]
                    return {}
                raise AssertionError(method)

            n._request = scripted
            terminal.retry_seconds = 0
            dispatch.reconcile_costs()
            assert calls[0]["method"] == "thread/resume" and "thread/turns/list" in [
                x["method"] for x in calls
            ]
            assert terminal.due and calls[-1]["method"] == "turn/interrupt"
            ack = facts(db, f)
            assert ack["terminals"][0]["interrupt_ack_at"]
            status = "interrupted"
            dispatch.reconcile_costs()
            assert not terminal.due
            assert list(terminal_products.pending(c, f["run_id"])) == []
            settled = facts(db, f)
            assert (cut["submissions"], cut["calls"], cut["outbox_count"], cut["artifacts"]) == (
                settled["submissions"],
                settled["calls"],
                settled["outbox_count"],
                settled["artifacts"],
            )
        db = clone("terminal-rejection")
        with closing(store.open_store(db)) as c:
            # The formal independent-review check refuses a reviewer also bound as the coordinating principal.
            intent.bind_main(c, f["run_id"], f["thread"])
        rejected_code, rejected_reply = finish(start(ctx, db, f, "rejected", True, die=True))
        assert (rejected_code, rejected_reply) == (79, None), (rejected_code, rejected_reply)
        rejected = facts(db, f)
        assert rejected["terminals"][0]["reason"] == "GX_TERMINAL_REJECTED"
        assert len(rejected["submissions"]) == len(rejected["calls"]) == len(rejected["terminals"]) == 1
        assert (
            rejected["submissions"][0]["result"] == "REJECTED"
            and not rejected["repairs"]
            and not rejected["unwanted_failures"]
        ), rejected
        assert (
            len(rejected["formal_settlements"]) == 1
            and rejected["formal_settlements"][0]["submission_id"]
            == rejected["submissions"][0]["submission_id"]
        )
        assert direct(db, f, "rejected") == {"sent": False} and facts(db, f) == rejected
        with closing(store.open_store(db)) as c:
            verdict = ArtifactService(c).read_json(rejected["submissions"][0]["records_artifact_ref"])
            assert any("independent" in r["message"] for r in verdict["records"])
            rejected_fallback = list(map(dict, terminal_products.pending(c, f["run_id"])))
            assert rejected_fallback == [{"thread_id": f["thread"], "turn_id": f["turn"]}]
        db = clone("duplicate-failure")
        one = start(ctx, db, f, "bad", False, wait=True)
        two = start(ctx, db, f, "bad", False, wait=True)
        assert one[2].wait(25) and two[2].wait(25)
        one[3].set()
        two[3].set()
        results = [finish(one), finish(two)]
        assert all(rc == 0 for rc, _ in results) and results[0][1] == results[1][1], results
        bad = results[0][1]
        assert bad["sent"] and not bad["success"] and bad["body"]["failure_count"] == 1
        accounted = facts(db, f)
        assert (
            accounted["task"] == base["task"]
            and accounted["artifacts"] == base["artifacts"]
            and len(accounted["repairs"]) == 1
        )
        assert direct(db, f, "bad", True)["success"] is False and facts(db, f) == accounted
        assert direct(db, f, "bad", False) == bad and facts(db, f) == accounted
        assert direct(db, f, "corrected", True) == {"sent": False}
        corrected = facts(db, f)
        assert (
            len(corrected["submissions"]) == len(corrected["repairs"]) == corrected["outbox_count"] == 1
            and corrected["task"]["attempt"] == 1
        )
        db = clone("stale-gap")
        old = start(ctx, db, f, "delayed-invalid", False, wait=True)
        assert old[2].wait(25)
        assert direct(db, f, "won", True) == {"sent": False}
        winner = facts(db, f)
        old[3].set()
        rc, late = finish(old)
        assert (rc, late) == (0, {"sent": False}), (rc, late)
        assert facts(db, f) == winner and not winner["repairs"]
        db = clone("distinct-failures")
        one = start(ctx, db, f, "bad-1", False, wait=True)
        two = start(ctx, db, f, "bad-2", False, wait=True)
        assert one[2].wait(25) and two[2].wait(25)
        one[3].set()
        two[3].set()
        limits = [finish(one), finish(two)]
        assert all(rc == 0 for rc, _ in limits) and sum(not r["sent"] for _, r in limits) == 1, limits
        capped = facts(db, f)
        assert len(capped["repairs"]) == 2 and capped["terminals"][0]["reason"] == "GX_REPAIR_LIMIT"
        assert not capped["submissions"] and not capped["calls"]
        assert all(a in base["artifacts"] or a["kind"] == "brief" for a in capped["artifacts"]), capped[
            "artifacts"
        ]
        db = clone("retired-lease")
        assert direct(db, f, "first-bad", False)["body"]["failure_count"] == 1
        with closing(store.open_store(db)) as c:
            with store.transaction(c):
                recovered = TeamService(c).recover_failed_attempt(
                    task_id=f["task"], lease_ref=f["lease"], reason="probe lease reassigned"
                )
            assert recovered in ("READY", "BLOCKED")
        after_release = facts(db, f)
        old_lease = direct(db, f, "fresh-using-released-lease", False)
        assert (
            old_lease["sent"] and not old_lease["success"] and "failure_count" not in old_lease["body"]
        )
        assert facts(db, f) == after_release
        db = clone("authorization")
        denied = [
            direct(db, f, "wrong-turn", False, turn="other-turn"),
            direct(db, f, "wrong-thread", False, thread="other-thread"),
        ]
        with closing(store.open_store(db)) as c:
            authorized = owner(c, db, f["run_id"])
            authorized.active_turns[f["thread"]] = f["turn"]
            for namespace in (None, "another"):
                msg = envelope(f, "wrong-namespace", False)
                msg.message["params"]["namespace"] = namespace
                denied.append(callback(authorized, msg))
        assert all(r["sent"] and not r["success"] for r in denied) and facts(db, f) == base
        assert direct(db, f, "bad", False)["body"]["failure_count"] == 1
        bound = facts(db, f)
        with closing(store.open_store(db)) as c:
            with store.transaction(c):
                peer = records.create_run(
                    c,
                    objective="independent scope",
                    project_ref="audit-peer",
                    actor="audit",
                    workflow_id="science_grid",
                    workflow_version="3",
                )
            sibling = roots.Roots(c, run_id=peer, cwd=db.parent)
            peer_denied = callback(sibling, envelope(f, "bad", False))
            assert (
                peer_denied["sent"]
                and not peer_denied["success"]
                and "failure_count" not in peer_denied["body"]
            )
            assert not sibling.terminal_products.is_terminal(f["thread"], f["turn"])
        assert facts(db, f) == bound
        assert direct(db, f, "done", True) == {"sent": False}
        with closing(store.open_store(db)) as c:
            sibling = roots.Roots(c, run_id=peer, cwd=db.parent)
            peer_terminal = callback(sibling, envelope(f, "done", True))
            assert peer_terminal["sent"] and not peer_terminal["success"]
            assert not sibling.terminal_products.is_terminal(f["thread"], f["turn"])
            q = envelope(f, "cached-context", True)
            q.message["params"].pop("namespace")
            q.message["params"].update(tool="gx_task_context", arguments={})
            scoped = owner(c, db, f["run_id"])
            assert callback(scoped, q) == {"sent": False}
        sources = [
            "gx-h/gx_core/p0/roots.py",
            "gx-h/gx_core/p0/driver.py",
            "gx-h/gx_core/p0/root_delivery.py",
            "gx-h/gx_core/p0/terminal_products.py",
            "gx-h/gx_core/hostobs/app_server_client.py",
            "gx-h/gx_core/teams/service.py",
            "gx-h/gx_core/p0/zz_v31_terminal_products.sql",
            "acceptance/v31/terminal_crash_concurrency.py",
        ]
        build = {
            "repository_head": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "audited_worktree_sha256": {
                p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in sources
            },
            "workflow_sha256": f["workflow_sha256"],
            "python": sys.version,
            "sqlite": sqlite3.sqlite_version,
        }
        return {
            "fixture": f,
            "build": build,
            "command": "python -B acceptance/v31/terminal_crash_concurrency.py --output acceptance/v31/terminal-crash-concurrency.json",
            "evidence_scope": "Real two-job local science workflow and SQLite writers; transport and cold history are scripted.",
            "terminal_rejection_crash": {
                "exit_code": rejected_code,
                "wire": rejected_reply,
                "state": rejected,
                "verdict": verdict,
                "pending": rejected_fallback,
            },
            "released_lease_new_call": old_lease,
            "crash": {
                "exit_code": code,
                "wire_reply": reply,
                "separate_reader_cut": cut,
                "duplicate_wire": first,
                "cold_owner_methods": calls,
                "pending_after_ack": bool(ack["terminals"][0]["interrupt_ack_at"]),
                "pending_after_exact_turn_terminal": False,
            },
            "duplicate_invalid_writers": results,
            "corrected": corrected,
            "accepted_while_invalid_post_rollback_was_paused": {
                "late_writer_result": late,
                "repairs": winner["repairs"],
                "accepted_submissions": len(winner["submissions"]),
            },
            "distinct_invalid_writers": {
                "results": limits,
                "task": capped["task"],
                "repairs": capped["repairs"],
                "terminal": capped["terminals"],
            },
            "denied_unowned": {
                "unbound_thread_wrong_turn_or_namespace": denied,
                "unrelated_run_repair": peer_denied,
                "unrelated_run_terminal": peer_terminal,
            },
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    os.environ.update(GX_SCOPE="operator", GX_PROJECTS="*", GX_P0_CODEX="/no-provider-for-sqlite-audit")
    result = run()
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print("PASS", args.output)
