"""Register the original-coordinate directional block sensitivity method."""
import math

from .methods.signed import summarize

METHOD = "deposited-block-signed@1"


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs) != 1:
        raise ValueError("Supply one original deposition mmCIF as structure")
    if set(parameters) != {"mask", "endpoint", "effect_minimum", "zero_limit", "minimum_evaluable"}:
        raise ValueError("Supply mask, endpoint, effect_minimum, zero_limit, and minimum_evaluable")
    if parameters["mask"] not in ("common_backbone", "common_ca") or parameters["endpoint"] not in ("angle_deg", "rmsd_A"):
        raise ValueError("Choose common_backbone or common_ca and angle_deg or rmsd_A")
    e, z, k = (parameters[key] for key in ("effect_minimum", "zero_limit", "minimum_evaluable"))
    if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (e, z))
            or not 0 < z < e or type(k) is not int or k < 1):
        raise ValueError("Require 0 < zero_limit < effect_minimum and integer minimum_evaluable >= 1")


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return {"structure": refs[0]}, parameters


def sufficient_metric(parameters, metric_id, table_capability, output_name, table_document):
    """A table predates a numeric endpoint only when the registered settings yield a value."""
    if table_document.get("mask") != parameters["mask"]:
        return False
    ep = parameters["endpoint"]
    source = (table_capability, output_name, table_document.get("schema"))
    if source == ("deposited-block-influence@1", "block-influence.json", "gx.block-influence/1"):
        table = table_document
    elif source == (METHOD, "signed-blocks.json", "gx.signed-blocks/1") and table_document["endpoint"] == ep:
        # The signed details store raw fits per interval under one physical endpoint.
        # Restore the released table's baseline and interval fits to reapply this claim's e, z, k.
        # In particular, a classification computed under the producer's thresholds is insufficient.
        table = {"schema": "gx.block-influence/1", "baseline": {}, "blocks": []}
        for row in table_document["blocks"]:
            block = {"index": row["block_index"], "eligible": row["eligible"]}
            if block["eligible"]:
                before = {pair: {ep: values["before"]} for pair, values in row["pairs"].items()}
                if table["baseline"] and before != table["baseline"]:
                    return False
                table["baseline"] = before
                block["pairs"] = {pair: {ep: values["after"]} for pair, values in row["pairs"].items()}
            table["blocks"].append(block)
    else:
        return False
    result = summarize(table, ep, parameters["effect_minimum"], parameters["zero_limit"],
                       parameters["minimum_evaluable"])
    return type(result.get(metric_id)) is int


def register(ctx):
    ctx.capabilities.add_manifest("methods/signed.json")
    ctx.goal_methods.register(METHOD, validate=validate, bind=bind, sufficient_metric=sufficient_metric)
