"""Native researchers work from separate grants on the same installed package."""


def register(ctx):
    report_tools = ["gx_task_context", "gx_read_artifact", "gx_submit_module_report"]
    ctx.research_modules.register(
        module_id="method_workflow_survey", version="1",
        purpose="Shortlist applicable models, workflows or evaluation resources from public specifications.",
        output="module_report_bundle", cost_class="local_read",
        efforts=["medium", "deep"], focuses=["model", "workflow", "evaluation"],
        input_roles=["model_card", "workflow_spec", "evaluation_spec", "method_spec"],
        instructions=ctx.read_file("survey.md").decode("utf-8").strip(),
        catalog_access="public_methods", capabilities=[], tools=[*report_tools, "gx_query_catalog"],
    )
    ctx.research_modules.register(
        module_id="offline_literature_mechanism", version="1",
        purpose="Trace proposed mechanisms, competing explanations and gaps to passages in registered local sources.",
        output="module_report_bundle", cost_class="local_read",
        efforts=["deep"], focuses=["literature", "mechanism"],
        input_roles=["literature", "source", "test_fixture", "source_evidence", "source_review"],
        instructions=ctx.read_file("literature.md").decode("utf-8").strip(),
        capabilities=[], tools=[*report_tools, "gx_read_source", "gx_extract_evidence", "gx_review_evidence"],
    )
    ctx.research_modules.register(
        module_id="hypothesis_space", version="1",
        purpose="Build falsifiable alternatives and identify measurements that distinguish their predictions.",
        output="module_report_bundle", cost_class="local_read",
        efforts=["deep"], focuses=["hypothesis"],
        input_roles=["module_report", "source_evidence", "source_review", "observation",
                     "analysis", "model_prediction", "job_output"],
        instructions=ctx.read_file("hypotheses.md").decode("utf-8").strip(),
        published_inputs_only=True, capabilities=[], tools=report_tools,
    )
