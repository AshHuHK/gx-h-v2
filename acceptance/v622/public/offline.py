"""Official local Codex host with a metered, loopback-only scripted Responses transport."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Lock

from acceptance.v62c import materials, pilot_peer, rehearse
from acceptance.v622.public import funding, scripted


def delivered(body):
    for item in reversed(body["input"]):
        if item.get("role") in ("user", "developer"):
            for message in rehearse.responses.message_texts(item):
                if rehearse.MARKER in message:
                    raw = message.split(rehearse.MARKER, 1)[1]
                    packet,end = json.JSONDecoder().raw_decode(raw)
                    return packet, rehearse.MARKER + raw[:end]
    raise ValueError("The official request omitted the GX decision brief")


def scope_of(body):
    return delivered(body)[0]["scope"]


def final_message(ident, scope, reason, text):
    return ({"type":"message","id":"msg-"+ident,"role":"assistant",
             "content":[{"type":"output_text","text":text}]}, {"scope":scope,"tool":reason})


def action(wire, path, body, ident, assignment_complete, *, step, write_lock):
    brief, text = delivered(body)
    scope = brief["scope"]
    if scope == "run" and scripted.reference_branch(brief) and not pilot_peer.ref_for(brief, "structure"):
        if not assignment_complete.wait(20):
            raise RuntimeError("The assigned material was not published at the registered cutoff")
        return final_message(ident, scope, "operator_cut", "I will check the next brief for the assigned specimen.")
    try:
        replies = rehearse.historical_calls(body["input"], rehearse.responses.output_json)
    except ValueError as exc:
        if (brief.get("module") and not pilot_peer.ref_for(brief, "structure") and
                "SCOPE_DENIED: this root no longer owns the current task attempt" in str(exc)):
            return final_message(ident, scope, "superseded_lease", "A new task has received this assignment.")
        raise
    name, args = step(brief, replies)
    if scope == "run" and name == "gx_read_artifact":
        args.pop("operation_key", None)
    specs, tools = rehearse.wire_catalog(body)
    ns, = [namespace for namespace, tool in rehearse.declared_calls(specs) if tool == name]
    call_id = "call-" + ident
    record = {"call_id":call_id, "utc":datetime.now(timezone.utc).isoformat(), "brief":brief,
                 "brief_text":text, "tools":sorted(tools), "tool":name, "args":args, "namespace":ns,
                 "model":body["model"], "effort":(body.get("reasoning") or {}).get("effort"),
                 "tool_results":[{"tool":n,"ref":ref,
                                  "value_sha256":materials.digest(json.dumps(data, sort_keys=True).encode())}
                                 for (n,ref), data in replies.items()]}
    # Keep the last complete native trace across concurrent roles and an interrupted serializer.
    with write_lock:
        wire.append(record)
        tmp = path.with_suffix(".pending")
        materials.save(tmp,wire)
        tmp.replace(path)
    item = {"call_id":call_id,"type":"function_call","name":name,"arguments":json.dumps(args)}
    if ns:
        item["namespace"] = ns
    return item, {"scope":scope,"tool":name}


@contextmanager
def session(slot, binary, behavior, assignment_complete, ledger):
    slot, binary = Path(slot).resolve(), Path(binary).resolve()
    if behavior not in scripted.BEHAVIORS:
        raise ValueError(f"Unknown scripted behavior {behavior!r}")
    raw = slot / "raw"
    raw.mkdir(exist_ok=True)
    trace = raw / "wire.json"
    wire = json.loads(trace.read_bytes()) if trace.exists() else []
    write_lock = Lock()
    invoke = lambda body, ident: action(wire, trace, body, ident, assignment_complete,
                                        step=partial(scripted.peer_step, behavior=behavior),write_lock=write_lock)
    with funding.offline_responses(raw/"requests.jsonl", ledger, slot.name, rehearse.responses,
                                   invoke, scope_of=scope_of) as server:
        home = slot / "codex"
        home.mkdir(exist_ok=True)
        if (home / "auth.json").exists():
            raise ValueError("The scripted home unexpectedly contains live credentials")
        # Rebuild the local endpoint URL while retaining the real archived sessions and host state.
        with TemporaryDirectory(dir=slot) as scratch:
            env = rehearse.fixture_env(Path(scratch), server, str(binary))
            config = (Path(env["CODEX_HOME"]) / "config.toml").read_text()
        model, effort = rehearse.P["model"], rehearse.P["model_effort"]
        template = rehearse.archive.read(rehearse.ROOT/"vendor/codex/codex-rs/models-manager/models.json")["models"][0]
        if effort not in {row["effort"] for row in template["supported_reasoning_levels"]}:
            raise ValueError("Pinned host catalog does not advertise the registered effort")
        catalog = home/"scripted-catalog.json"
        materials.save(catalog, {"models":[{**template,"slug":model,"display_name":model,
                                            "tool_mode":"direct","use_responses_lite":False}]})
        (home/"config.toml").write_text("model_catalog_json = "+json.dumps(str(catalog))+"\n"+config)
        profile = {"default":{"provider":"gx_loopback","model":model,"effort":effort,"multi_agent":False}}
        profile_path=slot/"profile.json"
        if profile_path.exists() and json.loads(profile_path.read_bytes()) != profile:
            raise ValueError("The slot profile differs from the registered loopback model and effort")
        if not profile_path.exists():
            materials.save(profile_path, profile)
        env.update(HOME=str(slot), CODEX_HOME=str(home), GX_P0_PROFILE=str(profile_path),
                   PATH=str(Path(sys.executable).parent)+os.pathsep+env["PATH"])
        yield env, wire, server
