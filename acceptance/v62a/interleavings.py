#!/usr/bin/env python3
"""Run A2–A7 on public sources using controlled local callbacks and the installed GX runtime."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from acceptance.v62a.interleavings_fixture import (
    Actor, ArtifactService, GEOMETRY, PRODUCT, Roots, Scenario, branch, check, draft, ids, save,
    store, v6_decisions, v6_native,
)
from gx_core.errors import ScopeDenied, StaleRevision
from gx_core.teams.service import TeamService


def _events(s, kind):
    return s.rows("SELECT event_id,subject_id,payload_json FROM events WHERE run_id=? AND kind=? ORDER BY event_id",
                  s.run_id, kind)


def read(s, actor, ref, key, *, call_id=None):
    return s.call(actor, "gx_read_artifact", {"ref": ref, "operation_key": key}, call_id or key)


def deny(verb, expected=(ScopeDenied, StaleRevision)):
    try:
        verb()
    except expected as exc:
        return {"code": exc.code, "reason": exc.message, "details": exc.details}
    raise AssertionError("The unauthorized action succeeded")


def accepted_reports(s, count):
    reports = s.rows("SELECT module_instance_id,report_ref,operation_key,version FROM v62_module_reports "
                     "WHERE run_id=? ORDER BY module_instance_id", s.run_id)
    check(len(reports) == count, f"Expected {count} independent reports, observed {reports}")
    for row in reports:
        check(ArtifactService(s.conn).verify(row["report_ref"])["ok"], "Report byte verification failed")
    return reports


def _lead_move(s, key):
    return s.call(s.lead, v6_native.DECIDE, {"decision": draft(branch(key))}, key)


def a2(directory):
    histories = {}
    for order in ("lead_before_read", "lead_after_read", "cancel_after_read"):
        with Scenario(directory / order) as s:
            module = s.open("inspect-source", inputs=[s.materials[0]])
            worker = s.bind(module["task_id"])
            original = s.brief(worker)["head"]
            if order == "lead_before_read":
                change = _lead_move(s, "unrelated-public-branch")
            observation = read(s, worker, s.materials[0], "inspect-source-window")
            check(observation, "No result from scoped tool")
            if order == "lead_after_read":
                change = _lead_move(s, "unrelated-public-branch")
            [tool] = _events(s, "v62.module.tool_completed")
            if order.startswith("lead"):
                check(original != change["result_head"], "Control never changed the graph")
                report = s.report(worker, s.materials[0])
                [entry] = accepted_reports(s, 1)
                check(entry["module_instance_id"] == module["module_instance_id"], "Independent result lost its owner")
                histories[order] = dict(original=original, change=change, report_ref=entry["report_ref"],
                                        source_event=tool["event_id"], tool_answer_sha=ids.sha256_of_obj(observation),
                                        module=module["module_instance_id"], history=s.trace())
            else:
                old_proposal = v6_decisions.prepare(s.conn, run_id=s.run_id, task_id=worker.task_id,
                    lease_id=worker.lease_id, thread_id=worker.thread, turn_id=worker.turn, call_id="prepared-before-cancel",
                    decision=draft(s.node(worker, "pending-grant"), watches=[
                        {"event":"job_settled","subject_ref":"local:pending-grant"}]))
                cancelled = s.call(s.lead, "gx_cancel_module", {"module_instance_id": module["module_instance_id"],
                    "operation_key": "withdraw-assignment", "reason": "This input is no longer assigned."}, "cancel")
                rejected = deny(lambda: s.submit_node(worker, "late-measurement"))
                revoked_preparation = deny(lambda: v6_decisions.commit(s.conn, old_proposal))
                check(_events(s, "v62.module.tool_completed") == [tool], "Cancellation removed acknowledged evidence")
                check(not s.rows("SELECT 1 FROM jobs WHERE run_id=?", s.run_id), "Cancelled permission dispatched a Job")
                histories[order] = dict(cancellation=cancelled, late_write=rejected,
                                        retained_source=json.loads(tool["payload_json"]), prepared_write=revoked_preparation, history=s.trace())
    with Scenario(directory / "monitored_counterevidence") as s:
        watched = s.open("watch-this-module", inputs=[s.materials[0]], wake_on=["counterevidence"])
        peer = s.open("unaffected-module", inputs=[s.materials[1]])
        a, b = s.bind(watched["task_id"]), s.bind(peer["task_id"])
        s.call(s.lead, v6_native.DECIDE, {"decision": draft()}, "wait-for-inventory")
        read(s, a, s.materials[0], "monitored-inspection")
        early_payload = {"findings": [{"text": "The inspected bytes contain coordinate records, contrary to the fixture's null expectation.",
                                      "kind": "counterevidence", "source_refs": [s.materials[0]]}]}
        first = s.report(a, s.materials[0], "monitored-report", **early_payload)
        s.advance()
        lead = s.next_actor("run")
        [wake] = _events(s, "v62.module.wake")
        check(json.loads(wake["payload_json"])["reason"] == "counterevidence", "Early wake lost its trigger")
        check(s.rows("SELECT status FROM v62_modules WHERE module_instance_id=?", peer["module_instance_id"])
              == [{"status": "OPEN"}], "Peer had closed before the supposed early wake")
        delivered = s.brief(lead)
        check(len(delivered["module_reports"]) == 1 and first["report_ref"] in delivered["evidence_refs"],
              "The pointer to the watched report did not reach the new Lead")
        before = s.rows("SELECT task_id FROM v6_decision_tasks WHERE run_id=? AND scope='run' ORDER BY rowid", s.run_id)
        check(s.report(a, s.materials[0], "monitored-report", **early_payload) == first, "Early report replay changed")
        s.advance()
        check(_events(s, "v62.module.wake") == [wake] and s.rows(
            "SELECT task_id FROM v6_decision_tasks WHERE run_id=? AND scope='run' ORDER BY rowid", s.run_id) == before,
              "Replaying the event caused a second effective early wake")
        read(s, b, s.materials[1], "finish-independent-inspection")
        s.report(b, s.materials[1], "peer-round-finish")
        later = _events(s, "v62.module.wake")
        check(len(later) == 2 and json.loads(later[1]["payload_json"])["module_instance_ids"] == [peer["module_instance_id"]],
              "The ordinary round re-delivered the already released report")
        histories["monitored_counterevidence"] = dict(first_wake=json.loads(wake["payload_json"]),
            round_wake=json.loads(later[1]["payload_json"]), lead_received=lead.task_id,
            report_refs=delivered["evidence_refs"], history=s.trace())
    return {"status": "PASS", "schedules": histories}


def a3(directory):
    with Scenario(directory) as s:
        first = s.open("module-west", inputs=[s.materials[0]])
        second = s.open("module-east", inputs=[s.materials[1]])
        s.call(s.lead, v6_native.DECIDE, {"decision": draft()}, "wait-for-both-inventories")
        a, b = s.bind(first["task_id"]), s.bind(second["task_id"])
        check(a.scope != b.scope and first["budget_ref"] != second["budget_ref"], "Scopes/accounts aliased")
        base = s.head()
        ops = [s.node(a, "west", s.materials[0]),
               s.node(b, "east", s.materials[1], capability=GEOMETRY, parameters={"mask": "common_ca"})]
        decisions = [draft(op, watches=[{"event": "job_settled", "subject_ref": op["node"]}]) for op in ops]
        # Two real native callbacks on separate connections. Both finish outside-lock preparation
        # at the same head; a commanded release orders the CAS, independent of machine scheduling.
        both_prepared = threading.Barrier(2, timeout=15)
        first_committed = threading.Event()
        prepare = v6_decisions.prepare
        prepared = {}
        def prepare_then_hold(conn, **kwargs):
            plan = prepare(conn, **kwargs)
            check(not conn.in_transaction, "Prepared callback retained the SQLite writer")
            key = kwargs["task_id"]
            prepared[key] = plan.request["base"]
            both_prepared.wait()
            if key == b.task_id:
                check(first_committed.wait(15), "First proposal did not commit")
            return plan
        def propose(side):
            actor = (a, b)[side]
            with closing(store.connect(s.state / "gx.db")) as db:
                roots = Roots(db, run_id=s.run_id, cwd=s.state)
                roots.client = s.roots.client
                roots.active_turns[actor.thread] = actor.turn
                payload = {"threadId": actor.thread, "turnId": actor.turn, "callId": "proposal-" + str(side),
                           "tool": v6_native.DECIDE, "namespace": "gx", "arguments": {"decision": decisions[side]}}
                try:
                    return {"accepted": roots._call(payload)}
                except StaleRevision as exc:
                    return {"conflict": exc.to_payload()}
                finally:
                    if side == 0:
                        first_committed.set()
        with patch.object(v6_decisions, "prepare", prepare_then_hold), ThreadPoolExecutor(2) as threads:
            left, right = list(threads.map(propose, (0, 1)))
        check(prepared == {a.task_id: base, b.task_id: base}, "The two decisions did not share a base")
        check("accepted" in left and "conflict" in right, f"CAS did not yield exactly one winner: {left},{right}")
        check(len(s.rows("SELECT version FROM v6_graph_versions WHERE run_id=?", s.run_id)) == 1, "Lost CAS fencing")
        # A fresh preparation checks grant, budget, graph subjects and source references.
        from gx_core.p0 import v62_modules
        v62_modules.advance(s.conn, s.run_id)
        b2 = s.next_actor(b.scope)
        adopted_right = s.call(b2, v6_native.DECIDE, {"decision": decisions[1]}, "reconsider-east")
        check(adopted_right["result_head"]["version"] == 2, "Second decision did not extend first")
        west = s.settle(left["accepted"]["aliases"]["local:west"])
        east = s.settle(adopted_right["aliases"]["local:east"])
        check(west["job_id"] != east["job_id"], "Distinct input/method runs used one Job")
        a2, b3 = s.next_actor(a.scope), s.next_actor(b.scope)
        report1 = s.report(a2, west["result_ref"], "west-report")
        check(not s.rows("SELECT 1 FROM v62_module_wakes WHERE run_id=?", s.run_id), "Default batch woke early")
        report2 = s.report(b3, east["result_ref"], "east-report")
        check(report1 == s.report(a2, west["result_ref"], "west-report") and
              report2 == s.report(b3, east["result_ref"], "east-report"), "Report replay changed a receipt")
        wakes = s.rows("SELECT * FROM v62_module_wakes WHERE run_id=?", s.run_id)
        check(len(wakes) == 1, "Default round did not coalesce reports")
        jobs = s.rows("""SELECT j.job_id,j.status,j.measured_json,b.budget_ref,b.status AS settlement,b.amounts_json
            FROM jobs j JOIN budget_reservations b USING(reservation_ref) WHERE j.run_id=?""", s.run_id)
        accounts = {first["budget_ref"], second["budget_ref"]}
        check(len(jobs) == 2 and {j["budget_ref"] for j in jobs} == accounts
              and all(j["settlement"] == "SETTLED" for j in jobs), "CPU work was not settled to each module")
        history = s.trace()
        check(history["costs"]["lines"]["job_submissions"]["booked"] == 2, "Incorrect parent total for two Jobs")
        return {"status": "PASS", "prepared_outside_writer_at": prepared, "winner": left,
                "cas_conflict": right, "second_adoption": adopted_right, "reports": accepted_reports(s, 2),
                "charges": jobs, "round_wakes": wakes, "histories": history}


def a4(directory):
    with Scenario(directory) as s:
        mine = s.open("module1", inputs=[s.materials[0]])
        other = s.open("module2", inputs=[s.materials[1]])
        worker = s.bind(mine["task_id"])
        peer = s.bind(other["task_id"])
        escape = deny(lambda: s.submit_node(worker, "cross-scope", branch_id=other["branch_id"]))
        accepted = s.submit_node(worker, "inventory")
        [end] = s.rows("SELECT status,lease_id FROM tasks WHERE task_id=?", worker.task_id)
        check(end["status"] == "ACCEPTED", "Job decision task stayed open after its accepted amendment")
        check(s.rows("SELECT status FROM v62_modules WHERE module_instance_id=?", mine["module_instance_id"])
              == [{"status": "OPEN"}], "Graph amendment closed its module")
        read(s, peer, s.materials[1], "peer-independent-read")
        measured = s.settle(accepted["aliases"]["local:inventory"])
        successor = s.next_actor(mine["scope"])
        check(successor.task_id != worker.task_id and successor.lease_id != worker.lease_id, "Task lease was reused")
        check(measured["result_ref"] in s.brief(successor)["evidence_refs"], "Job output absent from new frozen scope")
        s.report(successor, measured["result_ref"], "inventory-report")
        s.report(peer, s.materials[1], "peer-report")
        check(len(s.rows("SELECT job_id FROM jobs WHERE run_id=?", s.run_id)) == 1, "Unexpected external work")
        return {"status": "PASS", "out_of_scope_refusal": escape, "adoption": accepted,
                "old_actor": asdict(worker), "successor": asdict(successor), "job": measured,
                "reports": accepted_reports(s, 2), "history": s.trace()}


def _counted_read(s, path, actor, ref, call_id):
    original = s.roots._read_artifact
    def observe_real_read(*args, **kwargs):
        answer = original(*args, **kwargs)
        with path.open("a", encoding="utf-8") as out:
            out.write(json.dumps({"pid": os.getpid(), "source": ref, "sha256": ids.sha256_of_obj(answer)}) + "\n")
            out.flush()
            os.fsync(out.fileno())
        return answer
    with patch.object(s.roots, "_read_artifact", observe_real_read):
        return read(s, actor, ref, "recover-source", call_id=call_id)


def crash_after_completion(directory):
    with Scenario(directory) as s:
        module = s.open("recover-inventory", inputs=[s.materials[0]])
        initial = s.bind(module["task_id"])
        accepted = s.submit_node(initial, "recoverable-job")
        measured = s.settle(accepted["aliases"]["local:recoverable-job"])
        worker = s.next_actor(module["scope"])
        answer = _counted_read(s, directory / "tool-executions.jsonl", worker, s.materials[0], "read-before-crash")
        check(not accepted_reports(s, 0), "Test crashed after publication")
        save(directory / "crash.json", {"pid": os.getpid(), "worker": asdict(worker), "module": module,
            "result_sha256": ids.sha256_of_obj(answer), "measured": measured,
            "job_ids": s.rows("SELECT job_id,runner_pid,runner_started,input_manifest_ref FROM jobs WHERE run_id=?", s.run_id),
            "identity": s.rows("SELECT exec_key,identity_json,request_sha256 FROM v6_job_attempts WHERE run_id=?", s.run_id),
            "history": s.trace()})
        os._exit(83)


def crash_on_launch(directory):
    from gx_core.jobs import executor
    with Scenario(directory) as s:
        module = s.open("uncertain-launch", inputs=[s.materials[0]])
        worker = s.bind(module["task_id"])
        accepted = s.submit_node(worker, "launch-interrupted")
        node_id = accepted["aliases"]["local:launch-interrupted"]
        start = executor.launch
        def launch_then_exit(job_dir):
            identity = start(job_dir)
            save(directory / "launch.json", {"job_id": job_dir.name, **identity, "scope": worker.scope,
                                             "lease_id": worker.lease_id, "node_id": node_id})
            os._exit(84)
        with patch.object(executor, "launch", launch_then_exit):
            s.advance()


def uncertain_launch(directory):
    directory.mkdir(parents=True)
    child = subprocess.run([sys.executable, "-B", __file__, "--interrupt-launch", str(directory)],
                           capture_output=True, text=True)
    check(child.returncode == 84, f"Worker never reached uncertain launch: {child.stderr}")
    original = json.loads((directory / "launch.json").read_bytes())
    with Scenario(directory, reopen=True) as s:
        before = s.rows("SELECT job_id,status,runner_pid FROM jobs WHERE run_id=?", s.run_id)
        check(len(before) == 1 and before[0]["status"] == "SUBMITTING" and before[0]["runner_pid"] is None,
              "GX had already saved its launch acknowledgement")
        # Count any new attempt to physically start the job when GX knows only the accepted intent.
        from gx_core.jobs import executor
        with patch.object(executor, "launch", wraps=executor.launch) as starts:
            result = s.settle(original["node_id"])
            for _ in range(2):
                s.advance()
        [job] = s.rows("SELECT job_id,status,runner_pid,runner_started,measured_json FROM jobs WHERE run_id=?",
                       s.run_id)
        check(starts.call_count == 0 and job["job_id"] == original["job_id"]
              and job["runner_pid"] == original["runner_pid"] and job["runner_started"] == original["runner_started"],
              "GX failed to reconcile the original external execution")
        successor = s.next_actor(original["scope"])
        report = s.report(successor, result["result_ref"], "report-reconciled-launch")
        return {"crash_exit": 84, "accepted_real_launcher": original, "before_reconciliation": before,
                "after": job, "additional_launches": starts.call_count, "report":report["report_ref"],
                "history": s.trace()}


def a5(directory):
    directory.mkdir(parents=True, exist_ok=False)
    crashed = subprocess.run([sys.executable, "-B", __file__, "--crash-worker", str(directory)],
                             capture_output=True, text=True)
    check(crashed.returncode == 83, f"Worker failed to reach crash boundary: {crashed.returncode} {crashed.stderr}")
    original = json.loads((directory / "crash.json").read_bytes())
    with Scenario(directory, reopen=True) as s:
        worker = Actor(**original["worker"])
        check(worker.task_id in [t["task_id"] for t in s.rows("SELECT task_id FROM tasks WHERE run_id=?", s.run_id)],
              "Original task was lost at recovery")
        check(not s.rows("SELECT report_ref FROM v62_module_reports WHERE run_id=?", s.run_id),
              "Report existed before recovery")
        # The crashed fixture host's exit status is known. Use the existing GX backend owner
        # to release exactly its attempt and requeue, preserving the old lease history.
        TeamService(s.conn).recover_failed_attempt(task_id=worker.task_id, lease_ref=worker.lease_id,
                                                  reason="Scripted native transport exited with status 83")
        successor = s.next_actor(worker.scope)
        check(successor.lease_id != worker.lease_id, "Recovery inherited write authority from dead process")
        check(original["measured"]["result_ref"] in s.brief(successor)["evidence_refs"],
              "Recovered frozen brief does not cite the completed Job")
        current = s.rows("SELECT exec_key,identity_json,request_sha256 FROM v6_job_attempts WHERE run_id=?", s.run_id)
        check(current == original["identity"], "Execution fingerprint or frozen environment changed")
        for _ in range(2):
            s.advance()
        job_ids = s.rows("SELECT job_id,runner_pid,runner_started,input_manifest_ref FROM jobs WHERE run_id=?", s.run_id)
        check(job_ids == original["job_ids"] and len(job_ids) == 1, "Confirmed worker was launched again")
        answer = _counted_read(s, directory / "tool-executions.jsonl", successor, s.materials[0], "read-after-crash")
        check(ids.sha256_of_obj(answer) == original["result_sha256"], "Recovered tool reply differs")
        invocations = [json.loads(x) for x in (directory / "tool-executions.jsonl").read_text().splitlines()]
        check(len(invocations) == 1, "The lower-level artifact read ran twice")
        late = deny(lambda: s.report(worker, original["measured"]["result_ref"], "late-dead-worker"))
        # Request the same registered computation from the recovered module. GX compares
        # input bytes, method/runtime identity and original completed output before reuse.
        submitted = s.submit_node(successor, "post-crash-same-measurement")
        replay = s.settle(submitted["aliases"]["local:post-crash-same-measurement"])
        check(replay["status"] == "REUSED" and replay["job_id"] == job_ids[0]["job_id"]
              and replay["exec_key"] == current[0]["exec_key"]
              and replay["result_ref"] == original["measured"]["result_ref"],
              "Post-crash work did not verify and reuse the original Job and bytes")
        [reused_attempt] = s.rows("SELECT reused_from FROM v6_job_attempts WHERE node_id=?", replay["node_id"])
        check(reused_attempt["reused_from"] == original["measured"]["node_id"], "Reuse lost its original node")
        check(s.rows("SELECT count(*) AS n FROM jobs WHERE run_id=?", s.run_id) == [{"n": 1}],
              "A second CPU Job was launched after cold recovery")
        reporter = s.next_actor(worker.scope)
        first = s.report(reporter, replay["result_ref"], "publish-after-recovery")
        check(first == s.report(reporter, replay["result_ref"], "publish-after-recovery"),
              "Replayed publication differed")
        unknown = uncertain_launch(directory / "uncertain-admission")
        return {"status": "PASS", "uncertain_admission": unknown, "crash_exit": 83, "producer_pid": original["pid"], "recovery_pid": os.getpid(),
                "confirmed_tool_executions": invocations, "denied_old_lease": late,
                "old_lease": worker.lease_id, "new_lease": successor.lease_id, "jobs_after": job_ids,
                "exec_key": current[0]["exec_key"], "execution_identity": json.loads(current[0]["identity_json"]),
                "verified_reuse": {"recovered_node": replay["node_id"], "source_node": reused_attempt["reused_from"],
                                   "original_job": replay["job_id"], "verified_output": replay["result_ref"]},
                "reports": accepted_reports(s, 1), "before": original["history"], "after": s.trace()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path)
    parser.add_argument("--case", action="append", choices=("A2", "A3", "A4", "A5", "A6", "A7"))
    parser.add_argument("--crash-worker", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--interrupt-launch", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.interrupt_launch:
        crash_on_launch(args.interrupt_launch.resolve())
    if args.crash_worker:
        crash_after_completion(args.crash_worker.resolve())
    if not args.work or args.work.exists() or args.work.resolve().is_relative_to(PRODUCT):
        parser.error("--work must be a fresh external directory")
    args.work.mkdir(parents=True, mode=0o700)
    selected = args.case or ["A2", "A3", "A4", "A5", "A6", "A7"]
    cases = {"A2": a2, "A3": a3, "A4": a4, "A5": a5}
    if "A6" in selected:
        from acceptance.v62a.interleavings_limits import run
        cases["A6"] = run
    if "A7" in selected:
        from acceptance.v62a.interleavings_isolation import run
        cases["A7"] = run
    verifier_sources = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                        for p in sorted(Path(__file__).parent.glob("interleavings*.py"))}
    suite_commit = subprocess.check_output(["git", "-C", PRODUCT, "rev-parse", "HEAD"], text=True).strip()
    tracked = subprocess.check_output(["git", "-C", PRODUCT, "ls-files", "--cached", "--others", "--exclude-standard",
        "--", "gx-h/gx_core", "gx-h/plugins", "gx-h/contracts", "gx-h/config"], text=True).splitlines()
    files = {name: hashlib.sha256((PRODUCT / name).read_bytes()).hexdigest()
             for name in sorted(set(tracked)) if (PRODUCT / name).is_file()}
    provenance = {"commit": suite_commit, "runtime_file_count": len(files),
                  "runtime_sources_sha256": ids.sha256_of_obj(files), "acceptance_sources_sha256": verifier_sources}
    for case in selected:
        begin = time.monotonic()
        result = cases[case](args.work / (case + "-state"))
        host = "GX Roots native tool callbacks with a local scripted transport"
        if case == "A7":
            host += "; quick upgrade uses the pinned official Codex binary with a local scripted Responses peer"
        result.update(case=case, elapsed_seconds=round(time.monotonic() - begin, 3),
                      host_classification=host, paid_model_calls=0, provider_tokens=None,
                      observed_sources=provenance)
        save(args.work / (case + ".json"), result)
        print(case, result["status"], flush=True)


if __name__ == "__main__":
    main()
