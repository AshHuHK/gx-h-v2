"""Offer a local inventory focus; JobService runs the methods selected by the researcher."""


def register(ctx):
    ctx.research_modules.register(
        module_id="data_inventory", version="1", efforts=["medium", "deep"], focuses=["data"],
        purpose="Inventory the assigned local materials, their provenance, coverage and gaps.",
        output="module_report_bundle", cost_class="local_cpu",
        instructions=(
            "Use the assigned materials and select methods applicable to their exact source versions. "
            "Cite each finding, mark coverage limits and gaps, and identify suitable follow-up work. "
            "For the deposited coordinates, tl1a-coordinate-quality@1 is restricted to the released "
            "2RE9 source. deposited-chain-geometry@1 accepts either registered deposition, one per "
            "Job, and can report coverage for a named atom selection. Describe that selection explicitly."
        ),
        capabilities=["artifact-analysis@1", "tl1a-coordinate-quality@1", "deposited-chain-geometry@1"],
        tools=["gx_task_context", "gx_read_artifact", "gx_query_catalog", "gx_decide_revision",
               "gx_submit_module_report"], input_roles=["*"],
    )
