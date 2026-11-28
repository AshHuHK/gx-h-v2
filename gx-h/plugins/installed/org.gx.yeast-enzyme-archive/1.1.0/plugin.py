"""Literal S288C archive lookup for an ordinary Goal without material attachments."""
METHOD = "yeast-archive-lookup@2"
SUBSTRATES = ("maltose", "isomaltose", "trehalose", "lactose")


def validate(ctx, method, inputs, parameters, *, producers):
    if inputs or set(parameters) != {"taxid", "substrate"}:
        raise ValueError("Use inputs=[] and parameters taxid (integer) and substrate (one archived sugar).")
    if type(parameters["taxid"]) is not int or parameters["taxid"] != 559292:
        raise ValueError("This archive covers S. cerevisiae S288C, NCBI taxid 559292.")
    if parameters["substrate"] not in SUBSTRATES:
        raise ValueError("Supported archive substrates: " + ", ".join(SUBSTRATES))


def bind(ctx, method, refs, parameters):
    validate(ctx, method, refs, parameters, producers={})
    return {}, parameters


def register(ctx):
    ctx.capabilities.add_manifest("methods/lookup.json")
    ctx.goal_methods.register(METHOD, validate=validate, bind=bind)
