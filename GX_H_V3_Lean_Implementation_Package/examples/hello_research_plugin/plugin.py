from .package.workflow import HelloResearchWorkflow

def register(ctx):
    ctx.workflows.register(
        workflow_id="hello_research",
        version="1",
        hooks=HelloResearchWorkflow(ctx),
    )
