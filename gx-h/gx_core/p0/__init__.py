"""GX durable science operations on the official Codex App Server."""

from pathlib import Path

# Published workflows hash callbacks by qualified name and module bytes.
# Release adapters keep those import names when their files move to the plugin.
_release_adapters = Path(__file__).resolve().parents[2] / "plugins" / "compat"
__path__.extend(str(directory) for directory in sorted(_release_adapters.glob("*/native")))
