"""Register discovery and acquisition as ordinary run-scoped Goal methods."""
from .methods.archive import CANDIDATES, accession, authorize_entry, cross_references, request_ids

SEARCH = "pcna-archive-search@1"
FETCH = "pcna-archive-fetch@1"


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs) != 1:
        raise ValueError("Supply one archive input")
    if capability == FETCH:
        if set(parameters) != {"entry_id"}:
            raise ValueError("Supply the selected PDB entry_id")
        entry = accession(parameters["entry_id"])
    elif parameters:
        raise ValueError("Search uses the human P12004 archive input without parameters")
    ref, = inputs
    if ref in producers:
        if capability != FETCH or producers[ref] != SEARCH:
            raise ValueError("Fetch accepts candidates produced by the PCNA archive search")
        return
    archive = ctx.read_json(ref)
    if capability == FETCH:
        if archive.get("schema") == CANDIDATES:
            ctx.job_output(ref, SEARCH, "candidates.json")
        authorize_entry(archive, entry)
    elif "primaryAccession" in archive:
        cross_references(archive)
    else:
        request_ids(archive)


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return {"archive": refs[0]}, parameters


def register(ctx):
    for method, path in ((SEARCH, "methods/search.json"), (FETCH, "methods/fetch.json")):
        ctx.capabilities.add_manifest(path)
        ctx.goal_methods.register(method, validate=validate, bind=bind)
