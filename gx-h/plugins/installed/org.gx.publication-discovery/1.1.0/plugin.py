"""Goal bindings for question-first publication discovery and its named downstream outputs."""
import re

QUERY = "literature-query@2"
PAGE = "literature-page@2"
ORIGINAL = "literature-original@2"
PAGE_METHODS = (QUERY, PAGE, "scholarly-page-search@2", "literature-query@1", "literature-page@1", "scholarly-page-search@1")


def require_output(ctx, ref, producers, name):
    if ref in producers:
        if producers[ref] not in PAGE_METHODS:
            raise ValueError(f"Bind {name} from a publication-search Job")
        return
    # A page can come from the first search, a continuation, or the compatible legacy reader.
    for method in PAGE_METHODS:
        try:
            ctx.job_output(ref, method, name)
            return
        except Exception as error:
            if getattr(error, "code", None) != "SCHEMA_INVALID":
                raise
    raise ValueError(f"Use the verified {name} from a successful publication-search Job in this Run")


def validate(ctx, method, inputs, parameters, *, producers):
    if method == QUERY:
        query = parameters.get("query")
        if inputs or set(parameters) != {"query"} or not isinstance(query, str) or not query.strip() or len(query) > 600:
            raise ValueError("Start a fresh search with inputs=[] and one nonempty query (at most 600 characters)")
        return
    if len(inputs) != 1:
        raise ValueError("Supply exactly one named output of the earlier search Job")
    ref, = inputs
    if method == PAGE:
        if set(parameters) != {"action"} or parameters["action"] not in ("next", "replay"):
            raise ValueError("Choose action next or replay; the original query is read from the snapshot")
        require_output(ctx, ref, producers, "search-snapshot.json")
    elif method == ORIGINAL:
        pmcid = parameters.get("pmcid")
        if set(parameters) != {"pmcid"} or not isinstance(pmcid, str) or not re.fullmatch(r"PMC[1-9][0-9]{0,11}", pmcid, re.I):
            raise ValueError("Choose one open PMCID listed in candidates.json")
        require_output(ctx, ref, producers, "candidates.json")
        if ref not in producers and not any(pmcid.upper() in row["fetchable_pmcids"] for row in ctx.read_json(ref)["candidates"]):
            raise ValueError("The selected PMCID is not listed as open JATS on this page")
    else:
        raise ValueError("Unknown publication-discovery method")


def bind(ctx, method, refs, parameters):
    validate(ctx, method, refs, parameters, producers={})
    if method == QUERY:
        return {}, parameters
    return {"snapshot" if method == PAGE else "candidates": refs[0]}, parameters


def register(ctx):
    for ref in (QUERY, PAGE, ORIGINAL):
        ctx.capabilities.add_manifest("methods/" + ref.split("@")[0] + ".json")
        ctx.goal_methods.register(ref, validate=validate, bind=bind)
    ctx.research_modules.register(
        module_id="publication_discovery", version="2", cost_class="local_cpu",
        purpose="Search public literature directly from a scientific question, with or without supplied articles, and inspect original conditions.",
        output="module_report_bundle", efforts=["deep"], focuses=["literature"], input_roles=["*"],
        capabilities=[QUERY, PAGE, ORIGINAL],
        instructions=("Start a new literature search with no artifact inputs and a scientific query. "
                      "Inspect coverage and candidate identifiers. Use named snapshots to continue or replay, "
                      "and select open JATS originals from their candidate pages. Report evidence with the "
                      "original passage, conditions, uncertainties and relevance to the current question."),
        tools=["gx_task_context", "gx_read_artifact", "gx_query_catalog", "gx_decide_revision",
               "gx_read_source", "gx_extract_evidence", "gx_review_evidence", "gx_submit_module_report"],
    )
