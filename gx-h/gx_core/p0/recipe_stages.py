"""Translate performed release-pinned stages to fresh evidence and independent decisions."""
import json

from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied
from . import harness_policy, v6_macro_recipes

SCHEMA = "gx.research-recipe/4"
FIELDS = ("decision_points", "evaluation", "children")


def require_current(conn, run_id):
    if not harness_policy.current(conn, run_id):
        raise ScopeDenied("This archived Goal has no multi-stage recipe contract")


def capture_gate(gate, proofs, slots, external):
    producer = gate["producer_node"]
    if producer not in proofs or gate["source_ref"] not in external:
        raise SchemaInvalid("Include the filter producer and name its external measured input")
    spec, node, _, outputs = proofs[producer]
    selected = [o for o in outputs if (o["output_name"] == gate["output_name"] if
                "output_name" in gate else o["artifact_ref"] == node["result_ref"])]
    if len(selected) != 1 or node["version"] != gate["producer_version"]:
        raise SchemaInvalid("A portable filter needs one named verified output of its actual producer revision")
    roles = [i["role"] for i in selected[0]["source_inputs"] if i["artifact_ref"] == gate["source_ref"]
             and i["role"] == gate.get("source_role",i["role"])]
    if len(roles) != 1 or gate["source_ref"] not in spec["inputs"]:
        raise SchemaInvalid("The filter source must be an attributable measured input of that named output")
    return {"producer_step": slots[producer], "source_input": external[gate["source_ref"]],
            "source_role": roles[0], "output_name": selected[0]["output_name"],
            **{k:gate[k] for k in ("pointer", "equals", "require_lead_review")}}


def _registered(conn, ref, digest, ancestors):
    if ref in ancestors:
        raise SchemaInvalid("Nested recipe references must be acyclic")
    row = conn.execute("SELECT digest,definition_json FROM v6_saved_recipes WHERE reference=?", (ref,)).fetchone()
    if not row or row["digest"] != digest:
        raise SchemaInvalid("Nested recipe reference or pinned digest is unavailable")
    doc = json.loads(row["definition_json"])
    if ids.sha256_of_obj(doc) != digest:
        raise SchemaInvalid("Nested recipe content differs from its registered digest")
    for child in doc.get("children", []):
        _registered(conn, child["recipe"], child["digest"], ancestors | {ref})
    return doc


def _gate_mapping(gate, steps, inputs):
    return dict(gate, producer_step=steps[gate["producer_step"]], source_input=inputs[gate["source_input"]])


def _linked(conn, definition, child, points, evaluations):
    ref = child["recipe"]
    doc = _registered(conn, ref, child["digest"], {definition["reference"]})
    mapping, inputs = child["steps"], child["inputs"]
    existing = {s["slot"]:s for s in definition["steps"]}
    if (set(mapping) != {s["slot"] for s in doc["steps"]} or inputs.keys() != doc["inputs"].keys()
            or len(set(mapping.values())) != len(mapping) or len(set(inputs.values())) != len(inputs)
            or set(mapping.values()) - existing.keys() or set(inputs.values()) - definition["inputs"].keys()):
        raise SchemaInvalid("Bind every nested recipe step and material to its distinct performed parent slot")
    if calibration := doc.get("calibration"):
        mapped = dict(calibration, step=mapping[calibration["step"]])
        if definition.get("calibration") != mapped:
            raise SchemaInvalid("Preserve the nested calibration scope and independent step")
    if macro := doc.get("macro"):
        from copy import deepcopy
        projected = deepcopy(macro)
        for module in projected["modules"]:
            module.pop("sub_budget", None)
            for port in module["inputs"]:
                if "input" in port:
                    port["input"] = inputs[port["input"]]
                if "step" in port:
                    port["step"] = mapping[port["step"]]
        for port in projected["join"]["ports"]:
            for option in port["alternatives"]:
                if "step" in option:
                    option["step"] = mapping[option["step"]]
        if definition.get("macro") != projected:
            raise SchemaInvalid("Preserve the nested module slots, questions and join when composing the parent")
    for s in doc["steps"]:
        action = existing[mapping[s["slot"]]]
        ports = [{"input":inputs[p["input"]]} if "input" in p else {"step":mapping[p["step"]],"output":p["output"]}
                 for p in s["inputs"]]
        outputs = {o["name"]:o for o in action["outputs"]}
        if (any(action[k] != s[k] for k in ("capability","method_digest","parameters"))
                or ports != action["inputs"] or {mapping[p] for p in s["depends_on"]} != set(action["depends_on"])
                or any(any(outputs.get(o["name"],{}).get(k) != v for k,v in o.items()) for o in s["outputs"])
                or definition.get("provider_pins",{}).get(s["capability"]) != doc.get("provider_pins",{}).get(s["capability"])
                or action.get("evidence_gate") != (_gate_mapping(s["evidence_gate"],mapping,inputs) if "evidence_gate" in s else None)):
            raise SchemaInvalid("A nested step must reproduce its pinned procedure, named ports and filter")
    if doc.get("decision_point"):
        points.append(dict(doc["decision_point"],step=mapping[doc["decision_point"]["step"]]))
    points.extend(dict(p,step=mapping[p["step"]]) for p in doc.get("decision_points",[]))
    if e := doc.get("evaluation"):
        evaluations.append({"step":mapping[e["step"]],"holdout_input":inputs[e["holdout_input"]]})


def check_holdout(evaluation, bindings, assets):
    held = evaluation["holdout_input"]
    digest = assets.describe(bindings[held])["sha256"]
    if any(assets.describe(ref)["sha256"] == digest for key,ref in bindings.items() if key != held):
        raise SchemaInvalid("The evaluation holdout must have distinct bytes from development materials")


def capture(conn, definition, args, bindings, assets):
    definition["schema"] = SCHEMA
    for module in definition.get("macro", {}).get("modules", []):
        module.pop("sub_budget", None)
    steps = {s["slot"]:s for s in definition["steps"]}
    points = list(args.get("decision_points", []))
    if "decision_point" in definition:
        points.append(definition.pop("decision_point"))
    evaluations = [args["evaluation"]] if "evaluation" in args else []
    children = args.get("children", [])
    for child in children:
        _linked(conn, definition, child, points, evaluations)
    if children:
        definition["children"] = children
    if evaluations:
        e, *other = evaluations
        if any(item != e for item in other):
            raise SchemaInvalid("Register independent evaluations as separate versioned recipes")
        held, target = e["holdout_input"], e["step"]
        consuming = {k for k,s in steps.items() if {"input":held} in s["inputs"]}
        if held not in bindings or consuming != {target} or target not in steps or len(bindings) < 2:
            raise SchemaInvalid("Keep the named holdout exclusive to its separate evaluation step")
        check_holdout(e, bindings, assets)
        definition["evaluation"] = e
    decisions = {}
    for p in points:
        key = p["step"]
        if key not in steps or p["output"] not in {o["name"] for o in steps[key]["outputs"]}:
            raise SchemaInvalid("A decision must identify a named scientific output of its performed stage")
        v6_macro_recipes._question(p["question"], "decision question")
        value = dict(p, role="run_lead", event="job_settled")
        if key in decisions and value != decisions[key]:
            raise SchemaInvalid("One stage cannot bind conflicting decision questions")
        decisions[key] = value
    if decisions:
        definition["decision_points"] = list(decisions.values())


def validate_target(conn, run_id, doc, bindings, assets):
    require_current(conn, run_id)
    for child in doc.get("children", []):
        _registered(conn, child["recipe"], child["digest"], {doc["reference"]})
    if "evaluation" in doc:
        check_holdout(doc["evaluation"], bindings, assets)


def bound_ops(conn, run_id, doc, digest, args, answer):
    slots = {s["slot"]:s for s in doc["steps"]}
    bound = v6_macro_recipes._bound_nodes(conn, run_id, doc["reference"], digest, args["prefix"],
                                         args["branch"], slots, args["inputs"])
    if not bound:
        return
    aliases = {"local:"+args["prefix"]+"-"+s:n for s,n in bound.items()}
    real_branch = conn.execute("SELECT branch_id FROM v6_nodes WHERE node_id=?",(next(iter(bound.values())),)).fetchone()[0]
    remaining = []
    for op in answer["ops"]:
        if op["node"] in aliases:
            continue
        op["branch"] = real_branch
        for k in ("inputs","depends_on"):
            op[k] = [aliases.get(ref,ref) for ref in op[k]]
        if (gate := op.get("evidence_gate")) and gate["producer_node"] in aliases:
            gate["producer_node"] = aliases[gate["producer_node"]]
            gate["producer_version"] = conn.execute("SELECT version FROM v6_nodes WHERE node_id=?",(gate["producer_node"],)).fetchone()[0]
        remaining.append(op)
    answer.update(ops=remaining,bound_steps=bound)
    answer.pop("submission",None)


def verify_commission(conn, run_id, definition, spec, nodes):
    """A citation to an immutable recipe carries its actual procedure on native submission."""
    mark = spec["recipe_decision"]
    steps = {s["slot"]:s for s in definition["steps"]}
    step = steps[mark["step"]]
    parents = {}
    for nid in spec["depends_on"]:
        parent = nodes[nid].get("recipe_decision", {})
        if parent.get("recipe") != mark["recipe"] or parent.get("digest") != mark["digest"] or parent.get("step") in parents:
            raise SchemaInvalid("The adopted dependency must identify its performed recipe slot")
        parents[parent["step"]] = nid
    if set(parents) != set(step["depends_on"]) or len(spec["inputs"]) != len(step["inputs"]):
        raise SchemaInvalid("The adopted stage must use the saved dependency and input slots")
    ports = {p["index"]:p["output"] for p in spec.get("input_ports", ())}
    expected_ports = {i:p["output"] for i,p in enumerate(step["inputs"]) if "step" in p}
    if ports != expected_ports:
        raise SchemaInvalid("The adopted stage uses a different named data port")
    for i,p in enumerate(step["inputs"]):
        if ("step" in p and spec["inputs"][i] != parents[p["step"]]) or ("input" in p and spec["inputs"][i] in nodes):
            raise SchemaInvalid("Bind the stage's named materials and data ports to this Goal")
    gate = spec.get("evidence_gate")
    template = step.get("evidence_gate")
    if bool(gate) != bool(template):
        raise SchemaInvalid("An adopted stage must preserve its registered evidence filter")
    if template:
        producer = parents[template["producer_step"]]
        sources = [nodes[producer]["inputs"][i] for i,p in enumerate(steps[template["producer_step"]]["inputs"])
                   if p.get("input") == template["source_input"]]
        existing = conn.execute("SELECT version FROM v6_nodes WHERE run_id=? AND node_id=?", (run_id,producer)).fetchone()
        if not sources or len(set(sources)) != 1:
            raise SchemaInvalid("Bind the portable filter to one attributable source input")
        expected = {"producer_node":producer,"producer_version":existing[0] if existing else 1,
                    "source_ref":sources[0],**{k:template[k] for k in ("pointer","equals","require_lead_review","output_name","source_role")}}
        if ids.canonical_json(gate) != ids.canonical_json(expected):
            raise SchemaInvalid("The adopted filter must preserve the typed value, named producer output and material role")
    evaluation = definition.get("evaluation")
    if not evaluation or evaluation["step"] != step["slot"]:
        return
    if not spec.get("independent_repeat"):
        raise SchemaInvalid("A held-out evaluation requires a fresh independent Job")
    held = evaluation["holdout_input"]
    bindings = {p["input"]:spec["inputs"][i] for i,p in enumerate(step["inputs"]) if "input" in p}
    # Inspect this evaluation's connected predecessors, including disjoint material names.
    work = list(parents.values())
    seen = set()
    while work:
        parent = work.pop()
        if parent in seen:
            continue
        seen.add(parent)
        participant = nodes[parent]
        source_step = steps[participant["recipe_decision"]["step"]]
        for i,p in enumerate(source_step["inputs"]):
            if "input" in p:
                if p["input"] in bindings and bindings[p["input"]] != participant["inputs"][i]:
                    raise SchemaInvalid("The adopted stages bind one material slot to different originals")
                bindings[p["input"]] = participant["inputs"][i]
        work.extend(participant["depends_on"])
    from ..artifacts import ArtifactService
    check_holdout(evaluation, bindings, ArtifactService(conn))


def evaluation_material(conn, definition, ref):
    """Describe reuse of the original evaluation bytes without granting access to its result."""
    evaluation = definition["evaluation"]
    step = next(s for s in definition["steps"] if s["slot"] == evaluation["step"])
    index = step["inputs"].index({"input":evaluation["holdout_input"]})
    receipt = conn.execute("SELECT sources_json FROM v6_saved_recipes WHERE reference=?",
                           (definition["reference"],)).fetchone()
    sources = json.loads(receipt[0])
    if "macro" in definition:
        sources = sources["steps"]
    previous = conn.execute("""SELECT a.sha256 FROM v6_nodes n
        JOIN artifacts a ON a.artifact_ref=json_extract(n.spec_json,?) WHERE n.node_id=?""",
        (f"$.inputs[{index}]",sources[step["slot"]]["node_id"])).fetchone()
    current = conn.execute("SELECT sha256 FROM artifacts WHERE artifact_ref=?", (ref,)).fetchone()
    if not previous or not current:
        return "source_comparison_unknown"
    if previous[0] == current[0]:
        return "same_as_source_evaluation"
    return "differs_from_source_evaluation"
