"""Drive two competing READY tasks through GX and pinned Codex using scripted local inference."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from unittest.mock import patch

from e2e_responses import fixture_action
from native_probe import Probe, find_binary
from native_probe_peer import Peer
from gx_core import budgets
from gx_core.artifacts import ArtifactService
from gx_core.hostobs import host_policy
from gx_core.jobs.grid_workflow import submit_grid
from gx_core.native_release import native_state
from gx_core.orchestration import service
from gx_core.p0 import cli, intent, schema
from gx_core.p0.profiles import CODEX_VERSION, clash_env
from gx_core.persistence import records, store


class AssessmentPeer(Peer):
    """Produce ordinary artifact reads, submission and final reply through Responses."""

    def __init__(self, turn_cost):
        super().__init__()
        self.turn_cost = turn_cost
        self.issued = {}
        self.items = {}
        self.gate.clear()

    def record(self, body):
        row, n = super().record(body)
        item, facts = fixture_action(body, row["id"])
        task = facts["task_id"]
        so_far = self.issued.get(task, 0)
        cost = self.turn_cost - so_far if item["type"] == "message" else 2
        assert cost >= 2
        row.update(task_id=task, action=facts.get("tool", "reply"),
                   usage={"input_tokens":cost-1, "output_tokens":1, "total_tokens":cost})
        self.issued[task] = so_far + cost
        self.items[row["id"]] = item
        return row, n

    def item(self, row, n):
        if not self.gate.wait(30):
            raise RuntimeError("GX did not publish a waiting task before the provider deadline")
        return self.items[row["id"]]


def project_fixture(conn, calls, followup):
    policy, digest, host = host_policy.p0_host()
    workflow = service.default_registry().get("science_grid", "1")
    with store.transaction(conn) as tx:
        budget = budgets.create_root(tx, run_id=None, profile_name="p0", actor="fixture",
            limits={**budgets.profile("p0")["limits"], "tokens":100, "api_calls":calls})
        run = records.create_run(tx, objective="Review the supplied synthetic grid measurements",
            project_ref="competing-reviews", actor="fixture", budget_ref=budget,
            workflow_id="science_grid", workflow_version="1", host=host, agent_policy=policy,
            pins=service.installed_pins(workflow, policy_sha256=digest),
            inputs={"inputs":{"p0":True, "p0_spec":{"turn_token_budget":100}}})
        tx.execute("UPDATE budgets SET run_id=? WHERE budget_ref=?", (run, budget))
        store.acquire_coordinator_lease(tx, run_id=run, host="codex", owner="fixture")
        for status in ("AUDITING", "PLANNING", "ACTIVE"):
            store.transition(tx, machine="run", subject_id=run, to_state=status, actor="fixture")
        intent.bind_main(tx, run, "fixture-author")
        assets = ArtifactService(tx)
        def publish(kind, value):
            return assets.publish_bytes(run_id=run, namespace=run, rel_path=kind+".json", kind=kind,
                       data=json.dumps(value), media_type="application/json").artifact_ref
        proposal = publish("research_proposal", {"threshold":0.5, "author_principal":"fixture-author",
                "paths":[{"id":"p", "expected_reaches_threshold":True}]})
        observed = publish("grid_observations", {"observations":[{"x":1, "score":1}]})
        scored = publish("grid_summary", {"threshold":0.5, "input_ref":observed,
                        "count_at_or_above":1, "max_score":1})
        evidence = publish("research_evidence", {"routes":[{"path_id":"p", "status":"SUCCEEDED",
                 "observations_ref":observed, "evaluation_ref":scored, "outcome_kind":"valid_scientific_outcome"}]})
        tasks = []
        for idx in range(3 if followup else 2):
            deps = tasks[:] if idx == 2 else []
            task = records.create_task(tx, run_id=run, kind="research_round", role="evidence_reviewer",
                actor="fixture", required_artifacts=["science_assessment"], depends_on=deps,
                task_input={"round":0, "max_rounds":1, "output_kind":"science_assessment", "p0":True,
                            "proposal_ref":proposal, "evidence_refs":[evidence]},
                acceptance={"validate_workflow_product":True})
            if not deps:
                store.transition(tx, machine="task", subject_id=task, to_state="READY", actor="fixture")
            tasks.append(task)
    return run, budget, tasks


def launch_grid(conn, run):
    grid = {"synthetic":True, "method_id":"fixture-grid", "method_version":"1", "delay_seconds":5,
            "candidates":[{"candidate_id":"point", "x":2}]}
    source = ArtifactService(conn).publish_bytes(run_id=run, namespace=run, rel_path="running-grid.json",
            kind="grid", data=json.dumps(grid), media_type="application/json").artifact_ref
    job = submit_grid(conn, run_id=run, operation_id="in-flight-grid", input_ref=source,
                      binding={"workflow_id":"science_grid", "node_id":"experiment"})
    return job["job_id"]


def observed(conn, budget):
    def rows(sql):
        return [dict(row) for row in conn.execute(sql)]
    ledger = budgets.state(conn, budget)
    return {"balance":{k:ledger[k] for k in ("spent","reserved","available","reservations")},
            "tasks":rows("SELECT task_id,status,attempt,attempts_consumed FROM tasks ORDER BY rowid"),
            "jobs":rows("SELECT j.job_id,j.status,j.scheduler_ref,m.completion_sha256 FROM jobs j "
                        "LEFT JOIN p0_execution_manifests m USING(job_id) ORDER BY j.rowid"),
            "roots":rows("SELECT task_id,attempt,lease_id,thread_id FROM p0_roots ORDER BY rowid"),
            "leases":rows("SELECT task_id,lease_id,attempt FROM leases ORDER BY rowid"),
            "paid":rows("SELECT r.task_id,d.thread_id,d.turn_id,d.token_budget,u.total_tokens,u.terminal_status "
                        "FROM p0_root_deliveries d JOIN p0_roots r USING(thread_id) "
                        "LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id) "
                        "WHERE d.method='turn/start' ORDER BY d.rowid"),
            "submissions":rows("SELECT task_id,attempt,result FROM submissions ORDER BY rowid"),
            "failures":rows("SELECT kind,task_id FROM science_settlements WHERE kind IN "
                            "('agent_execution_failure','infrastructure_failure')")}


def check_case(report, tasks, expected_paid, cost, line):
    held, after = report["during_contention"], report["after"]
    assert [row["status"] for row in report["initial"]["tasks"][:2]] == ["READY","READY"]
    assert len(held["paid"]) == 1 and held["paid"][0]["turn_id"] and held["paid"][0]["token_budget"] == 100, held
    assert held["balance"]["reserved"]["tokens"] == 100 and not held["balance"]["spent"].get("tokens"), held
    assert [r["task_id"] for r in held["roots"]] == tasks[:2] and all(r["thread_id"] for r in held["roots"]), held
    waiting, = held["attempt_waits"]
    assert waiting["reason"] == "budget" and waiting["task_id"] == tasks[1] and waiting["lease_id"] == held["roots"][1]["lease_id"]
    assert [r["task_id"] for r in after["paid"]] == tasks[:expected_paid], after
    assert [r["token_budget"] for r in after["paid"]] == [100,65][:expected_paid], after
    assert all(r["terminal_status"] == "completed" and r["total_tokens"] == cost for r in after["paid"]), after
    assert all(r["attempt"] == 1 and r["result"] == "ACCEPTED" for r in after["submissions"]), after
    assert [r["task_id"] for r in after["submissions"]] == tasks[:expected_paid], after
    reservations = [row for row in after["balance"]["reservations"] if row["subject_kind"] == "native_turn"]
    assert len(reservations) == expected_paid
    assert all(r["status"] == "SETTLED" for r in after["balance"]["reservations"])
    assert after["balance"]["spent"]["tokens"] == cost*expected_paid and after["balance"]["spent"]["api_calls"] == expected_paid, after
    assert not any(after["balance"]["reserved"].values()), after
    assert not after["failures"] and all(r["attempts_consumed"] == 0 and r["attempt"] <= 1 for r in after["tasks"])
    assert held["roots"] == after["roots"][:2], "a contending Root or lease was replaced"
    for collection in ("roots","leases"):
        owners = [row["task_id"] for row in after[collection]]
        assert len(set(owners)) == len(owners) and all(row["attempt"] == 1 for row in after[collection])
    assert [r["task_id"] for r in after["tasks"] if r["status"] == "ACCEPTED"] == tasks[:expected_paid]
    assert set(report["provider_totals"]) == set(tasks[:expected_paid])
    assert all(total == cost for total in report["provider_totals"].values())
    for task in tasks[:expected_paid]:
        actions = [r["action"] for r in report["provider_requests"] if r["task_id"] == task]
        assert actions.count("gx_submit_product") == actions.count("reply") == 1, actions
    if job := report.get("in_flight_job"):
        assert held["jobs"][0]["job_id"] == job and held["jobs"][0]["status"] in ("RUNNING","SUBMITTED")
        finished, = after["jobs"]
        assert finished["job_id"] == job and finished["status"] == "SUCCEEDED" and finished["completion_sha256"], finished
    assert report["status"]["status"] == "PAUSED" and report["status"].get("driver") != "time_limit", report["status"]
    reason = report["status"]["pause"]["reason"]
    assert reason.startswith("budget_exhausted") and line in reason, reason
    assert report["cold_status"] == report["status"], "cold status lost the pause reason"


def run_case(folder, case, report):
    call_limit, per_turn, expected_paid, dimension = {
        "contention":(2,35,2,"api_calls"),
        "token-exhaustion":(5,100,1,"tokens"),
        "call-exhaustion":(1,35,1,"api_calls"),
    }[case]
    binary = find_binary()
    with ExitStack() as stack:
        peer = stack.enter_context(AssessmentPeer(per_turn))
        server = threading.Thread(target=peer.serve_forever, daemon=True)
        server.start()
        stack.callback(server.join)
        stack.callback(peer.shutdown)
        stack.callback(peer.gate.set)
        probe = Probe(folder/"official", binary, peer)
        knobs = {**probe.env, "GX_P0_TEST":"1", "GX_P0_TEST_PROVIDER":probe.provider,
                 "GX_P0_MODEL":"gpt-6.2-astra", "GX_P0_CODEX":str(binary)}
        stack.enter_context(patch.dict(os.environ, knobs))
        clash_env()
        report["binary"] = subprocess.check_output([binary, "--version"], text=True).strip()
        assert report["binary"] == "codex-cli "+CODEX_VERSION
        state = stack.enter_context(native_state(folder/"state"))
        conn = stack.enter_context(closing(store.open_store(state.db_path)))
        schema.install(conn)
        run, budget, tasks = project_fixture(conn, call_limit, expected_paid == 2)
        if case == "token-exhaustion":
            report["in_flight_job"] = launch_grid(conn, run)
        report.update(run=run, initial=observed(conn, budget))
        # Delay only the first provider reply. An independent read-only observer releases it
        # when a fresh public status read shows B waiting under its own original task lease.
        stop = threading.Event()
        def release_on_contention():
            with closing(cli.existing_db(state.directory, readonly=True)) as observer:
                while not stop.is_set():
                    pending = cli.snapshot(observer).get("attempt_waits", [])
                    if any(w["task_id"] == tasks[1] and w["reason"] == "budget" for w in pending):
                        report["during_contention"] = {**observed(observer, budget), "attempt_waits":pending}
                        peer.gate.set()
                        return
                    stop.wait(0.02)
        watcher = threading.Thread(target=release_on_contention, daemon=True)
        watcher.start()
        try:
            report["status"] = cli.run_until_stopped(conn, state.directory, run, 30)
        finally:
            stop.set()
            watcher.join()
            report["after"] = observed(conn, budget)
            report["provider_totals"] = peer.issued
            report["provider_requests"] = [{k:r[k] for k in ("id","task_id","action","usage") if k in r}
                                           for r in peer.rows]
        with closing(cli.existing_db(state.directory, readonly=True)) as cold:
            report["cold_status"] = cli.snapshot(cold)
        if report["status"]["status"] == "PAUSED":
            requests_before = len(peer.rows)
            with closing(cli.existing_db(state.directory, readonly=False)) as reopened:
                again = cli.run_until_stopped(reopened, state.directory, run, 2)
                assert again == report["cold_status"] and observed(reopened, budget) == report["after"]
            report["new_requests_on_paused_reopen"] = len(peer.rows) - requests_before
            assert report["new_requests_on_paused_reopen"] == 0
        check_case(report, tasks, expected_paid, per_turn, dimension)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("contention","token-exhaustion","call-exhaustion"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = {"case":args.case, "entry":"gx_core.p0.cli.run_until_stopped",
              "inference":"scripted loopback Responses; synthetic numerical assessment",
              "at":datetime.now(timezone.utc).isoformat(), "result":"FAIL"}
    try:
        with tempfile.TemporaryDirectory(prefix="gx-acc1-driver-") as tmp:
            run_case(Path(tmp), args.case, result)
        result["result"] = "PASS"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2)+"\n")
