"""Goal callbacks for the published 2RE9 coordinate methods."""
import hashlib
import json

from . import comparison

QUALITY = "tl1a-coordinate-quality@1"
GEOMETRY = "tl1a-geometry@1"
REVIEW = "tl1a-review@1"
LIMIT = "Deposited 2RE9 model 1, entity 1, chains A/B/C, one experimental source."


# Every digest gets its own module namespace and verified registration bytes.
def bind_release(read_file):
    global CRITERIA_BYTES, EVALUATOR_SHA, ADAPTER_SHA
    CRITERIA_BYTES = read_file("criteria.json")
    EVALUATOR_SHA = hashlib.sha256(read_file("comparison.py")).hexdigest()
    ADAPTER_SHA = hashlib.sha256(read_file("goal_methods.py")).hexdigest()


THRESHOLDS = {
    "coverage": {"min_residues": {"type": "integer", "minimum": 1},
                 "min_fraction": {"type": "number", "exclusiveMinimum": 0, "maximum": 1}},
    "comparison": {k: {"type": "number", "minimum": 0}
                   for k in ("max_angle_delta_deg", "max_rmsd_delta_angstrom")},
    "reproduction": {k: {"type": "number", "minimum": 0}
                     for k in ("angle_abs_tol_deg", "rmsd_abs_tol_angstrom")},
}
CRITERIA_SCHEMA = {"type": "object", "properties": {
    k: {"type": "object", "properties": fields, "additionalProperties": False}
    for k, fields in THRESHOLDS.items()}, "additionalProperties": False}

REEVALUATE_TOOL = {
    "type": "function", "name": "gx_reevaluate_geometry",
    "description": "Compare actual geometric measurements under revised coverage and comparison thresholds "
                   "without running another fit. Returns a retrospective evaluation and original evidence IDs.",
    "inputSchema": {"type": "object", "properties": {
        "measurement_refs": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                             "maxItems": len(comparison.PROTOCOLS), "uniqueItems": True},
        "criteria": CRITERIA_SCHEMA}, "required": ["measurement_refs", "criteria"], "additionalProperties": False},
}


def _criteria(changes, original=None):
    if not isinstance(changes, dict) or not set(changes) <= THRESHOLDS.keys():
        raise ValueError("Only coverage, comparison and reproduction thresholds can be revised")
    result = dict(original) if original is not None else json.loads(CRITERIA_BYTES)
    for section, values in changes.items():
        if not isinstance(values, dict) or not set(values) <= THRESHOLDS[section].keys():
            raise ValueError(f"Unsupported threshold in {section}")
        result[section] = {**result[section], **values}
    comparison.validate_criteria(result)
    return result


def describe(capability):
    if capability == QUALITY:
        return {"inputs": ["deposited mmCIF artifact"], "parameters": {},
                "outputs": ["quality.json"], "method": "Count observed atoms, residues and selection coverage.",
                "limitations": LIMIT}
    if capability == GEOMETRY:
        criteria = _criteria({})
        return {"inputs": ["deposited mmCIF artifact", "optional coordinate inventory Job output"],
                "parameters": {"protocol": list(comparison.PROTOCOLS), "criteria": CRITERIA_SCHEMA},
                "criteria_defaults": {k: criteria[k] for k in THRESHOLDS},
                "protocol_definitions": criteria["protocols"],
                "outputs": ["geometry.json"],
                "method": "Select one protocol. Fit three chain pairs by proper rigid superposition and report "
                          "coverage, RMSD in angstroms and unsigned angle in degrees. Optional criteria "
                          "override individual default thresholds. Input roles are positional. To change only the "
                          "interpretation of existing measurements, call gx_reevaluate_geometry with their output IDs.",
                "limitations": LIMIT}
    if capability == REVIEW:
        return {"inputs": ["deposited mmCIF artifact",
                           "one or more geometry Job outputs with distinct protocols and the same frozen criteria"],
                "parameters": {}, "outputs": ["checks.json"],
                "method": "Independently read the coordinates and repeat the fits using Biopython and NumPy SVD. "
                          "Compare against the original results and tolerances.",
                "limitations": LIMIT + " The numerical implementation is independent; the deposition is shared."}
    raise ValueError(f"No protein adapter for {capability}")


def observation_view(value):
    """Compact the inventory's atom-ID arrays for the Lead; numeric pointers keep their original paths."""
    if (not isinstance(value, dict) or value.get("schema") != "gx.tl1a.coordinate_quality/1"
            or value.get("method") != QUALITY or not isinstance(value.get("protocol_masks"), dict)):
        return value
    masks = value["protocol_masks"]
    return {**value, "protocol_masks": {name: {k: v for k, v in mask.items() if k != "selected_keys"}
                                        for name, mask in masks.items()},
            "view": {"omitted_pointers": [f"/protocol_masks/{name}/selected_keys" for name in masks
                                          if "selected_keys" in masks[name]],
                     "read_original_with": "gx_read_artifact"}}


def validate_shape(capability, inputs, parameters):
    if not isinstance(inputs, list) or not inputs or any(not isinstance(x, str) or not x for x in inputs):
        raise ValueError("Supply ordered artifact or dependency references, starting with the structure")
    if len(set(inputs)) != len(inputs) or not isinstance(parameters, dict):
        raise ValueError("Supply distinct inputs and a parameter object")
    if capability == QUALITY and len(inputs) == 1 and not parameters:
        return
    if capability == GEOMETRY and len(inputs) in (1, 2) and set(parameters) <= {"protocol", "criteria"}:
        if parameters.get("protocol") not in comparison.PROTOCOLS:
            raise ValueError("Select one documented atom-selection protocol")
        _criteria(parameters.get("criteria", {}))
        return
    if capability == REVIEW and 2 <= len(inputs) <= 1 + len(comparison.PROTOCOLS) and not parameters:
        return
    raise ValueError(f"Ordered inputs or parameters do not match {capability}")


def _output_row(ctx, ref, capability):
    return ctx.job_output(ref, capability, describe(capability)["outputs"][0])


def _job_output(ctx, ref, capability):
    row, frozen = _output_row(ctx, ref, capability)
    return row, ctx.read_bytes(ref, expected_sha256=row["sha256"]), frozen


def _check_structure(ctx, ref):
    if ctx.describe(ref)["sha256"] != comparison.SOURCE_SHA:
        raise ValueError("These released methods require this run's granted 2RE9 deposition")


def validate_inputs(ctx, capability, inputs, *, producers):
    """Check granted artifact roles and the declared outputs of not-yet-run dependencies."""
    structure = inputs[0]
    if structure in producers:
        raise ValueError("The structure role requires an already granted, pinned raw mmCIF")
    _check_structure(ctx, structure)
    expected = QUALITY if capability == GEOMETRY else GEOMETRY
    for ref in inputs[1:]:
        if ref in producers:
            if producers[ref] != expected:
                raise ValueError(f"Expected an output of {expected} for this position")
        else:
            _, frozen = _output_row(ctx, ref, expected)
            if frozen["structure"]["artifact_ref"] != structure:
                raise ValueError("The source Job did not measure the supplied structure")


def _measurement(ctx, ref):
    row, raw, frozen = _job_output(ctx, ref, GEOMETRY)
    observed = json.loads(raw)
    criteria = ctx.read_bytes(frozen["criteria"]["artifact_ref"],
                                expected_sha256=frozen["criteria"]["sha256"])
    if (observed.get("schema") != "gx.tl1a.geometry/1" or observed.get("method") != GEOMETRY or
            observed.get("input_sha256") != frozen["structure"]["sha256"] or
            observed.get("criteria_sha256") != frozen["criteria"]["sha256"] or
            observed.get("criteria_spec_sha256") != comparison.object_digest(json.loads(criteria))):
        raise ValueError("The coordinates or evaluation specification differ from the original Job")
    return row, raw, observed, frozen


def _review_inputs(ctx, structure, refs):
    routes = {}
    criteria_ref = None
    for ref in refs:
        row, raw, observed, frozen = _measurement(ctx, ref)
        if frozen["structure"]["artifact_ref"] != structure:
            raise ValueError("Review requires measurements of the supplied coordinates")
        if criteria_ref and criteria_ref != frozen["criteria"]["artifact_ref"]:
            raise ValueError("Review each frozen evaluation specification separately")
        criteria_ref = frozen["criteria"]["artifact_ref"]
        protocol = observed["protocol"]
        if protocol in routes:
            raise ValueError("Supply one measurement per protocol")
        routes[protocol] = {"observation_job_id": row["job_id"], "observations_ref": ref,
                            "observations_sha256": row["sha256"], "measurement_ref": ref,
                            "measurement_sha256": row["sha256"], "measurement_json": raw.decode()}
    source_set = ctx.publish_json("geometry_review", "review_sources",
                                 {"measurement_refs": refs})
    batch = {"schema": "gx.tl1a.review_inputs/1", "evidence_ref": source_set, "routes": routes}
    batch_ref = ctx.publish_json("geometry_review", "geometry_review_inputs", batch)
    return {"structure": structure, "criteria": criteria_ref, "observations": batch_ref}, {}


def prepare_inputs(ctx, capability, inputs, parameters):
    """Resolve typed scientific roles after the graph owner resolves dependencies to actual artifacts."""
    validate_shape(capability, inputs, parameters)
    structure = inputs[0]
    _check_structure(ctx, structure)
    if capability == QUALITY:
        return {"structure": structure}, {}
    if capability == REVIEW:
        return _review_inputs(ctx, structure, inputs[1:])
    if len(inputs) == 2:
        _, _, frozen = _job_output(ctx, inputs[1], QUALITY)
        if frozen["structure"]["artifact_ref"] != structure:
            raise ValueError("The inventory did not measure the supplied coordinates")
    criteria = _criteria(parameters.get("criteria", {}))
    criteria_ref = ctx.publish_json("geometry_criteria", "geometry_criteria", criteria)
    return {"structure": structure, "criteria": criteria_ref,
            "selection": inputs[1] if len(inputs) == 2 else criteria_ref}, {"protocol": parameters["protocol"]}


def _evaluate(ctx, measurement_refs, criteria):
    """Compute a retrospective reading of the original Jobs, outside the writer."""
    if (not isinstance(measurement_refs, list) or not 1 <= len(measurement_refs) <= len(comparison.PROTOCOLS)
            or not all(isinstance(ref, str) for ref in measurement_refs)
            or len(set(measurement_refs)) != len(measurement_refs)):
        raise ValueError("Supply one to three distinct geometry Job output references")
    projections, sources = [], []
    previous = None
    for ref in measurement_refs:
        row, _, observed, frozen = _measurement(ctx, ref)
        original = ctx.read_json(frozen["criteria"]["artifact_ref"])
        revised = _criteria(criteria, original)
        if previous and previous != revised:
            raise ValueError("Supply a common target specification for all measurements")
        previous = revised
        coverage, mapping = revised["coverage"], observed["mapping"]
        eligible = (len(observed["pairs"]) == len(revised["mapping"]["pairs_fixed_moving"])
                    and mapping["selected_residues"] >= coverage["min_residues"]
                    and mapping["selected_fraction_of_observed_union_residues"] >= coverage["min_fraction"])
        projections.append({**observed, "status": "measured" if eligible else "insufficient"})
        sources.append({"artifact_ref": ref, "sha256": row["sha256"], "job_id": row["job_id"],
                        "structure_ref": frozen["structure"]["artifact_ref"],
                        "source_sha256": frozen["structure"]["sha256"], "protocol": observed["protocol"],
                        "executed_criteria_ref": frozen["criteria"]["artifact_ref"]})
    if len({p["protocol"] for p in projections}) != len(projections):
        raise ValueError("Remeasuring one protocol cannot supply two different selections")
    criteria_ref = ctx.publish_json("geometry_criteria", "geometry_criteria", previous)
    result = {"schema": "gx.v6.geometry_reevaluation/1", "evaluation_timing": "retrospective",
              "method": GEOMETRY, "evaluator": "compare_protocols", "evaluator_sha256": EVALUATOR_SHA,
              "adapter_sha256": ADAPTER_SHA,
              "criteria_ref": criteria_ref, "criteria_sha256": comparison.object_digest(previous),
              "measurements": sources, "comparison": comparison.compare_protocols(projections, previous)}
    return result


def validate(ctx, capability, inputs, parameters, *, producers):
    validate_shape(capability, inputs, parameters)
    validate_inputs(ctx, capability, inputs, producers=producers)


def reevaluate(ctx, arguments):
    if set(arguments) != {"measurement_refs", "criteria"}:
        raise ValueError("Provide the measurement_refs and revised criteria")
    refs = arguments["measurement_refs"]
    result = _evaluate(ctx, refs, arguments["criteria"])
    return ctx.evaluation(result, refs, folder="geometry_reevaluation", kind="geometry_reevaluation")


def review_dependencies(capability, python_executable, *, probe):
    return probe(python_executable, {"biopython": "==1.88", "numpy": "==1.26.4"},
                 source_digest_key="review_source_sha256")
