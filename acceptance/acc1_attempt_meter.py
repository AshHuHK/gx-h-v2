"""Cross-attempt metering through the GX Driver, official Codex, real rejection and independent paid work."""
import argparse
from contextlib import closing, ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import threading
from unittest.mock import patch

from acc1_driver import observed, project_fixture
from e2e_responses import brief_from, fixture_action
from native_probe import Probe, find_binary
from native_probe_peer import Peer, tool
from gx_core.native_release import native_state
from gx_core.p0 import cli
from gx_core.p0.profiles import clash_env
from gx_core.persistence import records, store


class RejectedTurnPeer(Peer):
    def __init__(self, exhausted):
        super().__init__()
        self.cost = 70 if exhausted else 15
        self.awaiting_final_reply = threading.Event()
        self.release_final_reply = threading.Event()
        self.spent = {}
        self.facts = {}

    def record(self, body):
        row, n = super().record(body)
        brief = brief_from(body)
        task, attempt = brief["task_id"], brief["attempt"]
        rejected = task == self.first and attempt == 1
        if rejected:
            # The shipped scientific validator requires findings for each measured route.
            if any(i.get("type") == "function_call_output" for i in body["input"]):
                item, action = self.message(row["id"], "Received GX's rejection."), "reply"
            else:
                item, action = tool("gx_submit_product", {"product":{"findings":[]}}, row["id"]), "gx_submit_product"
        else:
            item, info = fixture_action(body, row["id"])
            action = info.get("tool", "reply")
        cap = self.cost if rejected else 15
        key = (task,attempt)
        spent = self.spent.get(key,0)
        cost = cap - spent if item["type"] == "message" else 2
        if cost < 1:
            # A host-issued extra reply is still a paid Responses request.
            action, cost = "extra_reply", 2
        row.update(task_id=task,attempt=attempt,action=action,
                   usage={"input_tokens":cost-1,"output_tokens":1,"total_tokens":cost})
        self.spent[key] = spent+cost
        self.facts[row["id"]] = item
        return row,n

    def item(self, row, n):
        if row["action"] == "reply":
            if row["task_id"] == self.first and row["attempt"] == 1:
                self.awaiting_final_reply.set()
                if not self.release_final_reply.wait(25):
                    raise TimeoutError("the other task did not finish while the rejected turn was open")
            elif row["task_id"] == self.other:
                if not self.awaiting_final_reply.wait(15):
                    raise TimeoutError("GX did not receive a rejection from the original Root")
        return self.facts[row["id"]]


def inspect(conn, budget):
    result = observed(conn,budget)
    result["paid"] = [dict(r) for r in conn.execute(
        "SELECT root.task_id,root.attempt,root.thread_id,d.turn_id,d.token_budget,b.status,"
        "b.created_at,b.settled_at,u.total_tokens,u.terminal_status "
        "FROM p0_root_deliveries d JOIN p0_roots root USING(thread_id) JOIN budget_reservations b USING(reservation_ref) "
        "LEFT JOIN p0_root_turn_usage u USING(thread_id,turn_id) WHERE d.method='turn/start' ORDER BY b.rowid")]
    result["leases"] = [dict(r) for r in conn.execute("SELECT task_id,lease_id,attempt,released_at FROM leases ORDER BY rowid")]
    return result


def fixture(conn, exhausted):
    rid,budget,(first,other) = project_fixture(conn, 10 if exhausted else 4, False)
    with store.transaction(conn) as tx:
        tx.execute("UPDATE runs SET inputs_json=json_set(inputs_json,'$.inputs.p0_spec.turn_token_budget',30) "
                   "WHERE run_id=?",(rid,))
        packet = json.loads(tx.execute("SELECT input_json FROM tasks WHERE task_id=?",(other,)).fetchone()[0])
        def next_review(deps):
            return records.create_task(tx,run_id=rid,kind="research_round",role="evidence_reviewer",
                actor="fixture",depends_on=deps,required_artifacts=["science_assessment"],
                task_input=packet,acceptance={"validate_workflow_product":True})
        third = next_review([other])
        # When all three have handed in, a fourth dependent task exposes real call exhaustion.
        last = next_review([first,third])
    return rid,budget,first,other,third,last


def check(result, first, other, third, last, exhausted):
    during, after = result["before_final_usage"],result["after"]
    rejected = [r for r in during["submissions"] if r["task_id"] == first and r["attempt"] == 1]
    assert len(rejected) == 1 and rejected[0]["result"] == "REJECTED", rejected
    old,new = [r for r in during["roots"] if r["task_id"] == first]
    assert (old["attempt"],new["attempt"]) == (1,2) and old["thread_id"] != new["thread_id"]
    assert old["lease_id"] != new["lease_id"]
    assert next(r for r in during["leases"] if r["lease_id"] == old["lease_id"])["released_at"]
    previous = next(r for r in during["paid"] if r["task_id"] == first and r["attempt"] == 1)
    assert previous["status"] == "HELD" and previous["terminal_status"] is None, previous
    assert {r["task_id"] for r in during["paid"] if r["status"] == "SETTLED"} >= {other,third}, during["paid"]
    assert all(r["task_id"] != first or r["attempt"] == 1 for r in during["paid"]), "replacement already received a paid turn"
    assert during["balance"]["available"]["tokens"] > 0 and during["balance"]["available"]["api_calls"] > 0
    assert not result["replacement_requests_before_final_usage"], "replacement already reached provider"
    assert result["status"]["status"] == "PAUSED" and result["cold_reopen_new_requests"] == 0
    assert not result["status"]["unsettled_turns"] and all(r["status"] == "SETTLED" for r in after["paid"])
    a = [r for r in after["paid"] if r["task_id"] == first]
    if exhausted:
        assert len(a) == 1 and a[0]["total_tokens"] == 70 and a[0]["token_budget"] == 30
        assert after["balance"]["spent"]["tokens"] == 100 and "tokens" in result["status"]["pause"]["reason"]
        pending = next(r for r in after["tasks"] if r["task_id"] == first)
        assert pending["attempt"] == 2 and pending["status"] == "CLAIMED"
    else:
        assert len(a) == 2 and a[1]["created_at"] >= a[0]["settled_at"]
        assert after["balance"]["spent"]["tokens"] == 60 and "api_calls" in result["status"]["pause"]["reason"]
        assert any(r["task_id"] == first and r["attempt"] == 2 and r["result"] == "ACCEPTED" for r in after["submissions"])
    assert {r["task_id"]:r["attempts_consumed"] for r in after["tasks"]} == {first:1,other:0,third:0,last:0}
    assert all(r["action"] != "extra_reply" for r in result["provider_requests"]), result["provider_requests"]


def run(folder, case, report):
    exhausted = case == "exhaustion"
    with ExitStack() as stack:
        peer = stack.enter_context(RejectedTurnPeer(exhausted))
        server = threading.Thread(target=peer.serve_forever,daemon=True)
        server.start()
        stack.callback(server.join)
        stack.callback(peer.shutdown)
        stack.callback(peer.release_final_reply.set)
        probe = Probe(folder/"official",find_binary(),peer)
        stack.enter_context(patch.dict(os.environ,{**probe.env,"GX_P0_TEST":"1","GX_P0_TEST_PROVIDER":probe.provider,
                                                  "GX_P0_CODEX":str(probe.binary),"GX_P0_MODEL":"gpt-6.2-astra"}))
        clash_env()
        state = stack.enter_context(native_state(folder/"project"))
        conn = stack.enter_context(closing(store.open_store(state.db_path)))
        rid,budget,first,other,third,last = fixture(conn,exhausted)
        peer.first,peer.other = first,other
        report.update(run_id=rid,tasks={"rejected":first,"independent":other,"depends_only_on_independent":third,
                                      "depends_on_both_paths":last})
        stop = threading.Event()
        def release_after_independent_work():
            with closing(cli.existing_db(state.directory,readonly=True)) as db:
                while not stop.wait(0.025):
                    if not peer.awaiting_final_reply.is_set():
                        continue
                    snapshot = inspect(db,budget)
                    if (any(r["task_id"] == first and r["attempt"] == 2 and r["thread_id"] for r in snapshot["roots"])
                            and any(r["task_id"] == third and r["status"] == "SETTLED" for r in snapshot["paid"])):
                        # The public Driver has now had a chance to try the replacement. Wait until it
                        # either started early, or GX durably recorded its attempt-level metering wait.
                        waits = cli.snapshot(db).get("attempt_waits",[])
                        if not (any(r["task_id"] == first and r["attempt"] == 2 for r in snapshot["paid"])
                                or any(w["task_id"] == first and w["attempt"] == 2 and w["reason"] == "metering" for w in waits)):
                            continue
                        report["before_final_usage"] = {**snapshot,"attempt_waits":waits}
                        report["replacement_requests_before_final_usage"] = sum(
                            r.get("task_id") == first and r.get("attempt") == 2 for r in peer.rows)
                        peer.release_final_reply.set()
                        return
        watch = threading.Thread(target=release_after_independent_work,daemon=True)
        watch.start()
        try:
            report["status"] = cli.run_until_stopped(conn,state.directory,rid,18)
        finally:
            stop.set()
            watch.join()
            report["after"] = inspect(conn,budget)
            report["provider_requests"] = [{k:r[k] for k in ("id","task_id","attempt","action","usage") if k in r}
                                           for r in peer.rows]
        if report["status"]["status"] == "PAUSED":
            count = len(peer.rows)
            with closing(cli.existing_db(state.directory,readonly=False)) as cold:
                twice = cli.run_until_stopped(cold,state.directory,rid,2)
                assert twice["status"] == "PAUSED" and inspect(cold,budget) == report["after"]
            report["cold_reopen_new_requests"] = len(peer.rows)-count
        check(report,first,other,third,last,exhausted)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case",choices=("remaining","exhaustion"),required=True)
    parser.add_argument("--out",type=Path,required=True)
    args=parser.parse_args()
    report={"case":args.case,"entry":"gx_core.p0.cli.run_until_stopped","codex":"official App Server 0.169.0",
            "inference":"deterministic loopback Responses","started":datetime.now(timezone.utc).isoformat(),"result":"FAIL"}
    try:
        with tempfile.TemporaryDirectory(prefix="gx-acc1-attempt-meter-") as temp:
            run(Path(temp),args.case,report)
        report["result"]="PASS"
    except Exception as exc:
        report["error"]=f"{type(exc).__name__}: {exc}"
        raise
    finally:
        report["ended"] = datetime.now(timezone.utc).isoformat()
        args.out.parent.mkdir(parents=True,exist_ok=True)
        args.out.write_text(json.dumps(report,indent=2)+"\n")
