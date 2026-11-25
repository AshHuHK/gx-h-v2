"""Run one installed-owner transport control on the official production host path without an external network."""
import argparse
from contextlib import closing, ExitStack
import fcntl
import hashlib
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
from threading import Event, Thread
from unittest.mock import patch

from acceptance.v623.live import deployment, hosts, physical, registration
from acceptance.v622.public import batch
from acceptance.v62c import rehearse
from acceptance.v622.public.enrollment import save, read
from acceptance.v623.test_live_funding import Approval


def isolated_network():
    routes = Path("/proc/net/route").read_text().splitlines()[1:]
    v6 = {line.split()[-1] for line in Path("/proc/net/ipv6_route").read_text().splitlines()}
    if routes or v6 - {"lo"}:
        raise ValueError("This control runs only in a fresh network namespace without external routes")
    with closing(socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) as sock:
        req = struct.pack("16sh", b"lo", 1)
        fcntl.ioctl(sock, 0x8914, req)  # SIOCSIFFLAGS: enable local loopback in this fresh namespace.
    return {"interfaces": sorted(i for _, i in socket.if_nameindex()), "external_routes": 0}


class IsolatedProvider:
    """Installed solely inside the no-network control, with a single bounded local charge."""
    source = "PRODUCTION"   # Select the production connector code path in this isolated experiment.
    deployment_id = "gx-isolated-provider-control"
    contract_id = "gx-isolated-charge-control"

    def __init__(self, directory):
        self.directory = Path(directory)
        v6 = {line.split()[-1] for line in Path("/proc/net/ipv6_route").read_text().splitlines()}
        if Path("/proc/net/route").read_text().splitlines()[1:] or v6 - {"lo"}:
            raise ValueError("A mock priced owner requires the isolated namespace")

    def deployment_receipt(self):
        return read(self.directory/"test-proof.json")

    def model_catalog(self):
        template = read(deployment.ROOT/"vendor/codex/codex-rs/models-manager/models.json")["models"][0]
        return {"models":[{**template, "slug":registration.P["model"], "display_name":"meter engineering control",
                           "tool_mode":"direct", "use_responses_lite":False}]}

    def quote(self, ident, body):
        return physical.Quote(ident, hashlib.sha256(body).hexdigest(), 1_000_000, self.contract_id, ident)

    def send(self, ident, body, quote):
        model = json.loads(body)["model"]
        events = [{"type":"response.created","response":{"id":"provider-"+ident}},
                  {"type":"response.output_item.done","item":{"type":"message","role":"assistant","id":"m-"+ident,
                        "content":[{"type":"output_text","text":"Transport control complete."}]}},
                  {"type":"response.completed","response":{"id":"provider-"+ident,"model":model,"status":"completed",
                        "usage":{"input_tokens":6,"input_tokens_details":{"cached_tokens":1},"output_tokens":1,
                                 "output_tokens_details":{"reasoning_tokens":0},"total_tokens":7}}}]
        raw = "".join("data: "+json.dumps(event)+"\n\n" for event in events).encode()
        # Exclusive create detects a duplicate physical send even if it would have been hidden by the GX ledger.
        with (self.directory/(ident+".sse")).open("xb") as stream:
            stream.write(raw)
        return physical.MeteredReply(ident, quote.quote_id, raw)

    def lookup(self, ident):
        receipt = self.directory/(ident+".sse")
        return physical.MeteredReply(ident, ident, receipt.read_bytes()) if receipt.is_file() else None


def run(work):
    network = isolated_network()
    work.mkdir(mode=0o700, parents=True, exist_ok=False)
    save(work/"control-context.json", {"classification":"ISOLATED_PROVISIONED_OWNER_CONTROL",
         "scope":"Transport and accounting control with stub PI/provider. All bills and model outputs here are synthetic.",
         "network":network, "new_real_model_inference":0, "counts_toward_public_four":False})
    site = work / "isolated-site"
    site.mkdir()
    candidate = subprocess.check_output(["git","rev-parse","HEAD"], cwd=deployment.ROOT,text=True).strip()
    protocol = deployment._sha(Path(__file__))
    approved = Approval().document() | {"deployment_id":IsolatedProvider.deployment_id,"contract_id":IsolatedProvider.contract_id,
                                        "candidate":candidate,"protocol_sha256":protocol}
    save(site/(deployment.PUBLIC+".json"), approved)
    (site/"provider-contract").write_text("No-network test fixture. Covers all 7 synthetic tokens.")
    program = site/"owner.py"
    program.write_text("from acceptance.v623.installed_meter_control import IsolatedProvider\n"
                       "from pathlib import Path\ndef open_owner(*,proxies): return IsolatedProvider(Path(__file__).parent)\n")
    spec = dict(schema="gx.priced-responses/1", provider_origin="https://provider.example.org",
                deployment_id=IsolatedProvider.deployment_id,contract_id=IsolatedProvider.contract_id,account_ref="test-account",
                adapter_sha256=deployment._sha(program), contract_sha256=deployment._sha(site/"provider-contract"))
    save(site/"binding.json",spec)
    save(site/"test-proof.json",spec | {"verified":True,"all_billable_tokens_bounded":True,"free_lookup_by_original_request_id":True,
                                        "provider_evidence_ref":"ISOLATED_ENGINEERING_FIXTURE", "model":registration.P["model"],
                                        "efforts":["max"]})
    # Existing GX insists on local proxy listeners before opening its stdio host. Within this namespace the
    # listeners immediately drop connections; there are no outbound routes or real credentials.
    with ExitStack() as stack:
        for port in (7890,7891):
            sock = stack.enter_context(closing(socket.socket()))
            sock.bind(("127.0.0.1",port))
            sock.listen()
            def reject(server):
                while True:
                    try:
                        peer,_ = server.accept()
                        peer.close()
                    except OSError:
                        break
            Thread(target=reject,args=(sock,),daemon=True).start()
        stack.enter_context(patch.object(deployment,"METER_DIRECTORY",site))
        stack.enter_context(patch.object(deployment,"PI_DIRECTORY",site))
        stack.enter_context(patch.object(deployment,"_owned",side_effect=lambda path:path.parent==site))
        auth = deployment._approval(series=deployment.PUBLIC, candidate=candidate, protocol_sha256=protocol,
                                    model=registration.P["model"], effort="max")
        assert auth["status"]=="VERIFIED"
        owner = deployment.open_deployment()
        reg = {"host":"live", "series":deployment.PUBLIC,"model":registration.P["model"],"effort":"max",
               "provider":"gx_admitted_responses", "deployment_id":owner.deployment_id,"contract_id":owner.contract_id,
               "slots":registration.P["slots"],"slot_cap":1_000_000,"batch_cap":4_000_000,"funding":{"authorization":auth}}
        slot=work/"01"
        (slot/"raw").mkdir(parents=True)
        goal=registration.grant("single")
        for key in ("run_budget","workspace_budget"):
            goal[key]["api_calls"]=1
        save(slot/"goal.json",goal)
        with (work/"test-control.lock").open("w") as lock, hosts.session(slot,rehearse.BINARY,reg,Event(),owner) as (env,_,meter,errors):
            cli=batch.native_cli(slot,env,lock.fileno())
            rid=cli("start","--goal","Run the isolated native transport control.","--run-key","installed-control",
                    "--goal-config",slot/"goal.json")["run_id"]
            cli("resume","--run-id",rid,"--timeout",4)
            ledger=meter.report()
            settled=[r for r in ledger["requests"] if r["state"]=="SETTLED"]
            if len(settled)!=1 or len(list(site.glob("*.sse")))!=1:
                raise ValueError(f"Expected one local fixture charge; found {len(settled)}; diagnostic {errors}")
            row,=settled
            assert row["actual"]==7 and row["upstream"]["model"]==registration.P["model"]
            assert owner.lookup(row["request_id"]).wire == meter.reconcile(row["request_id"])
            delivered=[json.loads(x) for x in (slot/"raw/physical-http.jsonl").read_text().splitlines()]
            native=next(r for r in delivered if r["phase"]=="RECEIVED" and r["request_id"]==row["request_id"])
            assert any(r["phase"]=="DELIVERED" and r["request_id"]==row["request_id"] for r in delivered)
            assert native["run_id"]==rid and native["thread_id"] and native["turn_id"] and native["lease_id"]
            assert not (slot/"codex/auth.json").exists()
    proof={"status":"PASS","classification":"ISOLATED_PROVISIONED_OWNER_CONTROL","candidate":candidate,"network":network,
           "series_kind":"engineering control","official_binary_sha256":deployment._sha(rehearse.BINARY),
           "model":native["model"],"effort":native["effort"],"run_id":rid,"thread_id":native["thread_id"],
           "turn_id":native["turn_id"],"lease_id":native["lease_id"],"request_id":row["request_id"],
           "quote_id":row["quote_id"],"provider_receipt_id":row["provider_receipt_id"],"terminal":row["upstream"],
           "fixture_measured_tokens":7,"fixture_sends":1,"settlements":1,"paid_provider_requests":0,
           "actual_provider_tokens":None,"actual_provider_usd":None,"live_science_slots":0}
    save(work/"result.json",proof)
    print(json.dumps(proof,indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work",type=Path,required=True)
    parser.add_argument("--inside",action="store_true",help=argparse.SUPPRESS)
    a=parser.parse_args()
    if a.inside:
        run(a.work.resolve())
    else:
        raise SystemExit(subprocess.run(["unshare","--user","--map-root-user","--net",sys.executable,"-B","-m",
            "acceptance.v623.installed_meter_control","--inside","--work",str(a.work.resolve())]).returncode)
