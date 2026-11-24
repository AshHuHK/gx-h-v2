"""P2A.1 official child recovery across a GX crash, two dropped frames and the metering grace."""
import argparse
from contextlib import closing
from datetime import timedelta
import gzip
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "acceptance"), str(ROOT / "gx-h")]
from acc1_protocol import MeterPeer, binary_facts
from archive import read, save, sha256
from child_wire import Wire
from gx_core import budgets
from gx_core.capabilities import storage
from gx_core.domain import clock
from gx_core.errors import ScopeDenied
from gx_core.p0 import terminal_products
from gx_core.hostobs.host_policy import p0_host
from gx_core.jobs.grid_workflow import observe_grid, submit_grid
from gx_core.p0 import driver, metering, schema
from gx_core.p0.roots import Roots
from gx_core.persistence import records, store
from native_probe import Probe, _proxy_preflight
from official_usage import balance

GRACE = 5
ACTOR = "official-child-double-loss"


class Peer(MeterPeer):
    def item(self, row, n):
        if not row["child"] and n != 1 and not self.release_child.wait(40):
            raise TimeoutError("GX restart never released the original parent response")
        return super().item(row, n)


def wait_for(predicate, description, timeout=25, proc=None):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        if value := predicate():
            return value
        if proc and proc.poll() is not None:
            raise RuntimeError(f"{description}: GX process exited {proc.returncode}")
        time.sleep(.025)
    raise TimeoutError(description)


def setup(db):
    with closing(store.open_store(db)) as conn:
        schema.install(conn)
        document, digest, host = p0_host()
        budget = budgets.create_root(conn, run_id=None, profile_name="p0", actor=ACTOR)
        # Run IDs are assigned within this fixture creation transaction.
        with store.transaction(conn, transaction_owner="Acceptance", operation="create_scoped_job_run"):
            run = records.create_run(conn, objective="Check a local arithmetic Job", project_ref=ACTOR,
                actor=ACTOR, workflow_id="science_grid", workflow_version="3", agent_policy=document,
                pins={"agent_policy_sha256":digest}, host=host, budget_ref=budget,
                inputs={"inputs":{"p0":True, "p0_spec":{"metering":{"grace_seconds":GRACE, "rollout_diagnostics":False}}}})
            foreign = records.create_run(conn, objective="Separate notebook", project_ref=ACTOR + "-foreign", actor=ACTOR)
        with store.transaction(conn, transaction_owner="Acceptance", operation="activate_job_run", run_id=run):
            for state in ("AUDITING", "PLANNING", "ACTIVE"):
                store.transition(conn, machine="run", subject_id=run, to_state=state, actor=ACTOR)
        grid = {"synthetic":True, "method_id":"fixture-grid", "method_version":"1",
                "candidates":[{"candidate_id":"double-loss", "x":2}]}
        source = storage.artifact_service(conn).publish_bytes(namespace=f"{run}/input", rel_path="grid.json",
            kind="job_input", run_id=run, data=json.dumps(grid)).artifact_ref
        job = submit_grid(conn, run_id=run, operation_id="original-grid", input_ref=source)["job_id"]
        job_dir = storage.jobs_root(conn) / job
        wait_for((job_dir / "outcome.json").is_file, "original CPU Job did not finish")
        observed = observe_grid(conn, job)
        assert observed["status"] == "SUCCEEDED", observed
        return {"run_id":run, "foreign_run":foreign, "budget":budget, "job":job,
                "job_outcome":read(job_dir / "outcome.json")}


def rows(conn, sql, args=()):
    return [dict(row) for row in conn.execute(sql, args)]


def snapshot(conn, case):
    return {"balance":balance(conn,case["root"],case["turn"]),
            "jobs":rows(conn,"SELECT job_id,run_id,status,idempotency_key,scheduler_ref,reservation_ref FROM jobs ORDER BY rowid"),
            "deliveries":rows(conn,"SELECT run_id,event_id,thread_id,turn_id,reservation_ref FROM p0_root_deliveries ORDER BY rowid"),
            "child_meters":rows(conn,"SELECT * FROM p0_native_child_meters ORDER BY rowid"),
            "child_turns":rows(conn,"SELECT * FROM p0_native_child_usage ORDER BY rowid"),
            "root_bindings":rows(conn,"SELECT run_id,thread_id,role,task_id,lease_id FROM p0_roots ORDER BY rowid"),
            "native_funds":rows(conn,"SELECT run_id,thread_id,turn_id,reservation_ref,budget_ref FROM p0_meter_native_turns ORDER BY sequence"),
            "reservations":rows(conn,"SELECT reservation_ref,budget_ref,status FROM budget_reservations ORDER BY rowid"),
            "responses":rows(conn,"SELECT * FROM p0_meter_responses ORDER BY rowid")}


def cold_history(roots, out):
    original = roots._request

    def request(method, params):
        reply = original(method, params)
        if method in ("thread/read","thread/resume","thread/turns/list","thread/turn/items/list","turn/start"):
            thread = reply.get("thread")
            result = ({key:thread.get(key) for key in ("id","source","sessionId","parentThreadId","forkedFromId","status","turns")}
                      if thread is not None else reply)
            with (out / "cold-history.jsonl").open("a") as sink:
                sink.write(json.dumps({"method":method, "thread":params.get("threadId"), "reply":result}) + "\n")
        return reply
    roots._request = request


def worker(phase, work, out, binary):
    case = read(work / "case.json")
    with closing(store.open_store(work / "gx.sqlite")) as conn:
        if phase == "warm":
            argv = [sys.executable, "-B", str(Path(__file__).with_name("child_wire.py")), case.pop("relay_socket")]
            with Roots(conn, run_id=case["run_id"], cwd=work/"official/work", argv=argv, env=os.environ, token_budget=1000) as roots:
                root = roots.ensure_main()
                turn = roots.human_turn("Delegate one check of the original local arithmetic Job " + case["job"],
                                        message_id="original-grid-check")
                case.update(root=root, turn=turn)
                save(out / "warm-binding.json", {**case,"pid":os.getpid()})
                roots.client.run_until(lambda _: False, timeout=80)
                raise RuntimeError("warm GX survived the prescribed crash")
        save(out / "cold-start.json", {"pid":os.getpid(),"ns":time.monotonic_ns()})
        wait_for((work / "history-ready").exists, "original native host never closed", timeout=45)
        case = read(out / "warm-binding.json")
        before = snapshot(conn, case)
        save(out / "before-recovery.json", before)
        assert before["balance"]["budget_status"] == "HELD" and not before["child_meters"] and not before["child_turns"], before
        argv = [str(binary), "app-server", "--listen", "stdio://"]
        with Roots(conn, run_id=case["run_id"], cwd=work/"official/work", argv=argv, env=os.environ, token_budget=1000) as roots:
            cold_history(roots, out)
            roots.reconcile_meter(case["root"])
            deadline = time.monotonic() + 8
            while not conn.execute("SELECT 1 FROM p0_native_child_usage WHERE terminal=1").fetchone() and time.monotonic() < deadline:
                roots.poll(.05)
            usage = conn.execute("SELECT child_id, turn_id FROM p0_native_child_usage WHERE terminal=1").fetchone()
            assert usage is not None, "cold history did not identify a terminal child"
            # Negative control at the GX callback boundary, separate from the official wire.
            call = {"threadId": usage["child_id"], "turnId": usage["turn_id"], "callId": "scope-control",
                    "namespace": terminal_products.NAMESPACE, "tool": "gx_submit_product", "arguments": {}}
            try:
                roots._call(call)
            except ScopeDenied as exc:
                assert "only the assigned GX root" in str(exc), exc
                save(out / "write-denial-control.json", {"kind": "scripted callback control", "call": call,
                                                       "code": exc.code, "reason": str(exc)})
            else:
                raise AssertionError("terminal child obtained GX product submission authority")
        recovered = snapshot(conn, case)
        save(out / "after-history.json", recovered)
        child, = recovered["child_meters"]
        usage, = recovered["child_turns"]
        assert child["run_id"] == case["run_id"] and child["parent_id"] == child["root_id"] == case["root"]
        assert child["metadata_verified"] and usage["child_id"] == child["child_id"] and usage["terminal"]
        assert usage["owner_turn"] == case["turn"] and recovered["balance"]["terminal_status"] == "completed", recovered
        with store.transaction(conn, transaction_owner="Acceptance", operation="pause_completed_fixture", run_id=case["run_id"]):
            store.transition(conn, machine="run",subject_id=case["run_id"],to_state="PAUSED",actor=ACTOR)
        assert recovered["balance"]["budget_status"] == "HELD" and not recovered["balance"]["settlements"]
        assert metering.policy(conn, case["run_id"])["grace_seconds"] == GRACE
        with patch.dict(os.environ, {driver.OFFLINE:"1"}):
            remaining = max(0, GRACE - clock.age_seconds(recovered["balance"]["terminal_at"]))
            with closing(driver.Driver(conn, work, case["run_id"])) as owner:
                owner.drive(remaining + 1.25)
            settled = snapshot(conn, case)
            with closing(driver.Driver(conn, work, case["run_id"])) as owner:
                owner.drive(.35)
            replay = snapshot(conn, case)
        save(out / "post-grace.json", settled)
        save(out / "repeat-driver.json", replay)
        assert clock.age_seconds(recovered["balance"]["terminal_at"]) >= GRACE
        assert settled["balance"]["budget_status"] == "SETTLED", settled
        account = settled["balance"]
        assert account["confidence"] == "ESTIMATED" and account["settled_tokens"] == account["token_budget"]
        assert clock.parse(account["estimated_at"]) >= clock.parse(account["terminal_at"]) + timedelta(seconds=GRACE)
        assert len(account["settlements"]) == 1
        assert settled["balance"]["settlements"] == replay["balance"]["settlements"]
        assert {k:v for k,v in settled["balance"].items() if k!="read_at"} == {k:v for k,v in replay["balance"].items() if k!="read_at"}
        assert before["native_funds"] == replay["native_funds"]
        assert before["root_bindings"] == replay["root_bindings"] == [
            {"thread_id": case["root"], "run_id": case["run_id"], "role": "main", "task_id": None, "lease_id": None}]
        assert before["jobs"] == replay["jobs"] and before["deliveries"] == replay["deliveries"] and len(before["jobs"]) == len(before["deliveries"]) == 1
        assert [(r["reservation_ref"],r["budget_ref"]) for r in before["reservations"]] == [(r["reservation_ref"],r["budget_ref"]) for r in replay["reservations"]]
        fund, = replay["native_funds"]
        assert (fund["run_id"],fund["thread_id"],fund["turn_id"]) == (case["run_id"],case["root"],case["turn"])
        assert fund["budget_ref"] == case["budget"] and fund["reservation_ref"] == account["reservation_ref"]
        assert replay["jobs"][0]["job_id"] == case["job"] and replay["jobs"][0]["run_id"] == case["run_id"]
        assert not conn.execute("SELECT 1 FROM p0_meter_threads WHERE run_id=?", (case["foreign_run"],)).fetchone()


def command(phase, work, out, binary, env):
    argv = [sys.executable,"-B",str(Path(__file__).resolve()),"--worker",phase,"--work",str(work),"--out",str(out),"--binary",str(binary)]
    with (out / (phase + ".log")).open("w") as log:
        return subprocess.Popen(argv, cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)


def experiment(binary, out, work, socket_path):
    _proxy_preflight()
    lock = read(ROOT/"release-assets/lock.json")["linux_x64"]
    host = binary_facts(binary)
    assert host["sha256"] == lock["binary_sha256"]
    peer = Peer()
    peer.scenario("delayed_child")
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    probe = Probe(work/"official",binary,peer)
    profile_env = {key: os.environ[key] for key in ("GX_TEST_RESOURCE_PROFILE", "GX_ACCEPTANCE_RESOURCE_SCOPE",
                                                    "CUDA_VISIBLE_DEVICES") if key in os.environ}
    env = {**probe.env, **profile_env,"GX_P0_TEST":"1","GX_P0_TEST_PROVIDER":probe.provider,"GX_P0_MODEL":"gpt-6.1-sol","GX_P0_CODEX":str(binary),
           "PYTHONDONTWRITEBYTECODE":"1"}
    profile = work / "roles.json"
    save(profile, {"roles":{"main":{"multi_agent":True}}})
    env["GX_P0_PROFILE"] = str(profile)
    warm = cold = wire = None
    try:
        with patch.dict(os.environ, env):
            case = setup(work/"gx.sqlite")
        save(work/"case.json", {**case, "relay_socket": str(socket_path)})
        wire = Wire(work, binary, env, out, socket_path)
        warm = command("warm",work,out,binary,env)
        wait_for((out/"warm-binding.json").exists,"persist original parent binding",proc=warm)
        case = read(out/"warm-binding.json")
        def drops(kind):
            return [f for f in wire.frames if f["disposition"] == kind]
        wait_for(lambda: any(r["frame"]["method"]=="item/completed" for r in drops("drop_spawn"))
                 and drops("withhold_child_start") and len(peer.rows)==3,"original spawn, child start and three admitted requests",proc=warm)
        warm.kill()
        assert warm.wait(timeout=5) == -9
        crash_ns = time.monotonic_ns()
        cold = command("cold",work,out,binary,env)
        ready = wait_for(lambda: read(out/"cold-start.json") if (out/"cold-start.json").exists() else None,
                         "cold GX did not reopen store",proc=cold)
        lost, = drops("withhold_child_start")
        cparams = lost["frame"]["params"]
        child, child_turn = cparams["threadId"], cparams["turn"]["id"]
        assert ready["pid"] != warm.pid and lost["ns"] <= crash_ns < ready["ns"]
        decision_ns = time.monotonic_ns()
        save(out/"lost-start.json",{"actual_host_frame":lost, "gx_crash_ns":crash_ns, "gx_restarted_ns":ready["ns"],
                                   "discarded_ns":decision_ns,"action":"drop withheld original child turn/started before releasing the original response"})
        peer.release_child.set()
        def completed(thread, turn):
            return next((r for r in wire.frames if r["frame"]["method"]=="turn/completed" and
                         r["frame"]["params"]["threadId"]==thread and r["frame"]["params"]["turn"]["id"]==turn), None)
        terminal = wait_for(lambda: completed(child,child_turn),"official child did not complete during restart",proc=cold)
        parent_terminal = wait_for(lambda: completed(case["root"],case["turn"]),"original parent did not complete",proc=cold)
        assert terminal["ns"] >= decision_ns and terminal["frame"]["params"]["turn"]["status"] == "completed"
        assert parent_terminal["frame"]["params"]["turn"]["status"] == "completed"
        call_ids = [r["id"] for r in peer.rows]
        wire.shutdown()
        assert not wire.errors and wire.process.returncode == 0, (wire.errors, wire.process.returncode)
        (work/"history-ready").touch()
        assert cold.wait(timeout=40) == 0, (out/"cold.log").read_text()
        final = read(out/"repeat-driver.json")
        usage, = final["child_turns"]
        assert (usage["child_id"],usage["turn_id"],usage["owner_turn"]) == (child,child_turn,case["turn"])
        spawn_ids = {r["frame"]["params"]["item"].get("agentThreadId") for r in drops("drop_spawn")}
        assert child in spawn_ids and child != case["root"]
        assert len(call_ids)==len(set(call_ids))==3 and sum(r["child"] for r in peer.rows)==1
        assert [r["id"] for r in peer.rows] == call_ids
        history = [json.loads(line) for line in (out/"cold-history.jsonl").read_text().splitlines()]
        assert any(r["method"]=="thread/read" and r["thread"]==child for r in history)
        assert not any(r["method"] == "turn/start" for r in history)
        parent_items = [item for r in history if r["thread"] == case["root"] for turn in r["reply"].get("turns", [])
                        if turn["id"] == case["turn"] for item in turn["items"]]
        assert any(i.get("type") == "subAgentActivity" and i.get("kind") == "started"
                   and i.get("agentThreadId") == child for i in parent_items)
        host_receipts = [r["frame"]["params"] for r in wire.frames if r["frame"]["method"] == "rawResponse/completed"]
        expected = {r["id"]: (child if r["child"] else case["root"], child_turn if r["child"] else case["turn"],
                               r["usage"]["total_tokens"]) for r in peer.rows}
        actual = {r["responseId"]: (r["threadId"], r["turnId"], r["usage"]["totalTokens"]) for r in host_receipts}
        assert len(host_receipts) == len(expected) and actual == expected, host_receipts
        return {"status":"PASS","closure":"CLOSED","case":"child-double-loss","run_id":case["run_id"],
            "root":case["root"],"turn":case["turn"],"child":child,"child_turn":child_turn,"foreign_run":case["foreign_run"],
            "job":case["job"], "original_model_request_ids":call_ids, "extra_model_requests":0, "duplicate_jobs":0,
            "wire_usage_tokens":sum(amount for _, _, amount in actual.values()),
            "observable_gx_lower_bound":final["balance"]["observed_lower_bound"],
            "original_gx_pid":warm.pid,"restarted_gx_pid":ready["pid"],"original_native_pid":wire.process.pid,
            "grace_seconds":GRACE, "settled":final["balance"], "official_host":host,
            "original_native_exit_code":wire.process.returncode, "restarted_native_history":"cold-history.jsonl",
            "checks":{"real_spawn_lost":True,"process_restart":True,"real_child_start_lost":True,
                "child_completed_after_restart":True,"persisted_parent_discovered_child":True,
                "recovered_child_write_denied":True, "original_root_is_only_gx_authority":True,
                "run_parent_turn_scoped":True,"one_immutable_settlement":True,"no_extra_calls_or_jobs":True}}
    finally:
        peer.release_child.set()
        for proc in (warm,cold):
            if proc and proc.poll() is None:
                proc.kill()
                proc.wait()
        if wire:
            wire.shutdown()
        peer.shutdown()
        peer.server_close()
        save(out/"provider-requests.json",peer.rows)
        if (work/"gx.sqlite").is_file():
            with closing(sqlite3.connect(work/"gx.sqlite")) as src, closing(sqlite3.connect(work/"copy.sqlite")) as dst:
                src.backup(dst)
            with (work/"copy.sqlite").open("rb") as src, gzip.open(out/"ledger.sqlite.gz","wb") as dst:
                shutil.copyfileobj(src,dst)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out",type=Path,required=True)
    p.add_argument("--binary",type=Path)
    p.add_argument("--worker",choices=("warm","cold"),help=argparse.SUPPRESS)
    p.add_argument("--work",type=Path,help=argparse.SUPPRESS)
    args = p.parse_args()
    out = args.out.resolve()
    relative = Path("vendor/codex-runtime/package") / read(ROOT/"release-assets/lock.json")["linux_x64"]["binary"]
    binary = (args.binary or (ROOT / relative if (ROOT / relative).is_file() else ROOT.parent/"gx-h-v-3"/relative)).resolve()
    if args.worker:
        worker(args.worker,args.work,out,binary)
        return
    out.mkdir(parents=True,exist_ok=True)
    if any(out.iterdir()):
        p.error("choose an empty evidence directory")
    source = {p.name:sha256(p) for p in (Path(__file__),Path(__file__).with_name("child_wire.py"))}
    revision = subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    receipt = {"status":"FAIL","closure":"OPEN","argv":sys.orig_argv,"revision":revision,"source_sha256":source}
    try:
        # Only the socket needs Linux's short sockaddr_un path. All other files
        # stay under the caller's TMPDIR or requested evidence directory.
        with tempfile.TemporaryDirectory(prefix="gx-child-loss-") as tmp, \
                tempfile.TemporaryDirectory(prefix="gx-wire-", dir="/tmp") as sockets:
            receipt.update(experiment(binary, out, Path(tmp), Path(sockets) / "gx.sock"))
    except Exception as exc:
        receipt["error"] = repr(exc)
        raise
    finally:
        save(out/"receipt.json",receipt)
        save(out/"manifest.json",{path.name:sha256(path) for path in sorted(out.iterdir()) if path.is_file()})
    print(json.dumps({key:receipt[key] for key in ("case","status","closure","run_id","extra_model_requests")}))


if __name__ == "__main__":
    main()
