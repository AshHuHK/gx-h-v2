"""Connect bibliographic discovery, selected originals and community leads to the Goal runtime."""
from .methods.scholarly import pmc_id, query_text, selected, validate_request

SEARCH = "scholarly-search@1"
FETCH = "scholarly-fetch@1"
SOCIAL = "social-source-intake@1"


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs) != 1:
        raise ValueError("Supply exactly one scholarly source input")
    ref, = inputs
    if capability == SEARCH:
        if set(parameters) != {"query"}:
            raise ValueError("Supply one free-text query")
        query = query_text(parameters["query"])
        if ref in producers:
            if producers[ref] != SEARCH:
                raise ValueError("Replay requires search-snapshot.json from scholarly-search@1")
        else:
            validate_request(ctx.read_json(ref), query)
    elif capability == FETCH:
        if set(parameters) != {"pmcid"}:
            raise ValueError("Supply one open JATS PMCID from candidates.json")
        identifier = pmc_id(parameters["pmcid"])
        if ref in producers:
            if producers[ref] != SEARCH:
                raise ValueError("Fetch requires search candidates from scholarly-search@1")
        else:
            ctx.job_output(ref, SEARCH, "candidates.json")
            selected(ctx.read_json(ref), identifier)
    elif capability == SOCIAL:
        if parameters or ref in producers:
            raise ValueError("Supply one PI-provided community link or export with no parameters")
        from .methods.social import social_intake
        social_intake(ctx.read_bytes(ref))
    else:
        raise ValueError("Unknown scholarly method")


def bind(ctx, capability, refs, parameters):
    validate(ctx, capability, refs, parameters, producers={})
    return {"source": refs[0]}, parameters


def register(ctx):
    for method, name in ((SEARCH, "search"), (FETCH, "fetch"), (SOCIAL, "social")):
        ctx.capabilities.add_manifest(f"methods/{name}.json")
        ctx.goal_methods.register(method, validate=validate, bind=bind)
    ctx.research_modules.register(
        module_id="scholarly_investigation", version="1",
        purpose="Search scholarly databases, acquire an open original, and assess what its actual passages support.",
        output="module_report_bundle", cost_class="local_cpu",
        efforts=["deep"], focuses=["literature"], input_roles=["*"],
        capabilities=[SEARCH, FETCH],
        instructions=(
            "Choose and refine your own search terms for the assigned question. Look at the source-specific "
            "return status and choose a relevant open primary article from candidates.json. Fetch it using "
            "its PMCID and read the actual assigned JATS article with gx_read_source. Record the locator "
            "of methods, measurements, qualifications and counterevidence used in your reasoning. Distinguish "
            "a catalogue annotation or an abstract from a direct measurement under specified conditions. "
            "Use the delivered artifact references in your report and explain which next comparison would "
            "change your interpretation. When full text is unavailable, state that gap or choose another source."
        ),
        tools=["gx_task_context", "gx_read_artifact", "gx_query_catalog", "gx_decide_revision",
               "gx_read_source", "gx_extract_evidence", "gx_review_evidence", "gx_submit_module_report"],
    )
