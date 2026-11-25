"""Bundle the three coordinate methods already shipped with GX."""


def register(ctx):
    ctx.capabilities.add_manifest("tl1a-coordinate-quality@1")
    ctx.capabilities.add_manifest("tl1a-geometry@1")
    ctx.capabilities.add_manifest("tl1a-review@1")
