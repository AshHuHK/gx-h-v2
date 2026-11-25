"""Measure a real protected Job across an unrelated GX decision with a synthetic callback."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def clock():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def active_pid(pid, birth):
    stat = (Path("/proc") / str(pid) / "stat").read_text().rsplit(") ", 1)[1].split()
    if stat[0] in ("Z", "X") or stat[19] != birth:
        raise AssertionError(
            "Protected capability is not a live process with its registered birth identity"
        )
    return {"pid": pid, "start_ticks": stat[19], "state": stat[0], "sampled_at": clock()}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--product", type=Path, required=True, help="Frozen GX product checkout")
    p.add_argument("--work", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    if not __debug__:
        p.error("Run with Python assertions enabled")
    if args.out.exists() or args.work.exists():
        p.error("Use new destinations for each deterministic trial")
    product = args.product.resolve()
    sys.path.insert(0, str(product / "acceptance/v6/controls"))
    import fixture
    import run as controls
    from gx_core.native_release import native_state
    from gx_core.persistence import store
    from gx_core.p0 import v6_decisions, v6_goal, v6_jobs
    from gx_core.p0.roots import Roots

    work = args.work.resolve()
    plan = fixture.prepare(work / "states", work / "review", api_calls=1)
    # This is a transaction probe, isolated from all scored live Runs. No model is started.
    entry = next(e for e in plan if e["packet_id"] == "p42")
    world = work / "states" / entry["slot"]
    state = world / "state"
    env = {**os.environ, "GX_CODEX_OFFLINE": "1"}
    started = controls.invoke(
        env,
        [
            "start",
            "--state",
            state,
            "--goal",
            fixture.fixture_objects()[0]["question"],
            "--run-key",
            "unrelated-amendment",
            "--goal-config",
            world / "goal-config.json",
        ],
        100,
    )
    rid = started["stdout"]["run_id"]
    dbfile = state / "gx.db"
    with native_state(state), closing(store.open_store(dbfile)) as conn:
        first = controls.bootstrap_fixture(conn, state, rid, fixture.fixture_objects()[1])
        graph = first["aliases"]
        refs = controls.inject(
            conn, rid, fixture.read(work / "review" / entry["slot"] / "intervention.json")
        )
        v6_goal.ensure_task(conn, rid)
        (task,) = conn.execute(
            "SELECT t.task_id FROM tasks t JOIN v6_decision_tasks dt USING(task_id) WHERE t.run_id=? AND t.status='READY'",
            (rid,),
        ).fetchone()
        roots = Roots(conn, run_id=rid, cwd=state)
        root = roots._claim(task)
        tid, turn, call = ("fixture-overlap-" + name + "-" + rid for name in ("thread", "turn", "call"))
        roots._bind(root, {"id": tid, "sessionId": tid, "cwd": root["cwd"], "source": "appServer"})
        with store.transaction(
            conn, transaction_owner="AcceptanceV6", operation="fixture_delivery", run_id=rid
        ):
            conn.execute(
                "INSERT INTO p0_root_deliveries(run_id,event_id,thread_id,client_id,text_sha256,human,interrupt,state,method,turn_id) VALUES (?,?,?,?,?,0,0,'acknowledged','turn/start',?)",
                (
                    rid,
                    "fixture-stop-geometry",
                    tid,
                    "fixture-stop-geometry",
                    hashlib.sha256(root["brief"].encode()).hexdigest(),
                    turn,
                ),
            )
        decision = {
            "mode": "amend",
            "ops": [
                {
                    "op": "stop_branch",
                    "branch": graph["local:geometry"],
                    "reason": "Terminate this synthetic geometry scope only.",
                }
            ],
            "evidence_refs": list(refs),
            "rationale": "Engineering probe: close one existing scope while its independent sibling is calculating.",
            "reconsider_when": [{"event": "operator_reopened", "subject_ref": "run"}],
        }
        prepared = v6_decisions.prepare(
            conn,
            run_id=rid,
            task_id=task,
            lease_id=root["lease_id"],
            thread_id=tid,
            turn_id=turn,
            call_id=call,
            decision=decision,
        )

        def submit():
            with closing(store.open_store(dbfile)) as owner:
                return v6_jobs.advance(owner, rid)

        with ThreadPoolExecutor(max_workers=1) as pool:
            ready = pool.submit(submit)
            expires = time.monotonic() + 20
            while time.monotonic() < expires:
                files = list((state / "jobs").glob("*/status.json"))
                status = json.loads(files[0].read_bytes()) if files else {}
                if status.get("state") == "running":
                    break
                if status.get("state") == "finished":
                    raise AssertionError("The protected job finished before the test could observe it")
                time.sleep(0.001)
            else:
                raise TimeoutError("GX did not start the protected Job before the observation deadline")
            sample1 = active_pid(status["tool_pid"], status["tool_started"])
            decision_receipt = v6_decisions.commit(conn, prepared)
            sample2 = active_pid(status["tool_pid"], status["tool_started"])
            ready.result()
        jobdir = state / "jobs" / status["job_id"]
        finish = time.monotonic() + 20
        while time.monotonic() < finish:
            v6_jobs.advance(conn, rid)
            row = conn.execute("SELECT * FROM jobs WHERE run_id=?", (rid,)).fetchone()
            if row and row["status"] == "SUCCEEDED":
                break
            time.sleep(0.025)
        else:
            raise TimeoutError("Protected job did not succeed")
        outcome = fixture.read(jobdir / "outcome.json")
        event = conn.execute(
            "SELECT event_id,ts FROM events WHERE kind='v6.decision.committed' AND subject_id=?",
            (decision_receipt["decision_ref"],),
        ).fetchone()
        assert (
            timestamp(outcome["started_at"])
            <= timestamp(sample1["sampled_at"])
            <= timestamp(sample2["sampled_at"])
            <= timestamp(outcome["finished_at"])
        )
        assert (
            timestamp(outcome["started_at"])
            <= timestamp(event["ts"])
            <= timestamp(outcome["finished_at"])
        )
        assert conn.execute("SELECT count(*) FROM jobs WHERE run_id=?", (rid,)).fetchone()[0] == 1
        assert (
            conn.execute(
                "SELECT count(*) FROM budget_reservations WHERE subject_kind='job' AND subject_id=?",
                (row["job_id"],),
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT status FROM v6_branches WHERE branch_id=?", (graph["local:geometry"],)
            ).fetchone()[0]
            == "STOPPED"
        )
        assert (
            conn.execute(
                "SELECT status FROM v6_branches WHERE branch_id=?", (graph["local:citation-index"],)
            ).fetchone()[0]
            == "OPEN"
        )
        paid_turns = conn.execute(
            "SELECT count(*) FROM p0_meter_native_turns WHERE run_id=?", (rid,)
        ).fetchone()[0]
        assert paid_turns == 0
        node = conn.execute("SELECT * FROM v6_nodes WHERE job_id=?", (row["job_id"],)).fetchone()
        r = {
            "mode": "SCRIPTED decision; real protected CPU Job",
            "run_id": rid,
            "scope_stopped": graph["local:geometry"],
            "scope_protected": graph["local:citation-index"],
            "commit": dict(event),
            "decision": decision_receipt["decision_ref"],
            "protected_job": row["job_id"],
            "operation": node["operation_key"],
            "reservation": row["reservation_ref"],
            "result_ref": node["result_ref"],
            "status": row["status"],
            "process_before_commit": sample1,
            "process_after_commit": sample2,
            "runner_outcome": outcome,
            "paid_turns": paid_turns,
            "snapshot_state": str(state),
        }
    frozen = controls.invoke(env, ["resume", "--state", state, "--run-id", rid, "--timeout", "1"], 40)
    jobs = frozen["stdout"]["jobs"]
    if len(jobs) != 1 or jobs[0]["job_id"] != r["protected_job"] or jobs[0]["status"] != "SUCCEEDED":
        raise AssertionError("Cold reopen changed the protected job")
    r["cold_resume"] = {"pid": frozen["pid"], "jobs": jobs, "graph_head": frozen["stdout"]["graph_head"]}
    fixture.save(args.out, r)


if __name__ == "__main__":
    main()
