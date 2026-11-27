"""Turn a completed analytical subgraph into portable, pinned Goal operations."""
import json
import re

from ..artifacts import ArtifactService
from ..capabilities import registry, shipped
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied, VersionImmutable
from ..persistence import store
from . import manifest, v6_jobs, v62_outputs

SAVE = "gx_save_recipe"
INSTANTIATE = "gx_instantiate_recipe"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}")


def _name(value):
    if not isinstance(value, str) or not _NAME.fullmatch(value):
        raise SchemaInvalid("Recipe names, slots and versions require a short identifier")
    return value


def _mapping(rows, key, value):
    result = {_name(row[key]): row[value] for row in rows}
    if len(result) != len(rows) or len(set(result.values())) != len(rows):
        raise SchemaInvalid("Recipe slot names and bindings must be distinct")
    return result


def _card(reference, digest, definition):
    return {"kind": "recipe", "reference": reference, "digest": digest, "title": definition["title"],
            "id_alignment": definition["id_alignment"], "missing_values": definition["missing_values"],
            "units": definition["units"], "inputs": definition["inputs"], "methods": {s["slot"]: s["capability"] for s in definition["steps"]},
            "method_pins": {s["capability"]: s["method_digest"] for s in definition["steps"]},
            "outputs": {s["slot"]: s["outputs"] for s in definition["steps"]}}


def cards(conn):
    return [_card(ref, digest, json.loads(doc)) for ref, digest, doc in
            conn.execute("SELECT reference,digest,definition_json FROM v6_saved_recipes ORDER BY reference")]


def prepare(conn, run_id, arguments):
    reference = _name(arguments["recipe_id"]) + "@" + _name(arguments["version"])
    slots = _mapping(arguments["nodes"], "slot", "node_id")
    if not slots:
        raise SchemaInvalid("Select at least one completed analytical node")
    bindings = _mapping(arguments["inputs"], "name", "artifact_ref")
    external = {ref: slot for slot, ref in bindings.items()}
    node_slots = {nid: slot for slot, nid in slots.items()}
    proofs = {}
    for slot, nid in slots.items():
        node = v6_jobs._node(conn, run_id, nid)
        if not node or node["status"] not in ("SUCCEEDED", "REUSED"):
            raise SchemaInvalid(f"Recipe step {slot} needs a completed Job on this Goal")
        spec = json.loads(node["spec_json"])
        if spec.get("evidence_gate") or set(spec["depends_on"]) - node_slots.keys():
            raise SchemaInvalid("Include the whole dependency or save a subgraph without an external condition")
        proof = manifest.read_manifest(conn, node["job_id"])
        v6_jobs.confirmed_result_identity(conn, nid, proof)
        decls = proof["admission"].get("output_declarations") or []
        if not decls or any(d.get("nature") not in {"analysis", "observed"} for d in decls):
            raise SchemaInvalid("Recipes support declared read-only analysis and observed outputs")
        proofs[nid] = (spec, node, proof, v62_outputs.recorded_outputs(
            conn, run_id, node["job_id"], record=proof, check_publications=True)[0])

    steps, sources, required = [], {}, {}
    assets = ArtifactService(conn)
    for slot, nid in sorted(slots.items()):
        spec, node, proof, outputs = proofs[nid]
        pinned = proof["admission"]["capability"]
        view = registry.candidate(conn, spec["capability"])
        if view["manifest_sha256"] != pinned["sha256"]:
            raise SchemaInvalid("The installed method differs from the one used in the source Job")
        portable = shipped.portable_digest(view["bundle"])
        ports = v6_jobs._ports(spec)
        selected = []
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
            else:
                if ref not in external:
                    raise SchemaInvalid("Name each external input of the selected subgraph")
                meta = assets.describe(ref)
                if meta["run_id"] != run_id:
                    raise ScopeDenied("Source input belongs to a different Goal")
                key = external[ref]
                required[key] = {"kind": meta["kind"], "media_type": meta["media_type"]}
                selected.append({"input": key})
        steps.append({"slot": slot, "capability": spec["capability"], "method_digest": portable,
                      "inputs": selected, "outputs": sorted(
                          ({"name": row["output_name"], "kind": row["kind"]} for row in outputs),
                          key=lambda o: o["name"]),
                      "depends_on": sorted(node_slots[dep] for dep in spec["depends_on"]),
                      "parameters": spec["parameters"], "question": spec["question"]})
        sources[slot] = {"node_id": nid, "job_id": node["job_id"], "completion_sha256": proof["manifest_hash"]}
    if required.keys() != bindings.keys():
        raise SchemaInvalid("The named inputs must match exactly the external inputs of the subgraph")
    definition = {"schema": "gx.research-recipe/1", "reference": reference,
                  "inputs": required, "steps": steps}
    for key in ("title", "units", "id_alignment", "missing_values"):
        if not isinstance(arguments[key], str) or not arguments[key].strip():
            raise SchemaInvalid(f"A reusable recipe needs {key}")
        definition[key] = arguments[key]
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


def instantiate(conn, run_id, arguments):
    reference = arguments["recipe"]
    row = conn.execute("SELECT digest,definition_json FROM v6_saved_recipes WHERE reference=?", (reference,)).fetchone()
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
    grants = json.loads(conn.execute("SELECT capabilities_json FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()[0])
    prefix = _name(arguments["prefix"])
    alias = {s["slot"]: f"local:{prefix}-{s['slot']}" for s in definition["steps"]}
    ops = []
    for step in definition["steps"]:
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
        ops.append({"op": "add_node", "node": alias[step["slot"]], "branch": arguments["branch"],
                    "capability": cap, "parameters": step["parameters"], "inputs": inputs,
                    "depends_on": [alias[d] for d in step["depends_on"]], "question": step["question"],
                    **({"input_ports": ports} if ports else {})})
    return {"recipe": reference, "digest": row[0], "ops": ops}


def authorize(conn, identity, tool, args):
    from . import v6_decisions
    _, task, _, _ = v6_decisions.authority(conn, **{k: v for k, v in identity.items() if k != "call_id"})
    if task["scope"] != "run":
        raise ScopeDenied("Only the Goal Lead can publish or instantiate a workspace recipe")
    visible = set(json.loads(task["visible_refs_json"]))
    if tool == SAVE:
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
