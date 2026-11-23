"""Bind the path contract to authorized, immutable GX records."""
from functools import lru_cache
import json

from gx_core import schema
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.orchestration.engine import module_sources
from gx_core.p0 import manifest, science, science_inputs
from gx_core.persistence import store
from gx_core.teams.service import MAX_INPUT_BYTES
from . import contract


DECISION_INSTRUCTION = (
    "Read the assessment and its original evidence. Decide which paths to continue, pivot, prune or stop, "
    "with a scientific reason grounded in the measured results. Submit proposal_ref, assessment_ref, "
    "continue_paths, next_grids, reason and stop_reason. Each continued path needs a fresh candidate grid. "
    "Optional pivots have from_path, id, prediction and grid. Supply a stop_reason when closing the study."
)
DECISION_SCHEMA = {
    "type": "object",
    "required": ["proposal_ref", "assessment_ref", "continue_paths", "reason"],
    "properties": {
        "proposal_ref": {"type": "string"}, "assessment_ref": {"type": "string"},
        "continue_paths": {"type": "array", "items": {"type": "string"}},
        "next_grids": {"type": "object"}, "reason": {"type": "string"},
        "stop_reason": {"type": ["string", "null"]},
        "pivots": {"type": "array", "items": {"type": "object"}},
        "source_decisions": {"type": "array", "items": {"type": "object"}},
        "intent_responses": {"type": "array", "items": {"type": "object"}},
    },
}


def semantic(message):
    raise SchemaInvalid(message, details={"category": "product_semantic"})


@lru_cache(maxsize=1)
def validator_digest():
    return module_sources(contract.SEMANTIC_VALIDATORS)[contract.__name__]


def verify_evaluation(conn, run, proposal, route):
    """The evaluation and original measurements must belong to this exact Job, path and criterion."""
    if route.get("outcome_kind") != "valid_scientific_outcome":
        return
    row = conn.execute("SELECT * FROM evaluations WHERE eval_id=? AND run_id=? AND path_id=? AND purpose='CANDIDATE'",
                       (route.get("eval_id"), run, route["path_id"])).fetchone()
    pin = proposal["evaluator"]
    if not row or (row["eval_spec_id"], row["eval_spec_version"], row["frozen_sha"]) != (
            pin["id"], pin["version"], pin["spec_hash"]):
        semantic("The candidate evaluation differs from the frozen path or evaluation specification.")
    origin = store.loads(row, "provenance_json")
    if (store.loads(row, "artifact_refs_json") != [route["observations_ref"], route["evaluation_ref"]]
            or (origin.get("job_id"), origin.get("input_ref")) != (route["job_id"], route["observations_ref"])):
        semantic("The evaluation cites a different Job or observation artifact.")
    job = manifest.read_manifest(conn, route["job_id"])
    admission, completion = job["admission"], job["completion"]
    if (admission["run_id"] != run or not completion or completion["status"] != "SUCCEEDED"
            or not any(r["artifact_ref"] == route["input_ref"] and r["role"] == "grid" for r in admission["inputs"])
            or not any(o["artifact_ref"] == route["observations_ref"] for o in completion["outputs"])
            or job["manifest_hash"] != origin.get("manifest_hash")):
        semantic("The candidate evaluation does not match the original Job manifest, grid or observations.")



def frozen(conn, run, inputs):
    assets = ArtifactService(conn)
    digests, sizes, data, deferred = {}, {}, {}, set()
    remaining_bytes = contract.MAX_INLINE_BYTES

    def read(ref, *, optional_large=False, metadata_only=False):
        nonlocal remaining_bytes
        if ref not in sizes:
            meta = assets.describe(ref)
            if meta["run_id"] != run:
                semantic("The grid assignment contains an artifact from a different run.")
            digests[ref], sizes[ref] = meta["sha256"], meta["bytes"]
            if metadata_only:
                return None
            if optional_large and sizes[ref] > remaining_bytes:
                deferred.add(ref)
            else:
                if optional_large:
                    remaining_bytes -= sizes[ref]
                raw = assets.read_bytes(ref, expected_sha256=digests[ref])
                try:
                    data[ref] = json.loads(raw)
                except (ValueError, UnicodeDecodeError) as exc:
                    raise SchemaInvalid("The frozen artifact contains invalid JSON.", details={"artifact_ref": ref}) from exc
        return data.get(ref)

    proposal_ref = inputs["proposal_ref"]
    proposal = read(proposal_ref)
    evidence = {ref: read(ref) for ref in inputs["evidence_refs"]}
    for item in evidence.values():
        for route in item["routes"]:
            verify_evaluation(conn, run, proposal, route)
            for key in ("input_ref", "observations_ref", "evaluation_ref"):
                if ref := route.get(key):
                    read(ref, optional_large=True)
    for item in inputs.get("supplement_sources", ()):
        read(item["artifact_ref"], metadata_only=True)
    requires_interpretation = proposal.get("requires_interpretation", False) or any(
        path.get("claim_scope") != "finite_grid_threshold" for path in proposal["paths"])
    try:
        return contract.ProductContract.freeze(
            proposal_ref, proposal, evidence, data, artifact_digests=digests, artifact_sizes=sizes, deferred_refs=deferred,
            validator_digest=validator_digest(), supplement_sources=inputs.get("supplement_sources", ()),
            intent_items=inputs.get("intent_items", {}), requires_interpretation=requires_interpretation)
    except contract.SemanticError as exc:
        semantic(str(exc))


def for_review(conn, run, inputs):
    scoped = science_inputs.with_context(conn, run, {
        "p0": True, "round": inputs["round"], "max_rounds": inputs["max_rounds"],
        "output_kind": "science_assessment", "proposal_ref": inputs["proposal_ref"],
        "evidence_refs": inputs["evidence_refs"],
    })
    rules = frozen(conn, run, scoped)
    descriptor = {"digest": rules.digest, "schema": rules.schema()}
    scoped.update(instruction=rules.brief(), product_contract=descriptor)
    if len(ids.canonical_json(scoped).encode()) > MAX_INPUT_BYTES:
        ref = science._publish(conn, run, "contracts", "grid_product_schema", descriptor["schema"])
        scoped.update(product_contract={"digest": rules.digest, "schema_ref": ref},
                      instruction="Interpret the frozen measurements for each path. The referenced schema lists "
                                  "the assigned model fields, source judgments and intent responses.")
    return scoped, rules


def for_decision(conn, run, inputs, assessment_ref):
    scoped = science_inputs.with_context(conn, run, {
        "p0": True, "round": inputs["round"], "max_rounds": inputs["max_rounds"],
        "output_kind": "science_decision", "proposal_ref": inputs["proposal_ref"],
        "evidence_refs": inputs["evidence_refs"], "assessment_ref": assessment_ref,
        "instruction": DECISION_INSTRUCTION,
    })
    digest = ids.sha256_of_obj(["science_grid@3/decision", DECISION_SCHEMA, scoped["proposal_ref"],
                                scoped["evidence_refs"], assessment_ref, scoped["supplement_sources"], scoped["intent_items"]])
    scoped["product_contract"] = {"digest": digest, "schema": DECISION_SCHEMA}
    return scoped


def prepare_native_product(conn, task, product):
    inputs = task["input"]
    pin = inputs["product_contract"]["digest"]
    if inputs["output_kind"] == "science_decision":
        failures = schema.validate(product, DECISION_SCHEMA)
        if failures:
            raise SchemaInvalid(failures[0], details={"category": "product_structure", "contract_digest": pin})
        return product
    descriptor = inputs["product_contract"]
    assigned_schema = descriptor.get("schema")
    if assigned_schema is None:
        assigned_schema = ArtifactService(conn).read_json(descriptor["schema_ref"])
    try:
        contract.validate_model_schema(product, assigned_schema, pin)
        rules = frozen(conn, task["run_id"], inputs)
        if pin != rules.digest or assigned_schema != rules.schema():
            semantic("The frozen assignment differs from its product contract.")
        return rules.evaluate(product)
    except contract.StructuralError as exc:
        raise SchemaInvalid(str(exc), details={"category": "product_structure", "contract_digest": pin,
                                                "pointer": exc.pointer}) from exc
    except contract.SemanticError as exc:
        semantic(str(exc))


def context(conn, run, task_id, attempt):
    result = science_inputs.task_context(conn, run, task_id)
    row = conn.execute("SELECT input_json FROM tasks WHERE run_id=? AND task_id=?", (run, task_id)).fetchone()
    inputs = store.loads(row, "input_json")
    if inputs.get("output_kind") == "science_assessment":
        result["instruction"] = inputs["instruction"]
    result["grid_measurements"] = {key: inputs[key] for key in ("proposal_ref", "evidence_refs", "assessment_ref")
                                   if key in inputs}
    return result
