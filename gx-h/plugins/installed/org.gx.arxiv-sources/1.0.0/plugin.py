"""Optional arXiv methods for the GX literature researcher and scientific Lead."""
from .methods import arxiv

SEARCH = "arxiv-search@1"
FETCH = "arxiv-fetch@1"


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs) != 1:
        raise ValueError("Supply one search request, snapshot or candidate set")
    ref, = inputs
    if capability == SEARCH:
        if set(parameters) != {"query"}:
            raise ValueError("Provide the arXiv query")
        query = arxiv.query_text(parameters["query"])
        if ref not in producers:
            arxiv.validate_request(ctx.read_json(ref), query)
        elif producers[ref] != SEARCH:
            raise ValueError("Replay a search-snapshot.json from arxiv-search@1")
    elif capability == FETCH:
        if set(parameters) != {"arxiv_id"} or not isinstance(parameters["arxiv_id"], str) or not arxiv.VERSIONED.fullmatch(parameters["arxiv_id"]):
            raise ValueError("Select one versioned arxiv_id from candidates.json")
        if ref not in producers:
            ctx.job_output(ref, SEARCH, "candidates.json")
            arxiv.select(ctx.read_json(ref), parameters["arxiv_id"])
        elif producers[ref] != SEARCH:
            raise ValueError("Use the candidate output of arxiv-search@1")
    else:
        raise ValueError("Unknown arXiv capability")


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return {"source": refs[0]}, parameters


def register(ctx):
    for capability, method in ((SEARCH, "search"), (FETCH, "fetch")):
        ctx.capabilities.add_manifest(f"methods/{method}.json")
        ctx.goal_methods.register(capability, validate=validate, bind=bind)
    ctx.research_modules.register(
        module_id="arxiv_investigation", version="1", purpose="Find applicable public preprints and read the selected version.",
        output="module_report_bundle", cost_class="local_cpu", efforts=["deep"], focuses=["literature"],
        input_roles=["*"], capabilities=[SEARCH, FETCH],
        instructions="Choose and refine an arXiv query. Read a version-bound PDF through gx_read_source. Cite the page, distinguish preprint claims from measured results, and record missing methods, data or figures.",
        tools=["gx_task_context", "gx_read_artifact", "gx_query_catalog", "gx_decide_revision",
               "gx_read_source", "gx_extract_evidence", "gx_review_evidence", "gx_submit_module_report"])
