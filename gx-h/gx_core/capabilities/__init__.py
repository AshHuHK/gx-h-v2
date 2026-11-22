"""Capabilities and environments: what is callable, on what evidence, and what it is.

    environments   registered from a probe the backend runs; readiness is an observation
    manifest       the capability manifest bundle: validation and the launch it resolves to
    registry       candidates, ``resolve`` (ACTIVE only), ``method_card``, ``equivalence``
    probe          one reading of the GPU and the preflight a GPU job must pass
    checks         the eight adapter checks of ``contracts/gates.json``
    tools          what this package publishes through ``gx_core.registry``

Other streams call ``gx_core.capabilities.registry.resolve``, ``.method_card`` and
``.equivalence``. Nothing is imported here, so importing one module of the package does
not load the others; ``tools`` is loaded by ``gx_core.registry.load_providers``.
"""
