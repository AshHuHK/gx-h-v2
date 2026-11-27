"""Release-owned discovery for trusted installed Python workflow packages."""
import importlib
import json
from .. import config
from ..errors import SchemaInvalid


def installation():
    return json.loads((config.CONFIG_DIR / "workflow_packages.json").read_text(encoding="utf-8"))


def component(reference):
    module, name = reference.split(":", 1)
    return getattr(importlib.import_module(module), name)


def research_ref(spec, current=None):
    if current is not None:
        return current["workflow_id"], current["workflow_version"]
    installed = installation()
    default_id, default_version = installed["research_default"].rsplit("@", 1)
    name = spec.get("workflow", default_id)
    version = spec.get("workflow_version", installed["research_versions"].get(name))
    if version is None and name == default_id:
        version = default_version
    if version is None:
        raise SchemaInvalid(f"Specify workflow_version for research workflow {name!r}")
    return name, str(version)


def example(name):
    entry = installation()["examples"][name]
    if "file" in entry:
        return json.loads((config.HARNESS_ROOT.parent / entry["file"]).read_text(encoding="utf-8"))
    return component(entry["factory"])()


def python_example(name):
    return example(installation()["python_examples"][name])


def native_guidance(mode):
    path = installation().get("native_guidance", {}).get(mode)
    return (config.HARNESS_ROOT / path).read_text(encoding="utf-8").strip() if path else ""
