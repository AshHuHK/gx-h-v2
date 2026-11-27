"""Research inventory of the operator's assigned source material."""


def register(ctx):
    ctx.research_modules.register(
        module_id="data_inventory", version="3", efforts=["medium", "deep"], focuses=["data"],
        purpose="Inventory the assigned local materials, their provenance, coverage and gaps.",
        output="module_report_bundle", cost_class="local_cpu",
        instructions=(
            "Inspect the assigned materials. Read the versioned method card to check its inputs, "
            "applicability, units and limitations before choosing a measurement. "
            "Cite measured outputs, describe missing information and separate observations from interpretations."
        ),
        capabilities=[],
        method_profiles=[
            {"output_nature": "analysis"},
            {"output_nature": "observed"},
            {"method_family": "bounded_json_analysis", "input_role": "spec",
             "output_kind": "file:artifact_analysis_result"},
        ],
        tools=["gx_task_context", "gx_read_artifact", "gx_query_catalog", "gx_decide_revision",
               "gx_submit_module_report"], input_roles=["*"],
    )
