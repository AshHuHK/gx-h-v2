"""Bind the two original publication tables in either order, using GX's material hashes."""
from .methods.retrospective import METHOD, TABLES


def bind(ctx, capability, refs, parameters):
    if capability != METHOD or parameters or len(refs) != 2:
        raise ValueError("Provide the published S1 kinetics and S3 conservation tables together, with no parameters.")
    by_digest = {ctx.describe(ref)["sha256"]: ref for ref in refs}
    if set(by_digest) != set(TABLES.values()):
        raise ValueError("The two required originals are Carlin et al. S1 Table and S3 Table.")
    return {role: by_digest[digest] for role, digest in TABLES.items()}, {}


def validate(ctx, capability, inputs, parameters, *, producers):
    bind(ctx, capability, inputs, parameters)


def register(ctx):
    ctx.capabilities.add_manifest("methods/retrospective.json")
    ctx.goal_methods.register(METHOD, validate=validate, bind=bind)
