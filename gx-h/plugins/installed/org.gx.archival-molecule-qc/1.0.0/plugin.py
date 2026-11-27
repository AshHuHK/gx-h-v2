"""Bind archived structural coverage measurements to the artifacts selected by the Lead."""

ROLES = {
    "archival-peptide-coverage@1": ("structure",),
    "archival-fab-coverage@1": ("numbering", "structure"),
}


def validate(ctx, capability, inputs, parameters, *, producers):
    roles = ROLES[capability]
    if len(inputs) != len(roles) or len(set(inputs)) != len(roles) or parameters:
        raise ValueError("Supply distinct files in this order: " + ", ".join(roles) + ". No parameters.")


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return dict(zip(ROLES[capability], refs, strict=True)), {}


def register(ctx):
    for method in ROLES:
        ctx.capabilities.add_manifest("methods/" + method.split("@")[0] + ".json")
        ctx.goal_methods.register(method, validate=validate, bind=bind)
