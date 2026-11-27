def register(ctx):
    ctx.research_modules.register(
        module_id="unit_notation_check", version="1", efforts=["deep"], focuses=["units"],
        purpose="Check how a public example reports measurement units.", output="module_report_bundle",
        cost_class="local_read", capabilities=[], input_roles=["test_fixture"],
        tools=["gx_task_context", "gx_read_artifact", "gx_submit_module_report"],
        instructions="Read the assigned fixture and cite the text that supplies the unit. Report ambiguous units as a gap.",
    )
