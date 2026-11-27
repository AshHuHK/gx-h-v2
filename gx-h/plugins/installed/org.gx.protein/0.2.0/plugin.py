"""Register released CPU methods and their Goal-specific input and result handling."""
from . import goal_methods as methods


def register(ctx):
    methods.bind_release(ctx.read_file)
    for capability in (methods.QUALITY, methods.GEOMETRY, methods.REVIEW):
        ctx.capabilities.add_manifest(capability)
        ctx.goal_methods.register(
            capability, describe=methods.describe, validate=methods.validate, bind=methods.prepare_inputs,
            project=methods.observation_view if capability == methods.QUALITY else None,
            dependencies=methods.review_dependencies if capability == methods.REVIEW else None,
        )
    descriptor = methods.REEVALUATE_TOOL
    ctx.goal_tools.register(name=descriptor["name"], capability=methods.GEOMETRY,
                            schema=descriptor["inputSchema"], description=descriptor["description"],
                            handler=methods.reevaluate)
    ctx.capabilities.add_manifest("methods/deposited_geometry.json")
