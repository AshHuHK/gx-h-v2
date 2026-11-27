"""Two analysis methods sharing a single scoped original or a labeled derived control."""


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs) != 1 or parameters:
        raise ValueError("Supply exactly one mmCIF material and no parameters")


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return {"structure": refs[0]}, {}


def register(ctx):
    for mode in ("backbone", "macrocycle"):
        ctx.capabilities.add_manifest("methods/" + mode + ".json")
        ctx.goal_methods.register("deposited-" + mode + "-qc@1", validate=validate, bind=bind)
