"""Turn a completed analytical subgraph into portable, pinned Goal operations."""
from collections import Counter
from copy import deepcopy
import json
import re

from .. import contracts
from ..artifacts import ArtifactService
from ..capabilities import manifest as capabilities
from ..capabilities import registry, shipped
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied, VersionImmutable
from ..jobs.service import JobService
from ..persistence import store
from ..plugins import goal as goal_plugins
from . import manifest, recipe_stages, v6_jobs, v62_outputs

SAVE = "gx_save_recipe"
INSTANTIATE = "gx_instantiate_recipe"
WATCH = "gx_watch_join"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")


def save_tool(modern):
    spec = contracts.native_goal_tool_spec(SAVE)
    if modern:
        spec = deepcopy(spec)
        spec["description"] += " A macro may select performed modules and computations, the named evidence join and an open decision question for a fresh Goal."
        return spec
    original = deepcopy(spec)
    for field in ("macro", *recipe_stages.FIELDS):
        original["inputSchema"]["properties"].pop(field, None)
    return original


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise SchemaInvalid("Recipe names, slots and versions require a short identifier")
    return value


def _mapping(rows, key, value):
    result = {_name(row[key]): row[value] for row in rows}
    if len(result) != len(rows) or len(set(result.values())) != len(rows):
        raise SchemaInvalid("Recipe slot names and bindings must be distinct")
    return result


def provider_pin(entry):
    """Pin the Goal adapter and its dependency closure as well as the executable bundle."""
    if not entry.get("plugin"):
        return None
    return {"plugin": entry["plugin"], "closure": entry.get("provider_closure", [entry["plugin"]])}


def _card(reference, digest, definition):
    return {**({"schema": recipe_stages.SCHEMA} if definition["schema"] == recipe_stages.SCHEMA else {}),
            "kind": "recipe", "reference": reference, "digest": digest, "title": definition["title"],
            "id_alignment": definition["id_alignment"], "missing_values": definition["missing_values"],
            "units": definition["units"], "inputs": definition["inputs"], "methods": {s["slot"]: s["capability"] for s in definition["steps"]},
            "method_pins": {s["capability"]: s["method_digest"] for s in definition["steps"]},
            "outputs": {s["slot"]: s["outputs"] for s in definition["steps"]},
            **{key: definition[key] for key in ("decision_point", "calibration", "provider_pins", "macro", *recipe_stages.FIELDS) if key in definition}}


def cards(conn):
    return [_card(ref, digest, json.loads(doc)) for ref, digest, doc in
            conn.execute("SELECT reference,digest,definition_json FROM v6_saved_recipes ORDER BY reference")]


def prepare(conn, run_id, arguments):
    reference = _name(arguments["recipe_id"]) + "@" + _name(arguments["version"])
    slots = _mapping(arguments["nodes"], "slot", "node_id")
    macro = "macro" in arguments
    if macro and ("calibration" in arguments or "decision_point" in arguments):
        raise SchemaInvalid("A macro decision uses its named join and fresh upstream evidence")
    if not slots and not macro:
        raise SchemaInvalid("Select at least one completed analytical node")
    bindings = _mapping(arguments["inputs"], "name", "artifact_ref")
    external = {ref: slot for slot, ref in bindings.items()}
    node_slots = {nid: slot for slot, nid in slots.items()}
    portable_natures = {"analysis", "observed", "model_prediction"}
    staged = any(field in arguments for field in recipe_stages.FIELDS)
    enriched = staged or macro or "decision_point" in arguments or "calibration" in arguments
    proofs = {}
    for slot, nid in slots.items():
        node = v6_jobs._node(conn, run_id, nid)
        if not node or node["status"] not in ("SUCCEEDED", "REUSED"):
            raise SchemaInvalid(f"Recipe step {slot} needs a completed Job on this Goal")
        spec = json.loads(node["spec_json"])
        if spec.get("evidence_gate"):
            staged = enriched = True
        if set(spec["depends_on"]) - node_slots.keys():
            raise SchemaInvalid("Include the whole dependency or save a subgraph without an external condition")
        proof = manifest.read_manifest(conn, node["job_id"])
        v6_jobs.confirmed_result_identity(conn, nid, proof)
        decls = proof["admission"].get("output_declarations") or []
        scientific = {d["name"] for d in decls if d.get("nature") in portable_natures}
        if not scientific or any(d.get("nature") and d["nature"] not in portable_natures for d in decls):
            raise SchemaInvalid("Recipes require outputs declared as analysis, observed or model_prediction")
        enriched |= any(d.get("nature") == "model_prediction" for d in decls)
        inventory = v62_outputs.recorded_outputs(conn, run_id, node["job_id"], record=proof, check_publications=True)[0]
        # License/notice files stay in every Job's manifest. They are not
        # scientific evidence and cannot be a recipe's decision or data port.
        outputs = [out for out in inventory if out["declaration_name"] in scientific]
        if not outputs:
            raise SchemaInvalid("The selected Job produced no declared scientific output")
        proofs[nid] = (spec, node, proof, outputs)

    # A Lead may have inspected the producer before opening the consumer. This
    # ordinary path names the published artifact directly in the consumer Job.
    # Restore that data edge when both Jobs form part of a portable recipe.
    produced = {}
    for producer_id, (_, _, _, inventory) in proofs.items():
        for output in inventory:
            produced.setdefault(output["artifact_ref"], []).append((producer_id, output["output_name"]))
    steps, sources, required, provider_pins = [], {}, {}, {}
    # Jobs without a scope revision used the immutable opening catalogue. Later
    # revisions carry the exact provider in the Job admission itself.
    initial = json.loads(conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0])
    opening = {m["capability_version"]: m for m in initial["inputs"]["capability_catalog"]}
    assets = ArtifactService(conn)
    for slot, nid in sorted(slots.items()):
        spec, node, proof, outputs = proofs[nid]
        pinned = proof["admission"]["capability"]
        view = registry.candidate(conn, spec["capability"])
        if view["manifest_sha256"] != pinned["sha256"]:
            raise SchemaInvalid("The installed method differs from the one used in the source Job")
        portable = shipped.portable_digest(view["bundle"])
        source_grant = proof["admission"].get("goal_capability") or opening.get(spec["capability"])
        if source_grant is None:
            raise SchemaInvalid("The source Job has no frozen method grant")
        provider_pins[spec["capability"]] = provider_pin(source_grant)
        ports = v6_jobs._ports(spec)
        selected = []
        dependencies = {node_slots[dep] for dep in spec["depends_on"]}
        for index, ref in enumerate(spec["inputs"]):
            if ref in node_slots:
                _, producer, _, inventory = proofs[ref]
                name = ports.get(index)
                if name is None:
                    matches = [row for row in inventory if row["artifact_ref"] == producer["result_ref"]]
                    if len(matches) != 1:
                        raise SchemaInvalid("Cannot resolve the historical primary result to one named output")
                    name = matches[0]["output_name"]
                if not any(row["output_name"] == name for row in inventory):
                    raise SchemaInvalid("Source graph used an unavailable named output")
                selected.append({"step": node_slots[ref], "output": name})
            elif enriched and (upstream := [pair for pair in produced.get(ref, []) if pair[0] != nid]):
                candidates = [pair for pair in upstream if pair[0] in spec["depends_on"]] or upstream
                if len(candidates) != 1:
                    raise SchemaInvalid("A shared historical output needs an unambiguous producing dependency")
                producer_id, name = candidates[0]
                dependencies.add(node_slots[producer_id])
                selected.append({"step": node_slots[producer_id], "output": name})
            else:
                if ref not in external:
                    raise SchemaInvalid("Name each external input of the selected subgraph")
                meta = assets.describe(ref)
                if meta["run_id"] != run_id:
                    raise ScopeDenied("Source input belongs to a different Goal")
                key = external[ref]
                required[key] = {"kind": meta["kind"], "media_type": meta["media_type"]}
                selected.append({"input": key})
        if any(row["nature"] not in portable_natures for row in outputs):
            raise SchemaInvalid("Cannot register a recipe with unverified scientific output provenance")
        steps.append({"slot": slot, "capability": spec["capability"], "method_digest": portable,
                      "inputs": selected, "outputs": sorted(
                          ({"name": row["output_name"], "kind": row["kind"],
                            **({"nature": row["nature"], "source_roles": [i["role"] for i in row["source_inputs"]]}
                               if enriched else {})} for row in outputs),
                          key=lambda o: o["name"]),
                      "depends_on": sorted(dependencies),
                      "parameters": spec["parameters"], "question": (
                          f"Apply {spec['capability']} to the named inputs of this Goal."
                          if enriched else spec["question"])})
        if gate := spec.get("evidence_gate"):
            steps[-1]["evidence_gate"] = recipe_stages.capture_gate(gate, proofs, node_slots, external)
        sources[slot] = {"node_id": nid, "job_id": node["job_id"], "completion_sha256": proof["manifest_hash"]}
    if macro:
        from . import v6_macro_recipes
        strategy, macro_proof = v6_macro_recipes.capture(conn, run_id, arguments, node_slots, external, produced, required)
    if required.keys() != bindings.keys():
        raise SchemaInvalid("The named inputs must match exactly the external inputs of the subgraph")
    definition = {"schema": "gx.research-recipe/1", "reference": reference,
                  "inputs": required, "steps": steps}
    previous = conn.execute("SELECT definition_json FROM v6_saved_recipes WHERE reference=?", (reference,)).fetchone()
    if not previous or "provider_pins" in json.loads(previous[0]):
        definition["provider_pins"] = provider_pins
    if enriched:
        definition["schema"] = "gx.research-recipe/2"
    if macro:
        definition.update(schema="gx.research-recipe/3", macro=strategy)
        sources = {"steps": sources, "macro": macro_proof}
    by_slot = {step["slot"]: step for step in steps}
    if point := arguments.get("decision_point"):
        step = by_slot.get(point["step"])
        if not step or point["output"] not in {out["name"] for out in step["outputs"]}:
            raise SchemaInvalid("A recipe decision needs a named scientific output of one selected step")
        definition["decision_point"] = {**point, "role": "run_lead", "event": "job_settled"}
    if calibration := arguments.get("calibration"):
        slot = calibration["step"]
        step = by_slot.get(slot)
        if (not step or len(steps) < 2 or step["depends_on"] or (point and point["step"] == slot)
                or any(src.get("step") == slot for other in steps for src in other["inputs"])):
            raise SchemaInvalid("Select an independent calibration step whose output is not a science input or decision trigger")
        definition["calibration"] = calibration
    for key in ("title", "units", "id_alignment", "missing_values"):
        if not isinstance(arguments[key], str) or not arguments[key].strip():
            raise SchemaInvalid(f"A reusable recipe needs {key}")
        definition[key] = arguments[key]
    if staged:
        recipe_stages.require_current(conn, run_id)
        recipe_stages.capture(conn, definition, arguments, bindings, assets)
    return definition, sources


def publish(conn, run_id, definition, sources):
    reference = definition["reference"]
    digest = ids.sha256_of_obj(definition)
    with store.transaction(conn, transaction_owner="ScientificLead", operation="save_recipe", run_id=run_id):
        conn.execute("INSERT INTO v6_saved_recipes VALUES(?,?,?,?,?) ON CONFLICT(reference) DO NOTHING",
                     (reference, digest, ids.canonical_json(definition), run_id, ids.canonical_json(sources)))
        saved = conn.execute("SELECT digest FROM v6_saved_recipes WHERE reference=?", (reference,)).fetchone()
        if saved[0] != digest:
            raise VersionImmutable(f"Recipe {reference} already identifies another definition")
    return _card(reference, digest, definition)


def save(conn, run_id, arguments):
    return publish(conn, run_id, *prepare(conn, run_id, arguments))


def decision_for(conn, spec, *, run_id=None):
    marker = spec["recipe_decision"]
    row = conn.execute("SELECT definition_json FROM v6_saved_recipes WHERE reference=? AND digest=?",
                       (marker["recipe"], marker["digest"])).fetchone()
    definition = json.loads(row[0]) if row else {}
    point = definition.get("decision_point")
    step = next((s for s in definition.get("steps", ()) if s["slot"] == marker["step"]), None)
    staged = definition.get("schema") == recipe_stages.SCHEMA
    if staged:
        point = next((p for p in definition.get("decision_points", ()) if p["step"] == marker["step"]), None)
    macro = definition.get("macro")
    if (not step or step["capability"] != spec["capability"]
            or (not macro and not staged and (not point or point["step"] != marker["step"]))):
        raise SchemaInvalid("The recipe decision must reference its saved method and evidence step")
    if run_id is not None:
        from . import harness_policy
        if staged:
            recipe_stages.require_current(conn, run_id)
        if macro and not harness_policy.active(conn, run_id):
            raise ScopeDenied("This archived Goal has no macro research policy")
        cap = spec["capability"]
        grant = next((g for g in goal_plugins.catalog(conn, run_id) if g["capability_version"] == cap), {})
        if grant.get("portable_sha256") != step["method_digest"] or (
                cap in definition.get("provider_pins", {}) and provider_pin(grant) != definition["provider_pins"][cap]):
            raise ScopeDenied("The recipe decision's method or provider differs from the saved procedure")
        if step["parameters"] != spec.get("parameters", {}):
            raise SchemaInvalid("The recipe decision's method parameters differ from the saved procedure")
    return point


def settled_decision(conn, run_id, node, spec):
    point = decision_for(conn, spec)
    if point is None:
        return None  # A macro watches its joined evidence at the registered decision point.
    outputs = v62_outputs.published_outputs(conn, run_id, node["job_id"]) if node["result_ref"] else []
    evidence = next((out for out in outputs if out["output_name"] == point["output"]), None)
    if evidence:
        state = "read_and_decide"
        # A later accepted Lead decision handles this point when its own frozen
        # brief contained this node and it cited this particular new artifact.
        handled = conn.execute("""SELECT 1 FROM v6_decisions d JOIN research_contexts c USING(context_ref)
            JOIN json_each(c.snapshot_json,'$.recipe_decisions') p
            JOIN json_each(d.request_json,'$.decision.evidence_refs') e
            WHERE d.run_id=? AND json_extract(p.value,'$.node_id')=? AND e.value=? LIMIT 1""",
            (run_id, node["node_id"], evidence["artifact_ref"])).fetchone()
        if handled:
            return None
    else:
        state = "missing_named_output" if node["result_ref"] else "job_" + node["status"].lower()
    return {**spec["recipe_decision"], **point, "job_id": node["job_id"], "node_id": node["node_id"],
            "state": state, "evidence": evidence}


def _calibration_match(conn, run_id, step, bindings, source):
    cap = step["capability"]
    method = registry.candidate(conn, cap)
    proof = manifest.read_manifest(conn, source["job_id"])
    if proof["manifest_hash"] != source["completion_sha256"]:
        return False
    v6_jobs.confirmed_result_identity(conn, source["node_id"], proof)
    if method["manifest"].get("weights"):
        return False  # A model Job is a new measurement unless its full calibration environment can be compared.
    original = json.loads(conn.execute("SELECT identity_json FROM v6_job_attempts WHERE node_id=?",
                                       (source["node_id"],)).fetchone()[0])
    original.pop("independent_node", None)
    if shipped.portable_digest(method["bundle"]) != step["method_digest"]:
        return False
    for part in ("entry_script", "parser"):
        if script := method["bundle"].get(part):
            capabilities.script_path(script)
    assets = ArtifactService(conn)
    entries = []
    refs = [bindings[port["input"]] for port in step["inputs"]]
    try:
        inputs, params = v6_jobs.bind_inputs(conn, run_id, cap, method, refs, step["parameters"], writable=False)
    except ScopeDenied:
        # A binder that must publish a derived input can materialize it in a fresh Job.
        return False
    for role, ref in inputs.items():
        verified = assets.verify(ref)
        if not verified["ok"]:
            raise SchemaInvalid("Target calibration input differs from its published bytes")
        entries.append({"role": role, "sha256": verified["observed_sha256"]})
    identity = v6_jobs.method_identity(conn, run_id, cap, method, entries, params)
    if identity != original or identity.get("required_data_sha256", {}) is None:
        return False
    return all(assets.verify(output["artifact_ref"])["ok"] for output in proof["completion"]["outputs"])


def instantiate(conn, run_id, arguments, *, visible_refs=None):
    reference = arguments["recipe"]
    row = conn.execute("SELECT digest,definition_json,source_run,sources_json FROM v6_saved_recipes WHERE reference=?", (reference,)).fetchone()
    if not row or arguments["digest"] != row[0]:
        raise SchemaInvalid("Recipe reference or digest does not match the saved workflow")
    definition = json.loads(row[1])
    bindings = arguments["inputs"]
    if not isinstance(bindings, dict) or bindings.keys() != definition["inputs"].keys():
        raise SchemaInvalid("Bind every named recipe input to an artifact of this Goal")
    assets = ArtifactService(conn)
    for name, ref in bindings.items():
        if not isinstance(ref, str):
            raise SchemaInvalid(f"Recipe input {name} must be an artifact reference")
        item = assets.describe(ref)
        if item["run_id"] != run_id:
            raise ScopeDenied("A recipe must read inputs from its target Goal")
        if any(item[key] != value for key, value in definition["inputs"][name].items()):
            raise SchemaInvalid(f"Input {name} does not match the saved artifact kind and media type")
    staged = definition["schema"] == recipe_stages.SCHEMA
    if staged:
        recipe_stages.validate_target(conn, run_id, definition, bindings, assets)
    grants = json.loads(conn.execute("SELECT capabilities_json FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()[0])
    target_providers = {g["capability_version"]: provider_pin(g) for g in goal_plugins.catalog(conn, run_id)}
    for cap, pin in definition.get("provider_pins", {}).items():
        if cap not in target_providers or target_providers[cap] != pin:
            raise ScopeDenied(f"The target Goal needs the saved provider and dependencies for {cap}")
    prefix = _name(arguments["prefix"])
    branch = arguments["branch"]
    if definition["schema"] in ("gx.research-recipe/2","gx.research-recipe/3",recipe_stages.SCHEMA) and definition["steps"] and not branch.startswith("local:"):
        existing = conn.execute("SELECT 1 FROM v6_branches WHERE run_id=? AND branch_id=?",
                                (run_id, branch)).fetchone()
        if not existing:
            suggested = "local:" + (branch if _NAME.fullmatch(branch) else prefix)
            raise SchemaInvalid(
                f"No branch {branch!r} exists in this Goal. For a new branch, call gx_instantiate_recipe "
                f"again with branch {suggested!r} to receive the open_branch fields and budget. "
                "To extend an existing branch, pass its open branch_id from gx_task_context.",
                details={"field": "branch", "suggested_new_branch": suggested})
    alias = {s["slot"]: f"local:{prefix}-{s['slot']}" for s in definition["steps"]}
    calibration = definition.get("calibration")
    point = definition.get("decision_point")
    repeat = arguments.get("independent_repeat", False)
    skipped = None
    receipt = None
    steps = definition["steps"]
    if not calibration and ("calibration_scope" in arguments or "calibration_parameters" in arguments):
        raise SchemaInvalid("This recipe has no registered calibration step")
    if calibration:
        slot = calibration["step"]
        steps = [dict(step, parameters=arguments.get("calibration_parameters", step["parameters"]))
                 if step["slot"] == slot else step for step in steps]
        step = next(item for item in steps if item["slot"] == slot)
        if grants.get(step["capability"]) != step["method_digest"]:
            raise ScopeDenied("The target Goal must have the pinned calibration method")
        source = json.loads(row["sources_json"])[slot]
        if (not repeat and arguments.get("calibration_scope") == calibration["scope"]
                and _calibration_match(conn, run_id, step, bindings, source)):
            skipped = slot
            receipt = {"step": slot, "status": "referenced", "source_run": row["source_run"], **source,
                       "scope": calibration["scope"]}
        else:
            receipt = {"step": slot, "status": "run_on_target", "scope": arguments.get("calibration_scope")}
    ops = []
    for step in steps:
        if step["slot"] == skipped:
            continue
        cap = step["capability"]
        if grants.get(cap) != step["method_digest"]:
            raise ScopeDenied(f"The target Goal needs the saved method {cap} at its exact release digest")
        inputs, ports = [], []
        for index, src in enumerate(step["inputs"]):
            if "input" in src:
                inputs.append(bindings[src["input"]])
            else:
                inputs.append(alias[src["step"]])
                ports.append({"index": index, "output": src["output"]})
        ops.append({"op": "add_node", "node": alias[step["slot"]], "branch": branch,
                    "capability": cap, "parameters": step["parameters"], "inputs": inputs,
                    "depends_on": [alias[d] for d in step["depends_on"] if d != skipped], "question": step["question"],
                    **({"independent_repeat": True} if repeat or definition.get("evaluation",{}).get("step") == step["slot"] or (calibration and step["slot"] == calibration["step"]) else {}),
                    **({"recipe_decision": {"recipe": reference, "digest": row[0], "step": step["slot"]}}
                       if staged or definition.get("macro") or (point and point["step"] == step["slot"]) else {}),
                    **({"input_ports": ports} if ports else {})})
        if gate := step.get("evidence_gate"):
            ops[-1]["evidence_gate"] = {"producer_node": alias[gate["producer_step"]], "producer_version": 1,
                "source_ref": bindings[gate["source_input"]], **{k:gate[k] for k in ("output_name","pointer","equals","require_lead_review")}}
    answer = {"recipe": reference, "digest": row[0], "ops": ops,
              **({"decision_point": point} if point else {}), **({"calibration": receipt} if receipt else {})}
    if definition["schema"] in ("gx.research-recipe/2","gx.research-recipe/3",recipe_stages.SCHEMA) and definition["steps"] and branch.startswith("local:"):
        from . import v6_graph
        contracts = v6_graph.decision_schema()["properties"]["ops"]["items"]["oneOf"]
        opening = next(kind for kind in contracts if kind["properties"]["op"]["const"] == "open_branch")
        minimum = Counter()
        for op in ops:
            method = registry.candidate(conn, op["capability"])["manifest"]
            minimum.update(JobService._budget_amounts(capabilities.effective_resources(method, {})))
        answer["submission"] = {
            "tool": "gx_decide_revision", "branch": branch,
            "instructions": "Prepend one open_branch operation to these add_node operations. "
                            "Fill the scientific fields for this Goal and fund the expanded Jobs.",
            "required_open_branch_fields": opening["required"], "minimum_budget_limits": dict(minimum)}
    if staged:
        answer.update({k:definition[k] for k in recipe_stages.FIELDS if k in definition})
        recipe_stages.bound_ops(conn, run_id, definition, row[0], arguments, answer)
    if "macro" in definition:
        from . import harness_policy, v6_macro_recipes
        if not harness_policy.active(conn, run_id):
            raise ScopeDenied("This archived Goal has no macro research policy")
        return v6_macro_recipes.expand(conn, run_id, definition, row[0], arguments, answer, visible_refs=visible_refs)
    return answer


def authorize(conn, identity, tool, args):
    from . import v6_decisions
    _, task, _, _ = v6_decisions.authority(conn, **{k: v for k, v in identity.items() if k != "call_id"})
    if task["scope"] != "run":
        raise ScopeDenied("Only the Goal Lead can publish or instantiate a workspace recipe")
    visible = set(json.loads(task["visible_refs_json"]))
    if tool == WATCH:
        return task
    if tool == SAVE:
        if "macro" in args:
            from . import harness_policy, v6_macro_recipes
            if not harness_policy.active(conn, identity["run_id"]):
                raise ScopeDenied("This archived Goal has no macro research policy")
            v6_macro_recipes.visible_source(conn, identity["run_id"], args, task)
        nodes = json.loads(task["snapshot_json"]).get("nodes", [])
        released = {node["node_id"] for node in nodes
                    if node.get("result_ref") and node.get("status") in ("SUCCEEDED", "REUSED")}
        if any(row["node_id"] not in released for row in args["nodes"]):
            raise ScopeDenied("Recipe steps must be completed and visible in this Lead brief")
        refs = [row["artifact_ref"] for row in args["inputs"]]
    else:
        refs = list(args["inputs"].values())
    if any(not isinstance(ref, str) or ref not in visible for ref in refs):
        raise ScopeDenied("Recipe inputs must be visible in this Lead brief")
    return task
