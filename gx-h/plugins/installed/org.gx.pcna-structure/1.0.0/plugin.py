"""Bind original-coordinate measurements to a deposited P12004 trimer."""
from .methods import geometry_metrics, interface_known

GEOMETRY = geometry_metrics.METHOD
INTERFACE = "pcna-interface@1"


def validate(ctx, capability, inputs, parameters, *, producers):
    if len(inputs)!=1:
        raise ValueError("Supply one original deposited mmCIF as structure")
    if capability==GEOMETRY:
        geometry_metrics.validate(parameters)
    elif capability==INTERFACE:
        if (not parameters.keys()<={"pair","block_index"} or parameters.get("pair","12") not in ("12","23","31")
                or type(parameters.get("block_index",-1)) is not int or parameters.get("block_index",-1)<-1):
            raise ValueError("Select pair 12, 23, or 31 and integer block_index >= -1")
    else:
        raise ValueError("Unknown PCNA method")


def bind(ctx, capability, refs, parameters):
    validate(ctx,capability,refs,parameters,producers={})
    defaults = {"pair":"12","block_index":-1}
    return {"structure":refs[0]}, {**defaults,**parameters}


def register(ctx):
    for capability, filename, known in ((GEOMETRY,"geometry",geometry_metrics.sufficient_metric),
                                        (INTERFACE,"interface",interface_known.sufficient_metric)):
        ctx.capabilities.add_manifest("methods/"+filename+".json")
        ctx.goal_methods.register(capability,validate=validate,bind=bind,sufficient_metric=known)
