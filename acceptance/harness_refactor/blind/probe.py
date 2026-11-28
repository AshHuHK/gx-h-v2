"""Check account, source freeze, rendering parity and stock thread/resume without starting a turn."""
import argparse
import json
from pathlib import Path

from common import BINARY, EFFORT, HERE, MODEL, environment, oauth_host, read, sources
from native import settings
from gx_core.p0 import profiles
from gx_core.hostobs.app_server_client import AppServerClient

def probe(folder):
    folder.mkdir(mode=0o700,parents=True,exist_ok=True)
    env=environment(folder)
    preflight=oauth_host().preflight(BINARY,MODEL,EFFORT)
    if preflight.get("status")!="READY":raise RuntimeError(preflight.get("reason_codes"))
    result={"preflight":{k:preflight[k] for k in ("status","auth_type","subscription_usage_state")},"turns_started":0,
            "files_match":sources()==read(HERE/"preregistration.json")["inputs"]}
    cwd=folder/"empty";cwd.mkdir(exist_ok=True)
    argv=profiles.app_server_argv(test_mode=False,provider="openai");argv[0]=str(BINARY)
    with AppServerClient(argv,cwd=cwd,env=env) as cl:
        cl.request("initialize",{"clientInfo":{"name":"archive_prep","version":"1"},"capabilities":{"experimentalApi":True}})
        cl.notify("initialized",{})
        cfg=cl.request("config/read",{"cwd":str(cwd),"includeLayers":False})["config"]
        par=settings(cwd,cfg,False);response=cl.request("thread/start",par)
        assert response["model"]==MODEL and response["modelProvider"]=="openai"
        assert response["sandbox"]["type"]=="readOnly" and not response["sandbox"].get("networkAccess")
        result.update(stock_thread_accepts_readonly_study=True,stock_multi_agent_requested=par["config"]["features.multi_agent_v2.enabled"],
                      stock_effective_model=response["model"],stock_approval=response["approvalPolicy"])
    return result

if __name__=="__main__":
    p=argparse.ArgumentParser(description=__doc__);p.add_argument("private", type=Path)
    print(json.dumps(probe(p.parse_args().private.resolve()),indent=2))
