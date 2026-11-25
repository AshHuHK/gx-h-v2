"""The native planner's presentation of the frozen science_grid@3 decision."""
import json

from .. import schema
from ..domain import ids
from ..errors import SchemaInvalid


VERSION = "science_grid@3/native-planner/1"
FIELD = "native_product_transport"
GRID = {
    "type": "object", "required": ["method_id", "method_version", "synthetic", "candidates"],
    "properties": {
        "method_id": {"type": "string", "enum": ["fixture-grid"]},
        "method_version": {"type": "string", "enum": ["1"]},
        "synthetic": {"type": "boolean", "enum": [True]},
        "candidates": {"type": "array", "minItems": 1, "items": {
            "type": "object", "required": ["candidate_id", "x"], "properties": {
                "candidate_id": {"type": "string"}, "x": {"type": "number", "description": "Finite coordinate."}}}},
    },
}
GRID_GUIDANCE = ('Each next_grids[path_id] and each pivot.grid is the complete fixture-grid@1 input object. '
                 'Set method_id="fixture-grid", method_version="1", synthetic=true and candidates to a '
                 'nonempty list of objects with candidate_id and finite numeric x.')
EVIDENCE_ROLE = ("Cite the assigned evidence packets or their routes' input_ref, observations_ref or "
                 "evaluation_ref that you actually compared. Cite the independent review separately in "
                 "the decision's assessment_ref and the supplement in this entry's artifact_ref.")


def bind(packet):
    """Freeze the transport with the newly admitted attempt, keeping the workflow assignment's digest."""
    inputs = packet["input"]
    if (packet["role"] != "planner" or inputs.get("output_kind") != "science_decision"
            or not (inputs.get("max_rounds", 1) > 1 or inputs.get("supplement_sources"))):
        return
    base = inputs["product_contract"]
    properties = dict(base["schema"]["properties"])
    properties["next_grids"] = {"type": "object", "description": GRID_GUIDANCE, "additionalProperties": GRID}
    properties["pivots"] = {"type": "array", "items": {"type": "object", "properties": {
        "from_path": {"type": "string"}, "id": {"type": "string"}, "prediction": {"type": "string"},
        "grid": GRID}}}
    if inputs.get("supplement_sources"):
        properties["source_decisions"] = {"type": "array", "items": {"type": "object", "properties": {
            "source_id": {"type": "string"}, "artifact_ref": {"type": "string"},
            "disposition": {"type": "string"}, "reason": {"type": "string"},
            "evidence_refs": {"type": "array", "description": EVIDENCE_ROLE, "items": {"type": "string"}}}}}
    descriptor = {"version": VERSION, "workflow_contract_digest": base["digest"],
                  "schema": {**base["schema"], "properties": properties}}
    # The full schema is part of the immutable native brief. Only one copy goes on the wire.
    packet["input"] = {**inputs, "product_contract": {"digest": base["digest"]}}
    packet[FIELD] = {**descriptor, "digest": ids.sha256_of_obj(descriptor)}


def assigned(binding):
    """Earlier Roots resume the presentation under which they were admitted."""
    brief = binding["brief"]
    if binding["role"] == "planner" and '"' + FIELD + '":' in brief:
        _, marker, body = brief.partition("GX task brief\n")
        if marker:
            return json.loads(body).get(FIELD)
    return None


def validate_grids(product, descriptor):
    from workflows.science_grid.nodes import _validate_grid

    grid_schema = descriptor["schema"]["properties"]["next_grids"]["additionalProperties"]
    point_schema = grid_schema["properties"]["candidates"]["items"]

    def check(grid, pointer):
        try:
            _validate_grid(grid)
        except SchemaInvalid as exc:
            raise SchemaInvalid(GRID_GUIDANCE, details={"category": "product_structure",
                "contract_digest": descriptor["digest"], "pointer": pointer}) from exc
        for index, point in enumerate(grid["candidates"]):
            issues = schema.validate(point, point_schema)
            if issues:
                raise SchemaInvalid(issues[0], details={"category": "product_structure",
                    "contract_digest": descriptor["digest"], "pointer": f"{pointer}/candidates/{index}"})

    # The workflow still validates the top-level decision and every scientific choice.
    grids = product.get("next_grids")
    if isinstance(grids, dict):
        for path_id, grid in grids.items():
            escaped = path_id.replace("~", "~0").replace("/", "~1")
            check(grid, "/next_grids/" + escaped)
    pivots = product.get("pivots")
    if isinstance(pivots, list):
        for index, pivot in enumerate(pivots):
            if isinstance(pivot, dict):
                check(pivot.get("grid"), f"/pivots/{index}/grid")
