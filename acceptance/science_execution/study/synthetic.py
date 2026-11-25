"""Transparent scripted Lead. It can see only the delivered protocol, materials and tool replies."""
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock

from acceptance.v622.public import funding, offline
from acceptance.v622.public.enrollment import save
from acceptance.v623.study.host import Replies
from acceptance.v62c import pilot_peer, rehearse
from . import protocol


def step(brief, replies):
    card_ref = pilot_peer.ref_for(brief, "workflow_spec")
    if not brief["branches"]:
        if ("gx_read_artifact", card_ref) not in replies:
            return "gx_read_artifact", {"ref":card_ref}
        card = replies["gx_read_artifact", card_ref]
        branch = {"op":"open_branch", "branch":"local:segment", "hypothesis":"A median may hide a pair-specific excursion.",
                  "alternative":"The median represents the enrolled pair set in this window.",
                  "method_family":"Mapped interval omission", "discriminating_observation":"Pair changes versus change of medians.",
                  "budget_limits":{"job_submissions":2,"cpu_seconds":180}, "stop_condition":"Stop after the table answers the local question."}
        params = {"mask":"common_backbone", "endpoint":"rmsd_A", "interval":"21-40"}
        name, args = pilot_peer.decision([branch], "檢查選定區段的中位數是否遮蔽單對變化。", [card_ref])
        args["decision"]["comparison_plan"] = {
            "branch":branch["branch"], "material_role":"structure",
            "method":{"capability":protocol.METHOD,"parameters":params,"output_name":"segment.json",
                      "metric_id":"median_delta_rmsd_A","unit":"Å"},
            "prediction":{"blind_id":card["blind_id"], "expected":"中位數可能掩蓋個別鏈對的變化。",
                          "on_expected":"列出超出容許量的鏈對。","on_alternative":"維持局部的代表值並報告適用域。"}}
        return name, args
    structure, mapping = (pilot_peer.ref_for(brief, role) for role in ("structure","mapping"))
    if not (structure and mapping):
        return None
    if not brief["nodes"]:
        params = brief["comparisons"][0]["method"]["parameters"]
        op = {"op":"add_node","node":"local:primary","branch":brief["branches"][0]["branch_id"],
              "capability":protocol.METHOD,"inputs":[structure,mapping],"parameters":params,"depends_on":[],
              "question":"Compare the change in each matched pair with the change of the two medians in this interval."}
        return pilot_peer.decision([op],"Measure the registered primary comparison.",[structure,mapping], "job_settled",op["node"])
    node = brief["nodes"][0]
    if not node.get("result_ref"):
        return pilot_peer.decision([], "Wait for the admitted measurement.", (), "job_settled", node["node_id"])
    output_ref = next((o["artifact_ref"] for o in node.get("outputs", ()) if o["output_name"]=="segment.json"), node["result_ref"])
    if ("gx_read_artifact", output_ref) not in replies:
        return "gx_read_artifact", {"ref":output_ref}
    table = replies["gx_read_artifact",output_ref]
    action = {"CONCEALED":"Name the affected pairs because the median conceals an excursion.",
              "REPRESENTATIVE_WITHIN_LIMIT":"Use a bounded representative summary for this interval.",
              "MEDIAN_ALSO_OVER_LIMIT":"Report the median excursion and the individual pairs; a stable representative is unsupported.",
              "BORDERLINE":"Report this interval as borderline and preserve the per-pair values.",
              "UNDETERMINED":"Report insufficient eligible positions; leave the comparison unresolved."}[table["interpretation"]]
    if ("gx_stage_claim",None) not in replies:
        return "gx_stage_claim", {"statement":action,"level":"open","scope":"Only this mapped interval, mask, endpoint and chain set.",
            "criteria":"Compare unrounded pair and median changes against the registered reporting limit.",
            "limitations":"Shared chains are not independent observations. Other segments are outside this comparison.",
            "rationale":"The complete table contains the pair and median before and after omission.","evidence_refs":[output_ref]}
    claim = replies["gx_stage_claim",None]["claim_ref"]
    op = {"op":"close_run","claim_refs":[claim],"reason":action,"unresolved":["Other intervals and populations remain unmeasured."]}
    name, args = pilot_peer.decision([op],action,[output_ref,claim])
    args["decision"]["metric_refs"] = [m["metric_ref"] for o in brief["observations"] if o["artifact_ref"]==output_ref
                                       for m in o.get("verified_metrics", ()) if "_delta_" in m["metric_id"]]
    return name, args


@contextmanager
def session(slot, binary, ledger, opened):
    raw = slot/"raw"
    trace = raw/"wire.json"
    wire = json.loads(trace.read_bytes()) if trace.exists() else []
    lock = Lock()

    def action(body, ident):
        brief, text = offline.delivered(body)
        call = step(brief, Replies(body))
        if call is None:
            if not opened.wait(35):
                raise RuntimeError("The operator did not open the registered material")
            return offline.final_message(ident,brief["scope"],"operator_cut","Await the next material brief.")
        name, args = call
        specs, _ = rehearse.wire_catalog(body)
        namespace, = [n for n,t in rehearse.declared_calls(specs) if t == name]
        item = {"type":"function_call","name":name,"call_id":"call-"+ident,"arguments":json.dumps(args)}
        if namespace:
            item["namespace"] = namespace
        with lock:
            wire.append({"call_id":item["call_id"],"brief":brief,"brief_text":text,"tool":name,"args":args})
            save(trace,wire)
        return item, {"scope":brief["scope"],"tool":name}

    with funding.offline_responses(raw/"requests.jsonl", ledger, slot.name, rehearse.responses, action,
                                   scope_of=offline.scope_of) as server:
        with TemporaryDirectory(dir=slot) as temporary:
            env = rehearse.fixture_env(Path(temporary),server,str(binary))
            config = (Path(env["CODEX_HOME"])/"config.toml").read_text()
        home = slot/"codex"
        home.mkdir(exist_ok=True)
        if (home/"auth.json").exists():
            raise ValueError("Synthetic operation requires a credential-free home")
        model, effort = rehearse.P["model"], rehearse.P["model_effort"]
        catalog = rehearse.archive.read(rehearse.ROOT/"vendor/codex/codex-rs/models-manager/models.json")["models"][0]
        cat = home/"scripted-catalog.json"
        save(cat, {"models":[{**catalog,"slug":model,"display_name":model,"tool_mode":"direct","use_responses_lite":False}]})
        (home/"config.toml").write_text("model_catalog_json = "+json.dumps(str(cat))+"\n"+config)
        profile = slot/"profile.json"
        save(profile, {"default":{"provider":"gx_loopback","model":model,"effort":effort,"multi_agent":False}})
        env.update(HOME=str(slot),CODEX_HOME=str(home),GX_P0_PROFILE=str(profile),
                   PATH=str(Path(sys.executable).parent)+os.pathsep+env["PATH"])
        yield env
