"""Expose the versioned coordinate analysis through the normal Goal method catalog."""


def register(ctx):
    ctx.capabilities.add_manifest("methods/deposited_geometry.json")
