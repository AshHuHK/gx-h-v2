"""Exercise operator recovery of an actual Codex rollout inside the isolated official-host fixture."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/"gx-h"),str(ROOT/"acceptance"),str(ROOT/"acceptance/v21")]
from acc1_protocol import binary_facts, fixture
from native_activity import capture, project
from native_budget import wait
from native_events import EventProbe
from native_probe import DYN_SPEC, find_binary, _proxy_preflight
from gx_core import budgets
from gx_core.domain import clock
from gx_core.p0 import metering, operator
from gx_core.p0.roots import Roots


def measure(probe,out):
    probe.peer.scenario("single_response")
    probe.peer.item=lambda row,index:probe.peer.message(row["id"],"4")
    with project(probe,grant=100,grace=1) as (conn,run,budget,env):
        with Roots(conn,run_id=run,cwd=probe.cwd,env=env,token_budget=100,main_tools=[DYN_SPEC],
                   main_call=lambda *args:{"result":4}) as roots:
            capture(roots,probe)
            original=roots.delivery.observe
            def drop_usage(method,params):
                if method not in ("rawResponse/completed","thread/tokenUsage/updated"):
                    original(method,params)
            roots.delivery.observe=drop_usage
            thread=roots.ensure_main()
            turn=roots.human_turn("Return the value of two plus two.",message_id="input")
            assert wait(roots,lambda:any(r["method"]=="turn/completed" and r.get("turn")==turn for r in probe.log))
            info=roots._request("thread/read",{"threadId":thread,"includeTurns":False})["thread"]
            roots.delivery.attached(thread,info)
        native=next(r for r in probe.log if r["method"]=="rawResponse/completed" and r.get("turn")==turn)
        rid=native.get("responseId") or native.get("response_id")
        if not rid:
            rid=probe.peer.rows[0]["id"]
        ref=conn.execute("SELECT reservation_ref FROM p0_meter_native_turns WHERE turn_id=?",(turn,)).fetchone()[0]
        # Reproduce a historical reservation missing its token grant. The real host completed
        # the original Turn; only the GX notification fault and historical funding shape are supplied.
        conn.execute("UPDATE p0_root_deliveries SET token_budget=NULL WHERE reservation_ref=?",(ref,))
        conn.execute("UPDATE budget_reservations SET amounts_json=? WHERE reservation_ref=?",('{"api_calls":1}',ref))
        conn.execute("UPDATE budgets SET reserved_json=? WHERE budget_ref=?",('{"api_calls":1}',budget))
        clock.advance(2)
        metering.maintain(conn,run)
        def show():
            s=metering.inspect(conn,run)
            return {"ticket":s["turns"][0],"budget":budgets.state(conn,budget)["spent"],"unresolved":s["unresolved_cases"]}
        before=show()
        def action(decision,key,**opts):
            return operator.submit(operator.local_context(conn),run,{"kind":"usage_resolve","decision":decision,
                "reason":"Recover the original host receipt","reservation_ref":ref,**opts},key)["result"]
        attached=action("attach-evidence","attach",source="codex-rollout",thread_id=thread,turn_id=turn,response_id=rid)
        intermediate=show()
        end=action("reconcile","reconcile")
        after=show()
        assert before["ticket"]["confidence"]=="UNRESOLVED" and before["budget"]=={}
        assert intermediate["ticket"]["budget_status"]=="HELD" and intermediate["budget"]=={}
        assert after["ticket"]["confidence"]=="PROVIDER_CONFIRMED" and after["budget"]=={"api_calls":1,"tokens":35}
        assert action("reconcile","reconcile")==end and len(probe.peer.rows)==1
        path=Path(info["path"])
        with path.open("rb") as stream:
            raw_sha=hashlib.file_digest(stream,"sha256").hexdigest()
        with closing(sqlite3.connect(out/"ledger.db")) as backup:
            conn.backup(backup)
        return {"run":run,"thread":thread,"turn":turn,"response":rid,"raw_receipt":native,
            "provider_requests":len(probe.peer.rows),"original_rollout_sha256":raw_sha,"entry_before":before,
            "attachment":attached,"after_attachment":intermediate,"reconciliation":end,"end":after,
            "fixture_fault":"Two native notification methods withheld from GX; historical reservation had no token grant.",
            "scope":"Actual official Codex local stdio and its actual runtime-written rollout; scripted loopback provider."}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output",type=Path,required=True)
    args=p.parse_args()
    args.output.mkdir(parents=True,exist_ok=False)
    _proxy_preflight()
    binary=find_binary()
    with fixture(binary,probe_type=EventProbe) as probe:
        result=measure(probe,args.output)
    result.update(binary_facts(binary),passed=True,evidence_class="OFFICIAL_HOST_LOOPBACK")
    (args.output/"result.json").write_text(json.dumps(result,indent=2)+"\n")
    print("PASS official attached history, explicit reconciliation, ledger and idempotent replay")


if __name__=="__main__":
    main()
