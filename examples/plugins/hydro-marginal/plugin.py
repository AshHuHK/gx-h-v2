"""Publish the analytical method through the standard Goal capability adapter."""


def register(ctx):
    ctx.capabilities.add_manifest("method.json")
