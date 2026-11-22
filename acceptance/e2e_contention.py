"""F12. Exercise two official Codex Roots competing for the same GX project budget."""
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import argparse
import json
import os
import subprocess
from pathlib import Path
import tempfile
import threading
import time
from unittest.mock import patch

from e2e_audit import require
from native_probe import Probe, find_binary
from native_probe_peer import Peer
from gx_core import budgets
from gx_core.errors import HoldActive
from gx_core.p0 import schema
from gx_core.p0.profiles import clash_env
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store


def snapshot(conn, budget):
    def select(sql):
        return [dict(r) for r in conn.execute(sql)]
    return {"balance": budgets.state(conn, budget),
            "roots": select("SELECT task_id,attempt,thread_id,role FROM p0_roots ORDER BY rowid"),
            "turns": select("SELECT thread_id,turn_id,terminal_status,total_tokens FROM p0_root_turn_usage ORDER BY rowid"),
            "reservations": select("SELECT subject_id,status,amounts_json FROM budget_reservations ORDER BY rowid"),
            "deliveries": select("SELECT event_id,state,thread_id,turn_id,method,token_budget "
                                 "FROM p0_root_deliveries ORDER BY rowid")}


def run(folder, result):
    with ExitStack() as stack:
        peer = stack.enter_context(Peer())
        thread = threading.Thread(target=peer.serve_forever, daemon=True)
        thread.start()
        stack.callback(peer.shutdown)
        p = Probe(folder/"runtime", find_binary(), peer)
        p.env.update({k: v for k, v in clash_env().items() if k.lower().endswith("_proxy")})
        knobs = {"GX_P0_TEST": "1", "GX_P0_TEST_PROVIDER": p.provider, "GX_P0_MODEL": "gpt-6.2-astra",
                 "GX_P0_CODEX": str(p.binary), "CODEX_HOME": str(p.home)}
        stack.enter_context(patch.dict(os.environ, knobs))
        conn = stack.enter_context(closing(store.open_store(folder/"gx.db")))
        schema.install(conn)
        with store.transaction(conn) as tx:
            limits = {**budgets.profile("p0")["limits"], "tokens": 100}
            budget = budgets.create_root(tx, run_id=None, profile_name="p0", actor="acceptance", limits=limits)
            rid = records.create_run(tx, objective="Verify shared budget competition", project_ref="contending-roots",
                                     actor="acceptance", budget_ref=budget)
            for status in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(tx, machine="run", subject_id=rid, to_state=status, actor="acceptance")
            tasks = [records.create_task(tx, run_id=rid, kind="research_round", role=role, actor="acceptance")
                     for role in ("planner", "evidence_reviewer")]
            for task in tasks:
                store.transition(tx, machine="task", subject_id=task, to_state="READY", actor="acceptance")
        root = stack.enter_context(Roots(conn, run_id=rid, cwd=p.cwd, env={**p.env, **knobs}, token_budget=100))
        result["version"] = subprocess.check_output([p.binary, "--version"], env=p.env, text=True).strip()
        result["before"] = snapshot(conn, budget)

        def finish(work):
            # A completed operator-assigned probe turn has no pending computation.
            root._request("thread/goal/set", {"threadId": work["thread_id"], "status": "paused"})
            peer.gate.set()
            event = root.client.run_until(lambda f: f.message.get("method") == "turn/completed" and
                    f.message["params"]["turn"]["id"] == work["turn_id"], timeout=15)
            require(event and event.message["params"]["turn"]["status"] == "completed", "turn did not finish")

        peer.gate.clear()
        stack.callback(peer.gate.set)
        first = root.dispatch(tasks[0])
        deadline = time.monotonic() + 5
        while not peer.rows and time.monotonic() < deadline:
            time.sleep(.01)
        require(len(peer.rows) == 1, "first official inference did not reach the barrier")
        try:
            root.dispatch(tasks[1])
        except HoldActive as exc:
            require(exc.details.get("reason") == "budget_reserved", "wrong reason for waiting")
            result["rejection"] = str(exc)
        else:
            raise AssertionError("second Root entered a turn while the first reserved the total")
        held = result["during_contention"] = snapshot(conn, budget)
        require(len(held["roots"]) == 2 and len({r["thread_id"] for r in held["roots"]}) == 2,
                "expected two distinct real Codex threads")
        require(len(held["deliveries"]) == len(peer.rows) == 1 and held["balance"]["reserved"]["tokens"] == 100,
                "refusal consumed a duplicate turn or changed the prior reservation")
        finish(first)
        peer.gate.clear()
        second = root.dispatch(tasks[1])
        require(second["thread_id"] == held["roots"][1]["thread_id"], "retry recreated the second Root")
        finish(second)
        after = result["after_retry"] = snapshot(conn, budget)
        require(len(peer.rows) == 2 and len(after["turns"]) == 2 and
                all(u["terminal_status"] == "completed" for u in after["turns"]), "wrong real turn count")
        require(after["balance"]["spent"]["tokens"] == 70 and after["balance"]["reserved"].get("tokens", 0) == 0,
                "unused allowance did not return to the project")
        require([r["token_budget"] for r in after["deliveries"]] == [100, 65], "retry ignored remaining tokens")
        result.update(result="PASS", model_service="scripted loopback Responses", profile="xhigh",
                      responses=len(peer.rows), external_charge_usd=0, human_intervention=False)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", type=Path, required=True)
    target = p.parse_args().out
    target.parent.mkdir(parents=True, exist_ok=True)
    report = {"id": "F12", "start": datetime.now(timezone.utc).isoformat(), "result": "FAIL"}
    try:
        with tempfile.TemporaryDirectory(prefix="gx-p0-competitor-") as temporary:
            run(Path(temporary), report)
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["end"] = datetime.now(timezone.utc).isoformat()
        target.write_text(json.dumps(report, indent=2) + "\n")
