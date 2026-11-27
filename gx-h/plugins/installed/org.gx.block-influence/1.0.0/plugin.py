"""Expose the full table and a cheap material- and interval-bound readout."""
METHOD = "deposited-block-influence@1"
READING = "deposited-block-reading@1"


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs) != 1:
        raise ValueError("This method analyzes one authorized input")
    if capability == METHOD:
        if set(parameters) != {"mask"} or parameters["mask"] not in ("common_ca", "common_backbone"):
            raise ValueError("Select common_backbone or common_ca")
        return
    if (set(parameters) != {"pair", "block_index"} or parameters["pair"] not in ("AB", "AC", "BC")
            or type(parameters["block_index"]) is not int or parameters["block_index"] < 0):
        raise ValueError("Supply a chain pair and a nonnegative integer block_index")
    ref, = inputs
    if ref in producers:
        if producers[ref] != METHOD:
            raise ValueError("A cell projection uses the full scientific table")
    else:
        ctx.job_output(ref, METHOD, "block-influence.json")


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return {"table" if capability == READING else "structure": refs[0]}, parameters


def register(ctx):
    for capability, file in ((METHOD, "block_influence"), (READING, "block_reading")):
        ctx.capabilities.add_manifest("methods/" + file + ".json")
        ctx.goal_methods.register(capability, validate=validate, bind=bind)
