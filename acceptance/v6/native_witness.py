"""Read provider-owned Codex rollout frames up to the one accepted GX call."""
from pathlib import Path

from verify import digest, need, parse, sha, stable


def texts(value):
    if isinstance(value, list):
        for item in value:
            yield from texts(item)
    elif isinstance(value, dict):
        if value.get("type") in {"input_text", "inputText", "text", "output_text"}:
            yield value["text"]


def documents(value):
    """Expand only complete structured values from model-visible messages/tool results."""
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from documents(item)
    elif isinstance(value, list):
        for item in value:
            yield from documents(item)
    elif isinstance(value, str):
        for off in (0, value.find("\n") + 1):
            text = value[off:].strip()
            if text.startswith(("{", "[")):
                try:
                    obj = parse(text)
                except ValueError:
                    continue
                yield from documents(obj)
                break


def rollout(path, decision, source):
    tid, turn, call = (decision[k] for k in ("thread_id", "turn_id", "call_id"))
    root, = [r for r in source["p0_roots"] if r["thread_id"] == tid]
    need(root["task_id"] == decision["task_id"] and root["lease_id"] == decision["lease_id"],
         "native Root and durable Decision belong to different attempts")
    session, context, active = None, None, None
    user, reads, calls, arguments = [], {}, {}, None
    selected, products, nested = {}, {}, {}
    active_exec = None
    delivered = {r["text_sha256"] for r in source["p0_root_deliveries"]
                 if (r["thread_id"], r["turn_id"], r["state"]) == (tid, turn, "acknowledged")}
    with Path(path).open("rb") as stream:
        for number, raw in enumerate(stream, 1):
            event = parse(raw)
            typ, obj = event["type"], event["payload"]
            pt = obj.get("type")
            if typ == "session_meta":
                need(session is None and obj["id"] == tid, "wrong official session")
                session = {k: obj.get(k) for k in ("id", "cli_version", "model_provider", "runtime_workspace_roots")}
            if typ == "event_msg" and pt == "task_started":
                active = obj["turn_id"]
            if typ == "turn_context":
                active = obj["turn_id"]
                if active == turn:
                    context = {k: obj.get(k) for k in ("turn_id", "model", "effort", "cwd",
                               "sandbox_policy", "permission_profile", "approval_policy")}
            if active != turn:
                continue
            if (typ == "event_msg" and pt == "item_completed" and obj.get("turn_id") == turn
                    and obj.get("thread_id") == tid and active_exec):
                item = obj.get("item", {})
                if item.get("type") == "DynamicToolCall" and item.get("success") is True:
                    nested[active_exec].append(item)
            if typ != "response_item":
                continue
            if pt == "custom_tool_call" and obj.get("name") == "exec":
                active_exec = obj["call_id"]
                nested[active_exec] = []
            if pt == "custom_tool_call_output" and obj["call_id"] == active_exec:
                # A nested action's GX receipt is observable even when the script discards its
                # output. Only credit material actually forwarded in the outer model response.
                forwarded = list(documents(obj["output"]))
                for item in nested[active_exec]:
                    internal = list(documents(item["content_items"]))
                    if item["tool"] in {"gx_stage_claim", "gx_reevaluate_geometry"}:
                        field = "claim_ref" if item["tool"] == "gx_stage_claim" else "evaluation_ref"
                        for body in internal:
                            if isinstance(body,dict) and field in body and body in forwarded:
                                products[body[field]] = {"frame":obj,"native_item":item,
                                    "call_id":item["id"],"line":number,"field":field}
                    if item["tool"] == "gx_read_artifact":
                        ref = item["arguments"].get("ref")
                        # Preserve the intersection of the authenticated read result and what
                        # the script actually forwarded; never expose a discarded tool result.
                        reads.setdefault(ref,[]).extend(x for x in internal if isinstance(x,(dict,list)) and x in forwarded)
                active_exec = None
            if pt == "message" and obj.get("role") == "user":
                user.extend(texts(obj.get("content")))
            if pt == "function_call":
                args = parse(obj["arguments"])
                name = obj["name"].split(".")[-1]
                calls[obj["call_id"]] = (name, args)
                if obj["call_id"] == call:
                    need(name in ("gx_decide_revision", "gx__gx_decide_revision"), "native tool is not decide_revision")
                    arguments = args
                    selected = obj
                    call_line = number
                    break
            if pt == "function_call_output" and obj["call_id"] in calls:
                name, args = calls[obj["call_id"]]
                if name.endswith(("gx_stage_claim", "gx_reevaluate_geometry")):
                    field = "claim_ref" if name.endswith("gx_stage_claim") else "evaluation_ref"
                    for value in documents(obj["output"]):
                        if isinstance(value,dict) and isinstance(value.get(field),str):
                            products[value[field]] = {"frame":obj,"call_id":obj["call_id"],"line":number,"field":field}
                if name.endswith("gx_read_artifact"):
                    ref = args.get("ref")
                    if ref:
                        reads.setdefault(ref, []).append(obj["output"])
    need(session and context and arguments is not None, "missing official session, turn or call")
    need(root["brief"] in user and sha(root["brief"].encode()) in delivered,
         "root brief not confirmed by rollout and acknowledged delivery")
    native_decision = arguments["decision"]
    if isinstance(native_decision, str):
        native_decision = parse(native_decision)
    recorded = parse(decision["request_json"])
    need(recorded["decision"] == native_decision and digest(native_decision) == recorded["draft_sha256"],
         "adopted payload differs from native tool arguments")
    callback, = [c for c in source["p0_root_calls"] if (c["thread_id"],c["call_id"]) == (tid,call)]
    need(callback["turn_id"] == turn and callback["request_sha"] == digest(["gx_decide_revision", arguments]),
         "GX callback digest or turn mismatch")
    return {"session":session, "context":context, "brief":root["brief"], "reads":reads, "call":selected, "call_line":call_line, "products":products,
            "root":root, "intent":native_decision, "callback":callback}


def seen(native, evidence_ref, value):
    """Match an actual value under its cited source; occurrences in model prose do not qualify."""
    for raw in native["reads"].get(evidence_ref, []):
        if any(stable(obj) == stable(value) for obj in documents(raw)):
            return True
    for obj in documents(native["brief"]):
        if not isinstance(obj, dict):
            continue
        source = next((obj.get(k) for k in ("artifact_ref", "evidence_ref", "result_ref", "id")
                       if obj.get(k) == evidence_ref), None)
        part = obj if source else obj.get(evidence_ref)
        if part is not None and any(stable(v) == stable(value) for v in documents(part)):
            return True
    return False
