"""Read a preserved official turn and exercise GX cold reconciliation on a disposable ledger copy."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v623.oauth import host
from gx_core.hostobs.app_server_client import AppServerClient, AppServerResponseError
from gx_core.p0 import profiles, root_delivery
from gx_core.p0.roots import Roots


def ledger(db):
    return [dict(row) for row in db.execute("""SELECT thread_id,turn_id,budget_status,confidence,terminal_status,
        prior_tokens,total_tokens,measured_tokens,estimated_at,reservation_ref FROM p0_meter_native_turns ORDER BY sequence""")]


def tail(lines, turn):
    records, pending, boundary = {}, [], None
    cumulative = None
    awaiting = False
    last_time = None
    started = False
    for line in lines:
        doc = json.loads(line)
        typ, p, stamp = doc["type"], doc.get("payload") or {}, doc["timestamp"]
        kind = p.get("type")
        if typ == "event_msg" and kind == "task_started":
            started = p.get("turn_id") == turn
        if not started:
            continue
        if typ == "token_usage_record" and p["turn_id"] == turn:
            records[p["response_id"]] = p["usage"]["total_tokens"]
            pending = []
            last_time = stamp
            awaiting = True
        elif typ == "event_msg" and kind == "token_count":
            total = ((p.get("info") or {}).get("total_token_usage") or {}).get("total_tokens")
            if not awaiting:
                pending.append({"at":stamp,"event":"unpaired_token_count","total_tokens":total,
                                "repeats_previous":total==cumulative})
            cumulative = total
            awaiting = False
        elif typ == "response_item" and kind in ("reasoning","message"):
            pending.append({"at":stamp,"event":kind,
                            **({"role":p.get("role")} if kind=="message" else {})})  # Never export message or reasoning contents.
        elif typ == "event_msg" and kind in ("task_complete","turn_aborted"):
            boundary = {"at":stamp,"event":kind,"reason":p.get("reason")}
            break
    return records, {"covered_responses":len(records),"covered_response_tokens":sum(records.values()),
                     "ids_and_usage_sha256": hashlib.sha256(json.dumps(sorted(records.items())).encode()).hexdigest(),
                     "last_response_at":last_time,"unmatched_tail":pending,"terminal":boundary}


def audit(slot):
    binary, pin = host._pin(ROOT/"vendor/codex-runtime/package/vendor/x86_64-unknown-linux-musl/bin/codex")
    reg = json.loads((slot.parent/"registration.json").read_text())
    out = {"schema":"gx.oauth.interrupted-turn-audit/1","candidate":reg["source_commit"],
           "series":reg["series"],"slot":slot.name,"official_binary_sha256":pin,
           "model_inference_calls":0,"live_store_writes":0,"previous_charge_overwrites":0}
    with closing(sqlite3.connect((slot/"state/gx.db").as_uri()+"?mode=ro",uri=True)) as original:
        original.row_factory = sqlite3.Row
        before = ledger(original)
        held, = [r for r in before if r["budget_status"]=="HELD"]
        rid, = original.execute("SELECT run_id FROM v6_goals").fetchone()
        tid, turn = held["thread_id"], held["turn_id"]
        path, = (slot/"codex/sessions").rglob("*"+tid+"*.jsonl")
        with path.open(encoding="utf-8") as stream:
            records, out["original_rollout_tail"] = tail(stream,turn)
        metered = dict(original.execute("SELECT response_id,tokens FROM p0_meter_responses WHERE thread_id=? AND turn_id=?",(tid,turn)))
        with path.open("rb") as f:
            original_sha=hashlib.file_digest(f,"sha256").hexdigest()
        out.update(run_id=rid,thread_id=tid,turn_id=turn,read_only_ledger_before=before,
                   gx_provider_response_receipts_equal_original=(metered==records),
                   confirmed_other_turn_tokens=sum(r["measured_tokens"] for r in before if r["confidence"]=="PROVIDER_CONFIRMED"),
                   acknowledged_response_lower_bound=sum(records.values())+sum(r["measured_tokens"] for r in before if r["confidence"]=="PROVIDER_CONFIRMED"),
                   gx_exact_rollout_proof=root_delivery._recorded_usage(path,tid,turn,tid,turn,0) is not None,
                   original_rollout_sha256=original_sha)
        out["existing_jobs"], = original.execute("SELECT COUNT(*) FROM jobs").fetchone()
        out.update(additional_jobs=0, additional_debits=0, extra_judge_turns=0,
                   original_target_status=json.loads((slot/"accounting.json").read_bytes())["status"],
                   exact_slot_usage=None, invoice_usd=None)
        from acceptance.v623.oauth.usage import reconcile
        reviewed = reconcile(slot,rid)
        out["hardened_read_only_reconciliation"] = reviewed
        out["signed_off_for_next_slot"] = reviewed["can_continue"]
        out["audit_checkout_head"] = subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
        sources = ["acceptance/v623/oauth/audit_tail.py","acceptance/v623/oauth/usage.py",
                   "gx-h/gx_core/p0/root_delivery.py","gx-h/gx_core/p0/metering.py"]
        out["audit_source_sha256"] = {n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in sources}
        out["original_accounting_sha256"] = hashlib.sha256((slot/"accounting.json").read_bytes()).hexdigest()
        with TemporaryDirectory(prefix="gx-read-original-") as tmp:
            private = Path(tmp)
            copy = private/"sessions"/path.relative_to(slot/"codex/sessions")
            copy.parent.mkdir(parents=True)
            shutil.copyfile(path,copy)
            env = host._env(private/"home",binary,reg["model"],reg["effort"],sessions=private/"sessions")
            # Include the original official thread projection. The serialized rollout alone
            # can resolve thread/read before the cold turns-list index has been populated.
            original_home = host._state() / ("session-" + hashlib.sha256(str(slot).encode()).hexdigest())
            for filename in ("state_5.sqlite", "thread_history_1.sqlite"):
                with closing(sqlite3.connect((original_home/filename).as_uri()+"?mode=ro",uri=True)) as src:
                    with closing(sqlite3.connect(private/"home"/filename)) as target:
                        src.backup(target)
            with closing(sqlite3.connect((original_home/"thread_history_1.sqlite").as_uri()+"?mode=ro",uri=True)) as history:
                state = history.execute("SELECT status,duration_ms FROM thread_turns WHERE thread_id=? AND turn_id=?",
                                        (tid,turn)).fetchone()
                out["official_native_turn_index"]={"status":state[0],"duration_ms":state[1]} if state else None
            argv = [str(binary),*profiles.app_server_argv(provider="openai",test_mode=False)[1:]]
            allowed = {"initialize","config/read","account/read","account/usage/read",
                       "thread/read","thread/turns/list","thread/items/list"}
            with closing(sqlite3.connect(private/"rehearsal.db",isolation_level=None)) as db, AppServerClient(
                    argv,cwd=private,env=env,default_timeout=20) as client:
                original.backup(db)
                db.row_factory = sqlite3.Row
                counts = Counter()
                class ReadOnly:
                    @staticmethod
                    def request(method,params,**kwargs):
                        assert method in allowed, "Unexpected RPC outside read-only recovery"
                        counts[method] += 1
                        reply = client.request(method,params,**kwargs)
                        if method=="thread/turns/list":
                            out["projected_turns"]=[{"turn_id":r["id"], "status":r["status"]} for r in reply["data"]]
                        return reply
                rpc = ReadOnly()
                rpc.request("initialize",{"clientInfo":{"name":"gx_reconcile_read","version":"1"},
                            "capabilities":{"experimentalApi":True}})
                client.notify("initialized")
                cfg = rpc.request("config/read",{"includeLayers":False,"cwd":str(private)})["config"]
                if not host.deployment._allowed_config(cfg) or cfg.get("forced_login_method")!="chatgpt":
                    raise ValueError("Official route mismatch")
                acc = rpc.request("account/read",{"refreshToken":False}).get("account") or {}
                if acc.get("type")!="chatgpt":
                    raise ValueError("Existing ChatGPT OAuth required for nonbilling metadata read")
                try:
                    answer = rpc.request("account/usage/read",{"threadId":tid})
                    est = answer.get("threadUsage")
                    out["official_thread_usage"] = {"api":"account/usage/read","contract":"estimated per thread",
                                                     "returned_thread_usage":isinstance(est,dict),"confirmed_invoice":False,
                                                     "field_names":sorted(est.keys()) if isinstance(est,dict) else []}
                except AppServerResponseError as err:
                    out["official_thread_usage"] = {"api":"account/usage/read","contract":"estimated per thread","error_code":err.code}
                role = Roots(db,run_id=rid,cwd=private)
                role.client = rpc
                view=rpc.request("thread/read",{"threadId":tid,"includeTurns":False})["thread"]
                if view["id"]!=tid or Path(view["path"]).resolve() not in (copy.resolve(), path.resolve()):
                    raise ValueError("Official read did not resolve exact original rollout copy")
                role.delivery.attached(tid,view)
                role.delivery.reconcile(tid)
                out["native_read_rpcs"] = dict(counts)
                out["scratch_owner_result"] = next(row for row in ledger(db) if row["turn_id"]==turn)
                out["original_ledger_unchanged"] = before==ledger(original)
    return out


if __name__ == "__main__":
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slot",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    args=ap.parse_args()
    value=audit(args.slot.resolve())
    args.out.write_text(json.dumps(value,indent=2)+"\n")
    print("Official read-only recovery:", value["scratch_owner_result"]["budget_status"], value["scratch_owner_result"]["confidence"])
