"""Expose bounded yeast hydrolysis evidence search and original-record acquisition to the Lead."""
from .methods.evidence import SUBSTRATES, check_scope, check_selection

INDEX = "yeast-reaction-index@1"
FETCH = "yeast-uniprot-record@1"
MCP = "yeast-enzyme-mcp@1"


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs) != 1:
        raise ValueError("Supply one yeast lookup scope or one candidates.json reference")
    ref, = inputs
    if capability == INDEX:
        if set(parameters) != {"substrate"} or parameters["substrate"] not in SUBSTRATES:
            raise ValueError("Select substrate trehalose or sucrose")
        check_scope(ctx.read_json(ref))
    else:
        if set(parameters) != {"accession"} or not isinstance(parameters["accession"], str):
            raise ValueError("Supply one accession selected from the query result")
        if ref in producers:
            if producers[ref] != INDEX:
                raise ValueError("Record acquisition requires the yeast reaction index output")
        else:
            ctx.job_output(ref, INDEX, "candidates.json")
            check_selection(ctx.read_json(ref), parameters["accession"])


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return {"scope" if capability == INDEX else "candidates": refs[0]}, parameters


def register(ctx):
    for name, filename in ((INDEX, "index"), (FETCH, "uniprot"), (MCP, "mcp")):
        ctx.capabilities.add_manifest("methods/" + filename + ".json")
        ctx.goal_methods.register(name, validate=validate, bind=bind)
