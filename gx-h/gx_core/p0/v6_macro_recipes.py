"""Portable module commissions and named joins in a workspace research recipe."""
from graphlib import TopologicalSorter
import json
import re

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied
from . import v62_catalog


def _question(text, what):
    if not isinstance(text, str) or not text.strip():
        raise SchemaInvalid(f"Give the next Lead a reusable {what}")
    if re.search(r"\b(?:gxa|gxt|gxr|gxj|module|node)_[0-9a-z]{12,}\b", text):
        raise SchemaInvalid(f"Refer to materials by their slots in the reusable {what}")
    return text


def capture(conn, run_id, args, node_slots, external, produced, required):
    """Extract procedural inputs and pins from performed work. Never copy a source answer."""
    from . import capability_views, research_joins, v6_recipes
    macro = args["macro"]
    record = research_joins.lookup(conn, run_id, macro["join_key"], macro.get("join_revision"))
    if not record or not record["outcome_event"]:
        raise SchemaInvalid("Save the join after it has woken the Lead on its actual upstream outcomes")
    spec = record["spec"]
    if record.get("instance"):
        from .recipe_instances import Producers
        spec = Producers(conn,run_id,record).physical_spec(spec)
        if not spec:
            raise SchemaInvalid("Select a Join with actual producers when saving performed work")
    material = ArtifactService(conn)
    inputs = v62_catalog._inputs(conn, run_id)
    source_catalog = inputs.get("research_catalog", {})
    templates = {(d["module_id"], d["version"]):d for d in v62_catalog.module_choices(source_catalog)}
    requested = {m["module_instance_id"]:m["slot"] for m in macro["modules"]}
    records = {r["module_instance_id"]:r for r in conn.execute("""SELECT m.*,r.report_ref FROM v62_modules m
            LEFT JOIN v62_module_reports r USING(run_id,module_instance_id)
            WHERE m.run_id=? AND m.module_instance_id IN (SELECT value FROM json_each(?))""",
            (run_id, ids.canonical_json(list(requested))))}
    report_slots = {r["report_ref"]:requested[mid] for mid, r in records.items() if r["report_ref"]}
    modules, sources, module_slots = [], {}, {}
    for selection in macro["modules"]:
        mid, slot = selection["module_instance_id"], v6_recipes._name(selection["slot"])
        if mid in module_slots or slot in module_slots.values() or slot in node_slots.values():
            raise SchemaInvalid("Each module and computation needs a distinct recipe slot")
        row = records.get(mid)
        if not row:
            raise ScopeDenied("The selected module does not belong to this Goal")
        if not row["report_ref"] or not material.verify(row["report_ref"])["ok"]:
            raise SchemaInvalid("Save an executed module with a readable, verified report")
        grant = json.loads(row["grant_json"])
        ports = []
        for ref in grant.get("commission_inputs", grant["input_refs"]):
            if ref in report_slots:
                ports.append({"module":report_slots[ref], "output":"report"})
            elif ref in produced:
                matches = produced[ref]
                if len(matches) != 1:
                    raise SchemaInvalid("Ambiguous producer for a delegated module input")
                source_node, output = matches[0]
                ports.append({"step": node_slots[source_node], "output": output})
            elif ref in external:
                name = external[ref]
                meta = material.describe(ref)
                if meta["run_id"] != run_id:
                    raise ScopeDenied("Delegation input belongs to a different source Goal")
                required[name] = {"kind": meta["kind"], "media_type": meta["media_type"]}
                ports.append({"input": name})
            else:
                raise SchemaInvalid("Name every external module input or select its successful producer")
        if grant.get("target_claim_refs"):
            raise SchemaInvalid("Select modules with portable inputs; a specific source claim cannot be replayed")
        template = templates.get((row["module_id"], row["module_version"]))
        if (not template or template["declaration_sha256"] != grant["declaration_sha256"]
                or template.get("plugin") != grant.get("plugin")):
            raise SchemaInvalid("The saved commission differs from its frozen module declaration")
        original = json.loads(row["snapshot_json"])
        historical_providers = {}
        if original["method_pins"]:
            scope = conn.execute("""SELECT payload_json FROM events WHERE run_id=? AND kind=? AND event_id <
                    (SELECT event_id FROM events WHERE run_id=? AND kind='v62.module.opened' AND subject_id=?)
                    ORDER BY event_id DESC LIMIT 1""", (run_id, capability_views.EVENT, run_id, mid)).fetchone()
            methods = {m["capability_version"]:m for m in (
                json.loads(scope[0])["catalog"] if scope else inputs["capability_catalog"])}
            for cap, pin in original["method_pins"].items():
                if cap not in methods or methods[cap]["portable_sha256"] != pin:
                    raise SchemaInvalid("Cannot establish the method provider at the original commission")
                historical_providers[cap] = v6_recipes.provider_pin(methods[cap])
        module = {"slot": slot, "module_id": row["module_id"], "module_version": row["module_version"],
                  "declaration_sha256": grant["declaration_sha256"], "provider": grant.get("plugin"),
                  "provider_closure": template.get("provider_closure", []),
                  "question": _question(selection["question"], "research question"),
                  "expected_delivery": _question(selection["expected_delivery"], "delivery request"),
                  "inputs": ports, "capabilities": sorted(grant["capabilities"]),
                  "method_pins": original["method_pins"], "method_providers": historical_providers,
                  "method_options": grant.get("method_options", {}),
                  "tools": sorted(grant["tools"]), "sub_budget": json.loads(row["budget_json"]),
                  "wake_on": sorted(json.loads(row["wake_json"]))}
        if grant.get("communication"):
            module["communication"] = grant["communication"]
        modules.append(module)
        module_slots[mid] = slot
        sources[slot] = {"module_instance_id": mid, "report_ref": row["report_ref"]}
    join = {k: spec[k] for k in ("mode", "k", "timeout_seconds") if k in spec}
    join["ports"] = []
    for port in spec["ports"]:
        alternatives = []
        for origin in port["alternatives"]:
            if "module_instance_id" in origin:
                which, slot = "module", module_slots.get(origin["module_instance_id"])
            else:
                which, slot = "step", node_slots.get(origin["node_id"])
            if slot is None:
                raise SchemaInvalid("Select all upstream sources named by the saved join")
            alternatives.append({which: slot, "output_name": origin["output_name"]})
        join["ports"].append({"slot": port["slot"], "required": port.get("required", False),
                              "alternatives": alternatives})
    question = _question(macro.get("question", spec["question"]), "join question")
    strategy = {"modules": sorted(modules, key=lambda m: m["slot"]), "join": join,
                "decision_point": {"event": "join_outcome", "role": "run_lead", "question": question,
                                   "inspect": [p["slot"] for p in join["ports"]]}}
    proof = {"modules": sources, "join_key": macro["join_key"], "join_revision": record["revision"],
             "registered_event": record["registration_event"], "outcome_event": record["outcome_event"]}
    return strategy, proof


def visible_source(conn, run_id, args, task):
    from . import research_joins
    spec = args["macro"]
    record = research_joins.lookup(conn, run_id, spec["join_key"], spec.get("join_revision"))
    if (not record or record["goal_version"] != task["goal_version"]
            or not record["outcome_event"] or record["outcome_event"] > task["event_cursor"]):
        raise ScopeDenied("The saved join outcome must already be in this Lead's frozen context")
    visible = set(json.loads(task["visible_refs_json"]))
    for item in spec["modules"]:
        report = conn.execute("SELECT report_ref FROM v62_module_reports WHERE run_id=? AND module_instance_id=?",
                              (run_id, item["module_instance_id"])).fetchone()
        if not report or report[0] not in visible:
            raise ScopeDenied("Every saved module must have delivered its report to this Lead")


def _bound_nodes(conn, run_id, ref, digest, prefix, branch, slots, bindings):
    """Identify raw local aliases on committed target decisions; never use old node IDs."""
    if branch.startswith("local:") and slots:
        branches = {r["branch_id"] for r in conn.execute("""SELECT b.branch_id,d.receipt_json FROM v6_branches b
            JOIN v6_decisions d USING(run_id,decision_ref) WHERE b.run_id=?""", (run_id,))
            if json.loads(r["receipt_json"]).get("aliases", {}).get(branch) == r["branch_id"]}
    else:
        branches = {branch}
    candidates = {}
    for row in conn.execute("""SELECT n.node_id,n.spec_json,d.receipt_json FROM v6_nodes n
            JOIN v6_decisions d ON d.decision_ref=n.decision_ref AND d.run_id=n.run_id
            JOIN v6_decision_tasks t ON t.task_id=d.task_id
            JOIN v6_goals g ON g.run_id=n.run_id AND g.goal_version=t.goal_version
            WHERE n.run_id=? AND json_extract(n.spec_json,'$.recipe_decision.recipe')=?
              AND json_extract(n.spec_json,'$.recipe_decision.digest')=?""", (run_id, ref, digest)):
        spec = json.loads(row["spec_json"])
        slot = spec["recipe_decision"]["step"]
        if slot not in slots or spec["branch"] not in branches:
            continue
        alias = "local:" + prefix + "-" + slot
        if json.loads(row["receipt_json"])["aliases"].get(alias) == row["node_id"]:
            candidates.setdefault(slot, []).append((row["node_id"], spec))
    found = {}
    for slot in TopologicalSorter({k: s["depends_on"] for k, s in slots.items()}).static_order():
        step = slots[slot]
        if any(parent not in found for parent in step["depends_on"]):
            continue
        inputs = [bindings[p["input"]] if "input" in p else found[p["step"]] for p in step["inputs"]]
        ports = {i: p["output"] for i, p in enumerate(step["inputs"]) if "step" in p}
        dependencies = {found[k] for k in step["depends_on"]}
        for nid, spec in candidates.get(slot, ()):
            if (spec["capability"] != step["capability"] or spec.get("parameters", {}) != step["parameters"]
                    or spec["inputs"] != inputs or set(spec["depends_on"]) != dependencies
                    or {p["index"]:p["output"] for p in spec.get("input_ports", [])} != ports):
                continue
            if slot in found:
                raise SchemaInvalid("This prefix identifies multiple accepted graphs; select a new instance prefix")
            found[slot] = nid
    return found


def require_target(conn, run_id, macro):
    from . import v62_modules, v6_recipes
    from ..plugins import goal as providers
    target_methods = {m["capability_version"]:v6_recipes.provider_pin(m) for m in providers.catalog(conn, run_id)}
    pins = json.loads(conn.execute("SELECT capabilities_json FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()[0])
    offers = {}
    for module in macro["modules"]:
        decl = v62_catalog.resolve(conn, run_id, module["module_id"], module["module_version"])
        tools, options = v62_modules.delegation_options(conn, run_id, decl)
        optional = {cap for cap, pin in module["method_options"].items() if options.get(cap) == pin}
        required_tools = set(module["tools"])
        if not optional:
            required_tools.discard("gx_select_capability")
        if (decl["declaration_sha256"] != module["declaration_sha256"] or decl["plugin"] != module["provider"]
                or decl.get("provider_closure", []) != module["provider_closure"] or not required_tools <= set(tools)):
            raise ScopeDenied("The target Goal does not provide the saved research module and tools")
        if (not set(module["capabilities"]) <= set(decl["capabilities"])
                or any(pins.get(cap) != pin for cap, pin in module["method_pins"].items())
                or any(target_methods.get(cap) != pin for cap, pin in module["method_providers"].items())):
            raise ScopeDenied("A delegated method or its exact release is unavailable in the target Goal")
        offers[module["slot"]] = (sorted(required_tools), sorted(optional))
    return offers


def expand(conn, run_id, definition, digest, arguments, answer, *, visible_refs=None):
    from . import v62_outputs, v62_reports
    macro = definition["macro"]
    offers = require_target(conn, run_id, macro)
    slots = {step["slot"]: step for step in definition["steps"]}
    prefix, bindings = arguments["prefix"], arguments["inputs"]
    branch = arguments["branch"]
    nodes = _bound_nodes(conn, run_id, definition["reference"], digest, prefix, branch, slots, bindings)
    if nodes:
        members = {conn.execute("SELECT branch_id FROM v6_nodes WHERE node_id=?", (nid,)).fetchone()[0]
                   for nid in nodes.values()}
        if len(members) != 1:
            raise SchemaInvalid("This prefix and branch alias identify multiple graphs; choose a unique instance prefix")
        known = {"local:" + prefix + "-" + slot: nid for slot, nid in nodes.items()}
        remaining = [op for op in answer["ops"] if op["node"] not in known]
        for op in remaining:
            op["branch"] = next(iter(members))
            for field in ("inputs", "depends_on"):
                op[field] = [known.get(value, value) for value in op[field]]
        answer.update(ops=remaining, bound_steps=nodes)
        answer.pop("submission", None)
    version = conn.execute("SELECT goal_version FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()[0]
    # The prefix names one instance across the transition from local branch alias to durable branch ID.
    from . import harness_policy, recipe_instances
    identity = recipe_instances.identity(conn, run_id, definition["reference"], digest, version, prefix, bindings, branch)
    modern = harness_policy.current(conn,run_id)
    keys = {m["slot"]:"recipe-" + identity + "-" + m["slot"] for m in macro["modules"]}
    aliases = {s:"module_" + ids.sha256_of_obj([run_id, key])[:32] for s,key in keys.items()}
    reports = {}
    if any("module" in port for m in macro["modules"] for port in m["inputs"]):
        released = set(v62_reports.released_report_refs(conn, run_id))
        reports = {r["module_instance_id"]:r["report_ref"] for r in conn.execute(
            "SELECT module_instance_id,report_ref FROM v62_module_reports WHERE run_id=?", (run_id,))
            if r["report_ref"] in released and (visible_refs is None or r["report_ref"] in visible_refs)}
    calls, pending = [], []
    # Rounds display chronology; an existing commission is returned without a new open call.
    number = conn.execute("SELECT COALESCE(MAX(round),0)+1 FROM v62_modules WHERE run_id=?", (run_id,)).fetchone()[0]
    for module in macro["modules"]:
        slot = module["slot"]
        tools, options = offers[slot]
        key, mid = keys[slot], aliases[slot]
        existing = conn.execute("SELECT grant_json FROM v62_modules WHERE run_id=? AND module_instance_id=?", (run_id, mid)).fetchone()
        selected = []
        for port in module["inputs"]:
            if "input" in port:
                selected.append(bindings[port["input"]])
            elif "module" in port:
                ref = reports.get(aliases[port["module"]])
                if not ref:
                    pending.append({"module":slot, "await":port})
                    break
                selected.append(ref)
            else:
                node = conn.execute("SELECT job_id,status FROM v6_nodes WHERE run_id=? AND node_id=?",
                                    (run_id, nodes.get(port["step"]))).fetchone()
                outputs = v62_outputs.published_outputs(conn, run_id, node["job_id"]) if node and node["status"] in ("SUCCEEDED","REUSED") else []
                output = next((v for v in outputs if v["output_name"] == port["output"]), None)
                if not output or (visible_refs is not None and output["artifact_ref"] not in visible_refs):
                    pending.append({"module": slot, "await": port})
                    break
                selected.append(output["artifact_ref"])
        else:
            if existing:
                grant = json.loads(existing["grant_json"])
                if grant.get("commission_inputs", grant["input_refs"]) != selected:
                    raise SchemaInvalid("This instance prefix is bound to a different module input; use a new prefix")
                calls.append({"slot":slot, "module_instance_id":mid, "opened":True})
                continue
            args = {"module_id": module["module_id"], "module_version": module["module_version"],
                    "operation_key": key, "round": number,
                    "focus": module["question"], "expected_delivery": module["expected_delivery"],
                    "input_refs": selected, "capabilities": module["capabilities"], "tools": tools,
                    "wake_on": module["wake_on"]}
            if "sub_budget" in module:
                args["sub_budget"] = module["sub_budget"]
            if "gx_decide_revision" in tools:
                args["method_choices"] = options
            if "communication" in module:
                args["communication"] = module["communication"]
            calls.append({"tool":"gx_open_module", "arguments":args, "slot":slot,
                          **({"needed_arguments":["sub_budget"]} if "sub_budget" not in args else {}),
                          "module_instance_id":mid, "opened":False})
    gate = macro["join"]
    ports, missing = [], []
    unavailable = {item["module"] for item in pending}
    waiting = set()
    for port in gate["ports"]:
        alternatives = []
        for source in port["alternatives"]:
            if "step" in source and source["step"] not in nodes:
                missing.append(source["step"])
            elif source.get("module") in unavailable:
                waiting.add(source["module"])
            else:
                location = ({"node_id": nodes[source["step"]]} if "step" in source else
                            {"module_instance_id": aliases[source["module"]]})
                alternatives.append({**location, "output_name": source["output_name"]})
        ports.append({**port, "alternatives": alternatives})
    point = macro["decision_point"]
    answer.update(module_calls=calls, pending_module_inputs=pending, decision_point=point)
    if missing:
        answer["join_pending_nodes"] = sorted(set(missing))
    if waiting:
        answer["join_pending_modules"] = sorted(waiting)
    if not missing and not waiting:
        watch = {k:gate[k] for k in ("mode","k","timeout_seconds") if k in gate}
        answer["join_call"] = {"tool":"gx_watch_join", "arguments":{**watch, "key":"recipe-" + identity,
                               "question":point["question"], "ports":ports}}
    answer["adoption"] = ("Submit the chosen computation ops and module_calls on this Goal. When computations feed "
                          "a module or join, re-expand with the same prefix and new material bindings. Register "
                          "join_call after its producers exist. Decide from this Goal's resulting evidence.")
    if modern:
        from . import research_joins
        answer["instance_key"] = "recipe-" + identity
        existing = research_joins.lookup(conn,run_id,answer["instance_key"])
        if existing:
            answer["logical_join"] = {key:existing[key] for key in ("key","revision","state","registration_event","outcome_event")}
        answer["instructions"] = answer["adoption"] = ("Previewing is read-only. Adopt with adopt=true or submit an issued module commission "
                              "or computation. GX then watches the named dependencies even for producers not yet born. "
                              "Set each commission's sub_budget from this Goal. Continue with the same branch, prefix and bindings.")
    return answer
