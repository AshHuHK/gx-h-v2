"""Goal bridge for archived calibration inference and mapped coordinate agreement."""

ROLES = {"archived-fold-calibration@1": ("specimen",),
         "mapped-structure-agreement@1": ("prediction", "reference", "mapping")}


def validate(ctx, capability, inputs, parameters, *, producers):
    roles = ROLES[capability]
    if len(inputs) != len(roles) or parameters:
        raise ValueError("Ordered inputs required: " + ", ".join(roles) + ". No parameters.")


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return dict(zip(ROLES[capability], refs, strict=True)), {}


def register(ctx):
    for method in ROLES:
        ctx.capabilities.add_manifest("methods/" + method.split("@")[0] + ".json")
        ctx.goal_methods.register(method, validate=validate, bind=bind)
