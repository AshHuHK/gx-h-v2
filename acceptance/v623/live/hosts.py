"""Verified App Server identity and native per-role session witnesses for the public pilot."""
from contextlib import closing, contextmanager
from functools import partial
import hashlib
import json
import os
import socket
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Lock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path[:0] = [str(ROOT),str(ROOT/"gx-h")]
from gx_core.hostobs.app_server_client import AppServerClient
from gx_core.p0 import profiles
from acceptance.v62c import materials, rehearse
from acceptance.v622.public import offline, funding, scripted
from acceptance.v622.public.enrollment import read, digest, save


def _identity(binary):
    """Only initialize stdio in an empty temporary home and disposable network namespace."""
    env = {k:v for k,v in os.environ.items() if k.lower() in
           {"path","lang","lc_all","all_proxy","http_proxy","https_proxy","no_proxy"}}
    with TemporaryDirectory() as tmp:
        env.update(HOME=tmp,CODEX_HOME=tmp)
        argv = [str(binary),"--enable","respect_system_proxy","--disable","system_proxy_fallback",
                "-c","analytics.enabled=false","app-server","--listen","stdio://"]
        with AppServerClient(argv,cwd=tmp,env=env,default_timeout=10) as client:
            reply = client.request("initialize",{"clientInfo":{"name":"gx_pilot_inspect","version":"1"},
                                                  "capabilities":{"experimentalApi":True}})
            client.notify("initialized",{})
        version = subprocess.check_output([str(binary),"--version"],cwd=tmp,env=env,text=True,timeout=10).strip()
    agent = reply["userAgent"].split(" (")[0]
    if agent.rsplit("/",1)[-1]!=profiles.CODEX_VERSION or version.split()[-1]!=profiles.CODEX_VERSION:
        raise ValueError("The installed official host version differs from the release")
    return {"rpc_methods":["initialize","initialized"],"user_agent":agent,"cli_version":version,
            "network_interfaces":sorted(name for _,name in socket.if_nameindex()),
            "ipv4_routes":Path("/proc/net/route").read_text().splitlines()[1:],
            "ipv6_routes":sorted({r.split()[-1] for r in Path("/proc/net/ipv6_route").read_text().splitlines()}),
            "home":"temporary empty profile","thread_starts":0,"turn_starts":0,"paid_requests":0}


def inspect_binary(binary):
    binary = Path(binary).resolve(strict=True)
    sha = digest(binary)
    if sha!=read(ROOT/"release-assets/lock.json")["linux_x64"]["binary_sha256"]:
        raise ValueError("App Server binary SHA differs from the pinned official release")
    # Starting the real binary with a read-only RPC is safe even on deployments with configured credentials.
    proc = subprocess.run(["unshare","--user","--map-root-user","--net",sys.executable,"-B",str(HERE/"hosts.py"),
                           "_identity",str(binary)],capture_output=True,text=True,timeout=20)
    if proc.returncode:
        raise RuntimeError("Isolated official App Server initialization failed")
    proof = json.loads(proc.stdout)
    if proof["ipv4_routes"] or set(proof["ipv6_routes"])-{"lo"}:
        raise ValueError("Host inspection must have no external network route")
    return {**proof,"binary":str(binary),"binary_sha256":sha,"source":"released linux-x86_64 Codex App Server",
            "inference":"NOT_RUN","provider_identity":"UNOBSERVED_WITHOUT_A_PROVIDER_RESPONSE"}


def role_witness(slot,rid,*,strict=True):
    """Verify each official native root's effective identity against its own session and delivered turn."""
    native = {}
    for file in sorted((slot/"codex/sessions").rglob("*.jsonl")):
        entry, turns = None, {}
        with file.open(encoding="utf8") as stream:
            for line in stream:
                row = json.loads(line)
                p = row.get("payload") or {}
                if row["type"]=="session_meta":
                    entry = p
                if row["type"]=="turn_context" and p.get("turn_id"):
                    turns[p["turn_id"]] = {"model":p.get("model"),"effort":p.get("effort")}
        if entry:
            native[entry["id"]] = (entry.get("model_provider"), turns, digest(file))
    with closing(rehearse.db_open(slot)) as db:
        roots = db.execute("""SELECT root_key,role,thread_id,lease_id,provider,model,effort,effective_profile_json
                  FROM p0_roots WHERE run_id=? AND thread_id IS NOT NULL ORDER BY rowid""",(rid,)).fetchall()
        records = []
        for row in roots:
            thread = row["thread_id"]
            provider, turns, session_sha = native.get(thread,(None,{},None))
            effective = json.loads(row["effective_profile_json"] or "{}")
            verified=provider==row["provider"] and effective.get("provider")==provider and effective.get("model")==row["model"]
            if strict and not verified:
                raise ValueError("Original native rollout and GX role identity disagree")
            actual = []
            for t in db.execute("SELECT * FROM p0_root_turn_usage WHERE thread_id=? ORDER BY rowid",(thread,)):
                ctx = turns.get(t["turn_id"])
                matched=bool(ctx and ctx["model"]==row["model"] and ctx["effort"]==row["effort"])
                if strict and not matched:
                    raise ValueError("Missing original official model or effort for a delivered turn")
                actual.append({k:t[k] for k in ("turn_id","total_tokens","terminal_status")}
                              |(ctx or {})|{"configuration_matched":matched})
            records.append({k:row[k] for k in ("root_key","role","thread_id","lease_id","model","effort","provider")}
                           |{"turns":actual,"session_sha256":session_sha,"provider_matched":verified})
    return records


if __name__=="__main__" and len(sys.argv)==3 and sys.argv[1]=="_identity":
    print(json.dumps(_identity(Path(sys.argv[2]))))


@contextmanager
def session(slot,binary,reg,ready,owner=None,*,fixture_step=None):
    """Launch ordinary GX Roots on a private native home with one metered provider and no ambient login."""
    from acceptance.v623.live import physical
    from acceptance.v623.live.transport import FixtureOwner,gateway
    home = slot/"codex"
    home.mkdir(exist_ok=True)
    if (home/"auth.json").exists():
        raise ValueError("The batch host requires its own credential-free session store")
    wire_file = slot/"raw/wire.json"
    wire = read(wire_file) if wire_file.exists() else []
    fixture = reg["host"]=="offline"
    if fixture:
        owner = FixtureOwner(slot,ready,wire,step=fixture_step)
    elif owner is None or owner.source!="PRODUCTION" or owner.deployment_id!=reg["deployment_id"] or owner.contract_id!=reg["contract_id"]:
        raise ValueError("The approved provider deployment and bound contract are unavailable")
    meter = physical.FundedTransport(slot.parent/"funding.sqlite",series=reg["series"],slots=[i["slot"] for i in reg["slots"]],
                                     slot_cap=reg["slot_cap"],batch_cap=reg["batch_cap"],owner=owner,
                                     **({"authorization":reg["funding"]["authorization"]} if not fixture else {}))
    # A disconnected operator resumes the original physical requests before authorizing any more.
    from acceptance.v623.live.pilot import reconcile_meter
    reconcile_meter(meter)
    provider = reg["provider"]
    with gateway(slot,meter) as (server,token,errors):
        url = "http://127.0.0.1:"+str(server.server_port)
        catalog = home/"pilot-catalog.json"
        if fixture:
            template = read(ROOT/"vendor/codex/codex-rs/models-manager/models.json")["models"][0]
            if reg["effort"] not in {p["effort"] for p in template["supported_reasoning_levels"]}:
                raise ValueError("Unsupported reasoning effort for this fixture")
            save(catalog,{"models":[{**template,"slug":reg["model"],"display_name":reg["model"],
                                      "tool_mode":"direct","use_responses_lite":False}]})
        else:
            # The selected provider-owner, which also signs quotes and handles usage, supplies its real catalog.
            manifest = owner.model_catalog()
            save(catalog,manifest)
        (home/"config.toml").write_text(
            f'model_catalog_json = {json.dumps(str(catalog))}\nmodel_provider = "{provider}"\n'
            f'openai_base_url = "{url}/v1"\nchatgpt_base_url = "{url}"\ncheck_for_update_on_startup = false\n'
            f'[model_providers.{provider}]\nname = "GX enrolled Responses route"\nbase_url = "{url}/v1"\n'
            'env_key = "GX_PILOT_ROUTE"\nwire_api = "responses"\nrequires_openai_auth = false\n'
            'request_max_retries = 0\nstream_max_retries = 0\nsupports_websockets = false\n'
            '[features]\nrespect_system_proxy = true\nsystem_proxy_fallback = false\n[analytics]\nenabled = false\n')
        profile = slot/"profile.json"
        save(profile,{"default":{"provider":provider,"model":reg["model"],"effort":reg["effort"],"multi_agent":False}})
        proxy = profiles.clash_env(native_http=True)
        env = {k:v for k,v in proxy.items() if k.lower() in
               {"all_proxy","http_proxy","https_proxy","no_proxy","path","ld_library_path","lang","lc_all"}}
        from gx_core.resource_profile import child_resource_environment
        env.update(child_resource_environment())
        env.update(HOME=str(slot),CODEX_HOME=str(home),GX_PILOT_ROUTE=token,GX_P0_CODEX=str(binary),
                   GX_P0_PROFILE=str(profile),GX_P0_PROVIDER=provider,
                   PATH=str(Path(sys.executable).parent)+os.pathsep+env.get("PATH",""),
                   CODEX_APP_SERVER_CHATGPT_BASE_URL=url)
        if fixture:
            env.update(GX_P0_TEST="1",GX_P0_TEST_PROVIDER=provider)
        yield env,wire,meter,errors
