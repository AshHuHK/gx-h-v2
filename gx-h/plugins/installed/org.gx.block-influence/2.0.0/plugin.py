"""A single committed segment comparison on the original coordinates and position map."""
METHOD = "deposited-block-influence@2"


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs) != 2 or inputs[0] == inputs[1]:
        raise ValueError("Use the original structure followed by its frozen position map")
    if (set(parameters) != {"mask", "interval", "endpoint"}
            or parameters["mask"] not in ("common_ca", "common_backbone")
            or parameters["endpoint"] not in ("angle_deg", "rmsd_A")
            or parameters["interval"] not in ("21-40", "101-120")):
        raise ValueError("Choose mask, registered reference interval and one geometry endpoint")


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return {"structure": refs[0], "mapping": refs[1]}, parameters


def register(ctx):
    ctx.capabilities.add_manifest("methods/segment.json")
    ctx.goal_methods.register(METHOD, validate=validate, bind=bind)
