"""A separated official Codex session using the local, funded study surrogate."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Lock
from acceptance.v622.public import offline, funding
from acceptance.v622.public.enrollment import save
from acceptance.v62c import rehearse
from . import scripted_lead


class Replies(dict):
    def __init__(self, body):
        super().__init__(rehearse.historical_calls(body["input"],rehearse.responses.output_json))
        self.pages={}
        calls={c["call_id"]:c for c in body["input"] if c.get("type")=="function_call"}
        for r in body["input"]:
            call=calls.get(r.get("call_id"))
            if call and r.get("type")=="function_call_output" and call["name"]=="gx_read_artifact":
                a=json.loads(call["arguments"])
                if "endpoint_offset" in a:
                    self.pages[call["name"],a["ref"],a["endpoint_offset"]]=rehearse.responses.output_json(r["output"])


@contextmanager
def scripted(slot,binary,ledger,opened):
    raw=slot/"raw"
    raw.mkdir(exist_ok=True)
    wire_file=raw/"wire.json"
    wire=json.loads(wire_file.read_bytes()) if wire_file.exists() else []
    lock=Lock()
    def action(body,ident):
        brief,text=offline.delivered(body)
        replies=Replies(body)
        next_call=scripted_lead.step(brief,replies)
        if next_call is None:
            count=sum(m["role"]=="structure" for m in brief["materials"])
            if count>=len(opened) or not opened[count].wait(25):
                raise RuntimeError("No operator material after the registered decision cut")
            return offline.final_message(ident,brief["scope"],"operator_cut","Waiting for the assigned deposition in my next brief.")
        tool,args=next_call
        specs,offered=rehearse.wire_catalog(body)
        namespace,=[n for n,t in rehearse.declared_calls(specs) if t==tool]
        cid="call-"+ident
        call={"call_id":cid,"type":"function_call","name":tool,"arguments":json.dumps(args)}
        if namespace:
            call["namespace"]=namespace
        record={"call_id":cid,"utc":datetime.now(timezone.utc).isoformat(),"brief":brief,"brief_text":text,
                "tools":sorted(offered),"tool":tool,"args":args,"namespace":namespace,"model":body["model"],
                "effort":body.get("reasoning",{}).get("effort"),"scope":brief["scope"]}
        with lock:
            wire.append(record)
            save(wire_file,wire)
        return call,{"scope":brief["scope"],"tool":tool}
    with funding.offline_responses(raw/"requests.jsonl",ledger,slot.name,rehearse.responses,action,
                                   scope_of=offline.scope_of) as server:
        with TemporaryDirectory(dir=slot) as temporary:
            env=rehearse.fixture_env(Path(temporary),server,str(binary))
            config=(Path(env["CODEX_HOME"])/"config.toml").read_text()
        home=slot/"codex"
        home.mkdir(exist_ok=True)
        if (home/"auth.json").exists():
            raise ValueError("Study surrogate requires a fresh credential-free Codex home")
        model,effort=rehearse.P["model"],rehearse.P["model_effort"]
        first=rehearse.archive.read(rehearse.ROOT/"vendor/codex/codex-rs/models-manager/models.json")["models"][0]
        if effort not in {i["effort"] for i in first["supported_reasoning_levels"]}:
            raise ValueError("Frozen local Codex model lacks its registered effort")
        catalog=home/"scripted-catalog.json"
        save(catalog,{"models":[{**first,"slug":model,"display_name":model,"tool_mode":"direct","use_responses_lite":False}]})
        (home/"config.toml").write_text("model_catalog_json = "+json.dumps(str(catalog))+"\n"+config)
        profile=slot/"profile.json"
        save(profile,{"default":{"provider":"gx_loopback","model":model,"effort":effort,"multi_agent":False}})
        env.update(HOME=str(slot),CODEX_HOME=str(home),GX_P0_PROFILE=str(profile),PATH=str(Path(sys.executable).parent)+os.pathsep+env["PATH"])
        yield env,wire,server
