"""Native worker presentation attached to an installed static workflow version."""

from functools import lru_cache
import json

from .. import config
from . import packages


PREFIX = "GX task brief\n"


class NativeWorker:
    role_tools = False
    schema_page_kind = None

    def check_start(self, spec):
        pass

    def snapshot(self, conn, run_id):
        return {}

    def assign(self, run, packet):
        """Optionally return a renderer to run after the Root has its artifact grants."""

    def assigned(self, binding):
        return None

    def validate_submission(self, binding, product):
        return None

    def guidance(self, brief):
        return brief

    def resume_text(self, brief):
        return brief


DEFAULT = NativeWorker()


@lru_cache(maxsize=8)
def _workers(directory):
    # This section belongs to the installed release. New operator-selected
    # packages use their Run composition and do not change these bindings.
    manifest = json.loads((directory / "workflow_packages.json").read_text(encoding="utf-8"))
    return manifest.get("native_workers", {})


def for_workflow(workflow_id, version):
    if not workflow_id:
        return DEFAULT
    name = _workers(config.CONFIG_DIR).get(f"{workflow_id}@{version}")
    return packages.component(name) if name else DEFAULT


def for_run(run):
    return for_workflow(run["workflow_id"], run["workflow_version"])
