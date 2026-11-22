from .package.tools import greet


def register(ctx):
    ctx.tools.register(
        name="hello_research",
        description="Greet a researcher by name.",
        schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        handler=greet,
    )
