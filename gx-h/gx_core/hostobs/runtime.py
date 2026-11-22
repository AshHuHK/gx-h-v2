"""Native GX session adapters. Each adapter runs a bounded session on the GX store."""


def runtime_adapter(name="codex_app_server"):
    if name == "codex_app_server":
        from . import app_server_adapter
        return app_server_adapter
    if name == "codex_source_host":
        from . import source_host_adapter
        return source_host_adapter
    raise ValueError(f"unknown GX runtime backend {name!r}")
