"""A scoped research role for a question whose method and outcome are still open."""


def register(ctx):
    ctx.research_modules.register(
        module_id="investigation",
        version="1",
        efforts=["medium", "deep"],
        focuses=["workflow", "evaluation"],
        purpose="Choose and execute measurements that distinguish explanations of the assigned question.",
        output="module_report_bundle",
        cost_class="local_cpu",
        instructions=(
            "Use the assigned question, material and method cards to choose a discriminating "
            "measurement. Respect the Lead's adopted prediction and its scope when running a "
            "test of that prediction. Report the measured result, its limits, and the action "
            "it warrants. A missing result, a counterexample or a reason to stop can be the "
            "most informative outcome. Cite the published evidence another researcher needs."
        ),
        input_roles=["*"],
        capabilities=[],
        method_profiles=[{"output_nature": "analysis"}, {"output_nature": "observed"}],
        tools=[
            "gx_task_context",
            "gx_read_artifact",
            "gx_query_catalog",
            "gx_decide_revision",
            "gx_submit_module_report",
        ],
    )
