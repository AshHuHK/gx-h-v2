"""One loopback Responses endpoint for an enrolled slot, fenced at every physical request."""
from contextlib import contextmanager, closing
from datetime import datetime, timezone
from functools import partial
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from threading import Lock, Thread
from uuid import uuid4

from acceptance.v62c import rehearse, materials
from acceptance.v622.public import offline, scripted
from acceptance.v622.public.enrollment import save, read
from acceptance.v623.live import physical


def scientific_step(brief, replies, *, behavior):
    name, args = scripted.peer_step(brief,replies,behavior)
    if name == "gx_open_module":
        args["module_version"] = "3"
    if name == "gx_decide_revision" and brief["scope"] == "run" and brief.get("comparisons"):
        decision = args["decision"]
        ops = {op["op"] for op in decision["ops"]}
        cited = set(decision.get("evidence_refs",[]))
        metrics = []
        for row in brief.get("observations",[])+brief.get("module_reports",[]):
            if row.get("artifact_ref") in cited or row.get("report_ref") in cited:
                metrics.extend(m["metric_ref"] for m in row.get("verified_metrics",[]) if m.get("metric_ref"))
        if metrics and ("stop_branch" in ops or "add_node" in ops or "close_run" in ops):
            disposition = "CHANGE" if "stop_branch" in ops else "STOP" if "close_run" in ops else "MAINTAIN"
            decision.update(comparison_id=brief["comparisons"][0]["comparison_id"],disposition=disposition,
                            metric_refs=sorted(set(metrics)),affected_scope="The conditional archived coordinate comparison.")
            if disposition == "STOP":
                decision["stop_reason"] = "INSUFFICIENT_DATA" if behavior=="narrow" else "SCIENTIFIC_COMPLETION"
    return name,args


class FixtureOwner:
    """A local, charge-free deployment owning both the quote and the final SSE receipt."""
    contract_id = "gx.loopback.responses/1"
    deployment_id = "gx-official-host-local-responses"
    source = "SCRIPTED_LOOPBACK"

    def __init__(self,slot,ready,wire,*,step=None):
        from acceptance.v623.live.registration import P
        self.path = slot/"raw/scripted-provider"
        self.path.mkdir(exist_ok=True)
        self.wire = partial(offline.action,wire,slot/"raw/wire.json",assignment_complete=ready,
                            step=step or partial(scientific_step,behavior=P["offline_rehearsal_choices"][slot.name]),write_lock=Lock())

    def quote(self,request_id,body):
        sha = hashlib.sha256(body).hexdigest()
        return physical.Quote(request_id,sha,35,self.contract_id,request_id)

    def send(self,request_id,body,quote):
        prior = self.lookup(request_id)
        if prior:
            return prior
        obj,details = self.wire(body=json.loads(body),ident=request_id)
        completion = {"type":"response.completed","response":{"id":request_id,"model":json.loads(body)["model"],"usage":{
                      "input_tokens":30,"input_tokens_details":{"cached_tokens":0},"output_tokens":5,"total_tokens":35}}}
        events = [{"type":"response.created","response":{"id":request_id}},{"type":"response.output_item.done","item":obj},completion]
        reply = ("".join("data: "+json.dumps(e)+"\n\n" for e in events)).encode()
        save(self.path/(request_id+".json"),{"quote_id":quote.quote_id,"sse":reply.decode()})
        return physical.MeteredReply(request_id,quote.quote_id,reply)

    def lookup(self,request_id):
        path = self.path/(request_id+".json")
        if not path.exists():
            return None
        r = read(path)
        return physical.MeteredReply(request_id,r["quote_id"],r["sse"].encode())


def binding(slot,headers,body):
    metadata = json.loads(headers.get("x-codex-turn-metadata","{}"))
    client = body.get("client_metadata") or {}
    thread = headers.get("thread-id") or metadata.get("thread_id") or client.get("thread_id")
    turn = metadata.get("turn_id") or client.get("turn_id")
    brief,_ = offline.delivered(body)
    with closing(rehearse.db_open(slot)) as db:
        row = db.execute("""SELECT r.thread_id,r.role,r.model,r.effort,r.lease_id,d.scope FROM p0_roots r
                            JOIN v6_decision_tasks d ON d.run_id=r.run_id AND d.task_id=r.task_id
                            WHERE r.thread_id=? AND r.run_id=?""",(thread,brief["run_id"])).fetchone()
    if not row or row["scope"]!=brief["scope"] or row["model"]!=body["model"] or row["effort"]!=(body.get("reasoning") or {}).get("effort"):
        raise ValueError("Native request cannot be bound to the registered GX root, model and scope")
    return {**dict(row),"turn_id":turn,"run_id":brief["run_id"]}


@contextmanager
def gateway(slot,transport):
    journal = slot/"raw/physical-http.jsonl"
    lock = Lock()
    token = secrets.token_urlsafe(32)
    errors = []
    def log(obj):
        with lock, journal.open("a") as out:
            out.write(json.dumps(obj)+"\n"); out.flush(); os.fsync(out.fileno())

    class Peer(BaseHTTPRequestHandler):
        def log_message(self,*args):
            pass

        def do_POST(self):
            if self.path!="/v1/responses" or not hmac.compare_digest(self.headers.get("Authorization",""),"Bearer "+token):
                self.send_error(403)
                return
            ident = uuid4().hex
            try:
                body = self.rfile.read(int(self.headers["Content-Length"]))
                native = binding(slot,self.headers,json.loads(body))
                # Identical follow-up retransmissions from this native turn regain their original paid operation.
                sha = hashlib.sha256(body).hexdigest()
                ident = hashlib.sha256(json.dumps([slot.name,native["thread_id"],native["turn_id"],sha]).encode()).hexdigest()
                route = native["scope"]
                log({"request_id":ident,"at":datetime.now(timezone.utc).isoformat(),"phase":"RECEIVED",
                     "payload_sha256":sha,**native})
                reply = transport.request(slot=slot.name,scope=route,request_id=ident,body=body)
                upstream = physical.terminal_metadata(reply)
                if upstream["model"]!=native["model"]:
                    raise ValueError("The actual deployed response was produced by a different or unknown model")
                if upstream["status"]!="completed":
                    raise ValueError("The upstream response was not completed; preserve its bill and pause")
            except Exception as exc:
                detail = str(exc)[:200] if transport.owner.source=="SCRIPTED_LOOPBACK" else "Inspect the deployed metering owner"
                cap_hit = isinstance(exc,physical.FundingBlocked) and "cannot admit this request under its bound" in str(exc)
                errors.append({"request_id":ident,"reason":type(exc).__name__,"detail":detail,
                               "reason_code":"BUDGET_STOP" if cap_hit else "HELD_OR_REFUSED"})
                log({"phase":"HELD_OR_REFUSED",**errors[-1]})
                self.send_error(409,"Request not available; consult the original enrollment")
                return
            self.send_response(200)
            self.send_header("Content-Type","text/event-stream")
            self.send_header("Content-Length",str(len(reply)))
            self.end_headers()
            try:
                self.wfile.write(reply)
            except (BrokenPipeError,ConnectionResetError):
                log({"request_id":ident,"phase":"CLIENT_DISCONNECTED_AFTER_PROVIDER_RECEIPT"})
                return
            log({"request_id":ident,"phase":"DELIVERED","wire_sha256":hashlib.sha256(reply).hexdigest()})

    # The resolved provider URL is pinned in the original GX Run and its native sessions.
    # Reopening the same slot must recover that listener before any additional model work.
    binding_file = slot/"route.json"
    port = read(binding_file)["port"] if binding_file.exists() else 0
    try:
        listener = ThreadingHTTPServer(("127.0.0.1",port),Peer)
    except OSError as exc:
        raise physical.FundingBlocked("The previously enrolled local Responses port is busy") from exc
    with listener as server:
        if not binding_file.exists():
            save(binding_file,{"bind":"127.0.0.1","port":server.server_port})
        thread = Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            yield server,token,errors
        finally:
            server.shutdown();thread.join()
