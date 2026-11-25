"""Bind the three released 2RE9 methods to ordered inputs chosen by a Goal-only Lead."""
from pathlib import Path
import json

from adapters.scientific.tl1a_geometry import review, worker
from ..artifacts import ArtifactService
from ..capabilities import environments, shipped
from ..domain import ids
from ..errors import EnvNotReady, SchemaInvalid, ScopeDenied
from ..persistence import store
from ..plugins import selection
from . import manifest, science


PLUGIN = "org.gx.protein@0.1.0-v6.1"
QUALITY, GEOMETRY, REVIEW = shipped.QUALITY, shipped.GEOMETRY, shipped.REVIEW
LIMIT = "Deposited 2RE9 model 1, entity 1, chains A/B/C, one experimental source."
CRITERIA_PATH = Path(worker.__file__).parent / "data" / "criteria.json"
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
                             "maxItems": len(worker.PROTOCOLS), "uniqueItems": True},
        "criteria": CRITERIA_SCHEMA}, "required": ["measurement_refs", "criteria"], "additionalProperties": False},
}


def supported(capability):
    return capability in (QUALITY, GEOMETRY, REVIEW)


def runtime_dependencies(capability, python_executable):
    """Probe the actual review interpreter against its release requirements before matching an old fit."""
    if capability != REVIEW:
        return {}
    requirements = {}
    for requirement in (CRITERIA_PATH.parent.parent / "requirements-review.txt").read_text().splitlines():
        if requirement.strip():
            name, version = requirement.split("==")
            requirements[name] = "==" + version
    probe = environments.probe({"env_id": "protein-review", "kind": "SYSTEM_BINARY",
                                "python_executable": python_executable, "requirements": requirements})
    if probe["status"] != "HEALTHY":
        raise EnvNotReady("Install the released coordinate reviewer dependencies into the Job interpreter",
                          details={"python_executable": python_executable, "reasons": probe["reasons"]})
    return {**{k: v["installed"] for k, v in probe["requirements"].items()},
            "review_source_sha256": ids.sha256_of_file(review.__file__)}


def _criteria(changes, original=None):
    if not isinstance(changes, dict) or not set(changes) <= THRESHOLDS.keys():
        raise SchemaInvalid("Only coverage, comparison and reproduction thresholds can be revised")
    result = dict(original) if original is not None else json.loads(CRITERIA_PATH.read_bytes())
    for section, values in changes.items():
        if not isinstance(values, dict) or not set(values) <= THRESHOLDS[section].keys():
            raise SchemaInvalid(f"Unsupported threshold in {section}")
        result[section] = {**result[section], **values}
    try:
        worker.validate_criteria(result)
    except (ValueError, TypeError, OverflowError) as exc:
        raise SchemaInvalid(str(exc)) from exc
    return result


def describe(capability):
    if capability == QUALITY:
        return {"inputs": ["deposited mmCIF artifact"], "parameters": {},
                "outputs": ["quality.json"], "method": "Count observed atoms, residues and selection coverage.",
                "limitations": LIMIT}
    if capability == GEOMETRY:
        criteria = _criteria({})
        return {"inputs": ["deposited mmCIF artifact", "optional coordinate inventory Job output"],
                "parameters": {"protocol": list(worker.PROTOCOLS), "criteria": CRITERIA_SCHEMA},
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
    raise SchemaInvalid(f"No protein adapter for {capability}")


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


def catalog(ctx, plugin_ref=PLUGIN):
    """Use the installed loader and the same immutable method pins as static workflows."""
    package_selection = selection.select([plugin_ref], "gx.goal", "1")
    rows = []
    for plugin in package_selection.plugins:
        package_pin = {"id": plugin.pin.id, "version": plugin.pin.version, "sha256": plugin.pin.digest}
        for capability in plugin.contributions.capabilities:
            receipt = shipped.attach(ctx, capability, package=plugin)
            rows.append({**receipt, **describe(capability), "plugin": package_pin})
    return rows


def validate(capability, inputs, parameters):
    if not isinstance(inputs, list) or not inputs or any(not isinstance(x, str) or not x for x in inputs):
        raise SchemaInvalid("Supply ordered artifact or dependency references, starting with the structure")
    if len(set(inputs)) != len(inputs) or not isinstance(parameters, dict):
        raise SchemaInvalid("Supply distinct inputs and a parameter object")
    if capability == QUALITY and len(inputs) == 1 and not parameters:
        return
    if capability == GEOMETRY and len(inputs) in (1, 2) and set(parameters) <= {"protocol", "criteria"}:
        if parameters.get("protocol") not in worker.PROTOCOLS:
            raise SchemaInvalid("Select one documented atom-selection protocol")
        _criteria(parameters.get("criteria", {}))
        return
    if capability == REVIEW and 2 <= len(inputs) <= 1 + len(worker.PROTOCOLS) and not parameters:
        return
    raise SchemaInvalid(f"Ordered inputs or parameters do not match {capability}")


def _output_row(conn, run_id, ref, capability):
    assets = ArtifactService(conn)
    row = assets.describe(ref)
    if row["run_id"] != run_id or not row["job_id"]:
        raise SchemaInvalid("Use an original Job output from this run")
    # Manifests and bytes are immutable. Read them outside any writer transaction.
    receipt = manifest.read_manifest(conn, row["job_id"])
    admitted, completed = receipt["admission"], receipt["completion"]
    method = admitted["capability"]
    if (admitted["run_id"] != run_id or f"{method['id']}@{method['version']}" != capability or
            not completed or completed["status"] != "SUCCEEDED" or row["origin"] != "JOB_OUTPUT"):
        raise SchemaInvalid(f"Expected a successful {capability} Job in the current run")
    output = next((item for item in completed["outputs"] if item["artifact_ref"] == ref), None)
    output_name = describe(capability)["outputs"][0]
    if (not output or output.get("output_name") != output_name or output["sha256"] != row["sha256"] or
            output.get("runner_sha256", output["sha256"]) != row["sha256"]):
        raise SchemaInvalid("The artifact differs from the scientific output in its Job receipt")
    return row, {item["role"]: item for item in admitted["inputs"]}


def _job_output(conn, run_id, ref, capability):
    row, frozen = _output_row(conn, run_id, ref, capability)
    raw = ArtifactService(conn).read_bytes(ref, expected_sha256=row["sha256"])
    return row, raw, frozen


def _check_structure(conn, run_id, ref):
    source = ArtifactService(conn).describe(ref)
    if source["run_id"] != run_id or source["sha256"] != worker.SOURCE_SHA:
        raise SchemaInvalid("These released methods require this run's granted 2RE9 deposition")


def validate_inputs(conn, run_id, capability, inputs, *, producers):
    """Check granted artifact roles and the declared outputs of not-yet-run dependencies."""
    structure = inputs[0]
    if structure in producers:
        raise SchemaInvalid("The structure role requires an already granted, pinned raw mmCIF")
    _check_structure(conn, run_id, structure)
    expected = QUALITY if capability == GEOMETRY else GEOMETRY
    for ref in inputs[1:]:
        if ref in producers:
            if producers[ref] != expected:
                raise SchemaInvalid(f"Expected an output of {expected} for this position")
        else:
            _, frozen = _output_row(conn, run_id, ref, expected)
            if frozen["structure"]["artifact_ref"] != structure:
                raise SchemaInvalid("The source Job did not measure the supplied structure")



def _measurement(conn, run_id, ref):
    row, raw, frozen = _job_output(conn, run_id, ref, GEOMETRY)
    observed = json.loads(raw)
    criteria = ArtifactService(conn).read_bytes(frozen["criteria"]["artifact_ref"],
                                              expected_sha256=frozen["criteria"]["sha256"])
    if (observed.get("schema") != "gx.tl1a.geometry/1" or observed.get("method") != GEOMETRY or
            observed.get("input_sha256") != frozen["structure"]["sha256"] or
            observed.get("criteria_sha256") != frozen["criteria"]["sha256"] or
            observed.get("criteria_spec_sha256") != ids.sha256_of_obj(json.loads(criteria))):
        raise SchemaInvalid("The coordinates or evaluation specification differ from the original Job")
    return row, raw, observed, frozen


def _review_inputs(conn, run_id, structure, refs):
    routes = {}
    criteria_ref = None
    for ref in refs:
        row, raw, observed, frozen = _measurement(conn, run_id, ref)
        if frozen["structure"]["artifact_ref"] != structure:
            raise SchemaInvalid("Review requires measurements of the supplied coordinates")
        if criteria_ref and criteria_ref != frozen["criteria"]["artifact_ref"]:
            raise SchemaInvalid("Review each frozen evaluation specification separately")
        criteria_ref = frozen["criteria"]["artifact_ref"]
        protocol = observed["protocol"]
        if protocol in routes:
            raise SchemaInvalid("Supply one measurement per protocol")
        routes[protocol] = {"observation_job_id": row["job_id"], "observations_ref": ref,
                            "observations_sha256": row["sha256"], "measurement_ref": ref,
                            "measurement_sha256": row["sha256"], "measurement_json": raw.decode()}
    source_set = science._publish(conn, run_id, "geometry_review", "review_sources",
                                 {"measurement_refs": refs})
    batch = {"schema": "gx.tl1a.review_inputs/1", "evidence_ref": source_set, "routes": routes}
    batch_ref = science._publish(conn, run_id, "geometry_review", "geometry_review_inputs", batch)
    return {"structure": structure, "criteria": criteria_ref, "observations": batch_ref}, {}


def prepare_inputs(conn, run_id, capability, inputs, parameters):
    """Resolve typed scientific roles after the graph owner resolves dependencies to actual artifacts."""
    validate(capability, inputs, parameters)
    structure = inputs[0]
    _check_structure(conn, run_id, structure)
    if capability == QUALITY:
        return {"structure": structure}, {}
    if capability == REVIEW:
        return _review_inputs(conn, run_id, structure, inputs[1:])
    if len(inputs) == 2:
        _, _, frozen = _job_output(conn, run_id, inputs[1], QUALITY)
        if frozen["structure"]["artifact_ref"] != structure:
            raise SchemaInvalid("The inventory did not measure the supplied coordinates")
    criteria = _criteria(parameters.get("criteria", {}))
    criteria_ref = science._publish(conn, run_id, "geometry_criteria", "geometry_criteria", criteria)
    return {"structure": structure, "criteria": criteria_ref,
            "selection": inputs[1] if len(inputs) == 2 else criteria_ref}, {"protocol": parameters["protocol"]}


def _evaluate(conn, run_id, measurement_refs, criteria):
    """Compute a retrospective reading of the original Jobs, outside the writer."""
    if (not isinstance(measurement_refs, list) or not 1 <= len(measurement_refs) <= len(worker.PROTOCOLS)
            or not all(isinstance(ref, str) for ref in measurement_refs)
            or len(set(measurement_refs)) != len(measurement_refs)):
        raise SchemaInvalid("Supply one to three distinct geometry Job output references")
    projections, sources = [], []
    previous = None
    for ref in measurement_refs:
        row, _, observed, frozen = _measurement(conn, run_id, ref)
        original = ArtifactService(conn).read_json(frozen["criteria"]["artifact_ref"])
        revised = _criteria(criteria, original)
        if previous and previous != revised:
            raise SchemaInvalid("Supply a common target specification for all measurements")
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
        raise SchemaInvalid("Remeasuring one protocol cannot supply two different selections")
    criteria_ref = science._publish(conn, run_id, "geometry_criteria", "geometry_criteria", previous)
    result = {"schema": "gx.v6.geometry_reevaluation/1", "evaluation_timing": "retrospective",
              "method": GEOMETRY, "evaluator": "compare_protocols", "evaluator_sha256": ids.sha256_of_file(review.__file__),
              "adapter_sha256": ids.sha256_of_file(__file__),
              "criteria_ref": criteria_ref, "criteria_sha256": ids.sha256_of_obj(previous),
              "measurements": sources, "comparison": review.compare_protocols(projections, previous)}
    return result


def reevaluate(conn, run_id, measurement_refs, criteria, *, task_id, lease_id, context_ref, thread_id, turn_id, call_id):
    """Publish one authenticated evaluation of in-scope outputs, with no new coordinate Jobs."""
    from .v6_claims import active_context, lookup_replay

    identity = {"run_id": run_id, "task_id": task_id, "lease_id": lease_id, "context_ref": context_ref,
                "thread_id": thread_id, "turn_id": turn_id, "call_id": call_id}
    if not all(isinstance(s, str) and s for s in identity.values()):
        raise ScopeDenied("A native evaluation requires a bound call, turn, task and lease")
    request = {**identity, "measurement_refs": measurement_refs, "criteria": criteria}
    key = "v6-evaluate:" + ids.sha256_of_obj([thread_id, call_id])
    replay = lookup_replay(conn, key, request)
    if replay is not None:
        return replay

    def authority():
        task = active_context(conn, run_id, task_id, lease_id, context_ref, thread_id, turn_id)
        visible = set(json.loads(task["visible_refs_json"]))
        if (not isinstance(measurement_refs, list) or not all(isinstance(s, str) and s in visible
                                                               for s in measurement_refs)):
            raise ScopeDenied("Evaluate original outputs presented in this decision context")

    authority()
    result = _evaluate(conn, run_id, measurement_refs, criteria)
    # Bytes have no authority as decision evidence until the scoped event is committed.
    ref = science._publish(conn, run_id, "geometry_reevaluation", "geometry_reevaluation", result)
    with store.transaction(conn, transaction_owner="v6.protein", operation="record_reevaluation", run_id=run_id):
        idem = store.begin_idempotent(conn, key, "v6.reevaluate_geometry", request)
        if idem.replay:
            return idem.response
        authority()
        store.append_event(conn, run_id=run_id, kind="v6.geometry.reevaluated", actor=thread_id,
                           subject_kind="task", subject_id=task_id, evidence_level="EXECUTION_OBSERVED",
                           payload={**identity, "evaluation_ref": ref, "criteria_ref": result["criteria_ref"],
                                    "source_refs": measurement_refs})
        answer = {"evaluation_ref": ref, "evaluation": result}
        store.complete_idempotent(conn, key, answer)
    return answer
