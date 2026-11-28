"""Forward structure inference for documented mature, naturally occurring food proteins."""

METHOD = "natural-food-fold@1"


def validate(ctx, capability, inputs, parameters, *, producers):
    if capability != METHOD or len(inputs) != 1 or parameters:
        raise ValueError("Select one registered natural food protein specimen; no overrides.")


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return {"specimen": refs[0]}, {}


def register(ctx):
    ctx.capabilities.add_manifest("methods/forward.json")
    ctx.goal_methods.register(METHOD, validate=validate, bind=bind)
