"""Register the capability described by this immutable analytical method package."""
def register(ctx):
    ctx.capabilities.add_manifest("method.json")
