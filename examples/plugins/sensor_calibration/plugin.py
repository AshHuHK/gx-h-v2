"""Goal contributions for the portable paired-voltage method."""


def project(value):
    # Keep summary metrics at their original JSON paths. The artifact also records error sign counts.
    return {name: value[name] for name in ("schema", "method", "input_sha256", "unit", "periods")}


def register(ctx):
    ctx.capabilities.add_manifest("methods/calibration.json")
    ctx.goal_methods.register("sensor-calibration@1", project=project)
