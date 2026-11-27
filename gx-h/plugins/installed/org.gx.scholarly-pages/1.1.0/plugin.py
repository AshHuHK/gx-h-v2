"""Bind page continuation to the same query and the prior provider response."""
import re

from .methods.windows import validate_search

SEARCH = "scholarly-page-search@2"
FETCH = "scholarly-page-fetch@2"
SEARCH_VERSIONS = (SEARCH, "scholarly-page-search@1")


def search_output(ctx, ref, name):
    for capability in SEARCH_VERSIONS:
        try:
            return ctx.job_output(ref, capability, name)
        except Exception as exc:
            if getattr(exc, "code", None) != "SCHEMA_INVALID":
                raise
    raise ValueError(f"Supply the verified {name} from a scholarly page search in this Run")


def validate(ctx, method, inputs, parameters, *, producers):
    if len(inputs) != 1:
        raise ValueError("Supply one request, previous search-snapshot.json or candidates.json")
    ref, = inputs
    if method == SEARCH:
        if set(parameters) != {"query", "action"} or parameters["action"] not in ("first", "next", "replay"):
            raise ValueError("Expected query and action: first, next, or replay")
        if ref in producers:
            if producers[ref] not in SEARCH_VERSIONS or parameters["action"] == "first":
                raise ValueError("Continue from this search method's named search-snapshot.json")
        else:
            if parameters["action"] == "next":
                search_output(ctx, ref, "search-snapshot.json")
            validate_search(ctx.read_json(ref), parameters["query"], parameters["action"])
    elif method == FETCH:
        if set(parameters) != {"pmcid"} or not isinstance(parameters["pmcid"], str) or not re.fullmatch(
                r"PMC[1-9][0-9]{0,11}", parameters["pmcid"], re.I):
            raise ValueError("Choose a PMCID from the current candidates.json")
        if ref in producers:
            if producers[ref] not in SEARCH_VERSIONS:
                raise ValueError("Use the candidates.json output of a scholarly page search")
        else:
            search_output(ctx, ref, "candidates.json")
            items = ctx.read_json(ref)["candidates"]
            if not any(parameters["pmcid"].upper() in item["fetchable_pmcids"] for item in items):
                raise ValueError("This page does not list the selected accession as open JATS")
    else:
        raise ValueError("Unknown literature method")


def bind(ctx, method, refs, parameters):
    validate(ctx, method, refs, parameters, producers={})
    return {"source": refs[0]}, parameters


def register(ctx):
    for name, file in ((SEARCH, "search"), (FETCH, "fetch")):
        ctx.capabilities.add_manifest(f"methods/{file}.json")
        ctx.goal_methods.register(name, validate=validate, bind=bind)
    ctx.research_modules.register(
        module_id="scholarly_page_investigation", version="2", cost_class="local_cpu",
        purpose="Trace publications across provider result pages and read the original conditions.",
        output="module_report_bundle", efforts=["deep"], focuses=["literature"], input_roles=["*"],
        capabilities=[SEARCH, FETCH],
        instructions=(
            "Choose a question and read as deeply as its importance warrants. Inspect each provider's "
            "coverage separately. Follow an available cursor when an unresolved comparison would benefit; "
            "cite the pages you inspected. Share the original passage, conditions, sample, denominator, "
            "limitations and its effect on the next action. A shared DOI can identify one paper across "
            "providers. Different papers can share samples. Record unknown independence explicitly."
        ),
        tools=["gx_task_context", "gx_read_artifact", "gx_query_catalog", "gx_decide_revision",
               "gx_read_source", "gx_extract_evidence", "gx_review_evidence", "gx_submit_module_report"],
    )
