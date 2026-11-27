"""Bindings for archived, natural S288C annotation and bibliographic coverage."""
from .archive import QUERY, RECORDS, canonical_query

LOOKUP = "yeast-archive-lookup@1"
COMPARE = "yeast-archive-compare@1"


def bindings(ctx, capability, inputs, parameters, producers):
    required = {"query"} if capability == LOOKUP else {"query", "prior_records"}
    if parameters or len(inputs) != len(required):
        raise ValueError("Use the query material; comparison also requires parent-records.json. Parameters are empty.")
    roles = {}
    for ref in inputs:
        if ref in producers:
            if producers[ref] != LOOKUP:
                raise ValueError("Expected the archive lookup as producer of parent-records.json")
            role = "prior_records"
        else:
            data = ctx.read_json(ref)
            schema = data.get("schema") if isinstance(data, dict) else None
            if schema == QUERY:
                canonical_query(data)
                role = "query"
            elif schema == RECORDS:
                role = "prior_records"
            else:
                raise ValueError("Expected a gx.yeast-archive-query/1 query or the prior parent-records.json")
        roles[role] = ref
    if set(roles) != required:
        raise ValueError("Provide one new query and the prior records for comparison, or a single query for lookup.")
    return roles


def validate(ctx, capability, inputs, parameters, *, producers):
    bindings(ctx, capability, inputs, parameters, producers)


def bind(ctx, capability, inputs, parameters):
    return bindings(ctx, capability, inputs, parameters, {}), parameters


def register(ctx):
    for cap, manifest in ((LOOKUP,"methods/lookup.json"), (COMPARE,"methods/compare.json")):
        ctx.capabilities.add_manifest(manifest)
        ctx.goal_methods.register(cap, validate=validate, bind=bind)
