"""Bind operator-selected contributions to one Run's existing workflow and method owners."""
from collections import OrderedDict
from dataclasses import dataclass
from functools import lru_cache
import hashlib
import importlib.util
import json
from pathlib import Path
from threading import RLock

from .. import config
from ..domain import ids
from ..errors import SchemaInvalid, VersionImmutable
from ..orchestration import packages

# Contains installed code bindings only. Each invocation receives its own current Run and Task.
_bound = OrderedDict()
_lock = RLock()
_NEW = object()


@dataclass(frozen=True)
class Bound:
    hooks: object
    receipt: dict


@lru_cache(maxsize=128)
def _saved(inputs_json):
    return json.loads(inputs_json).get("plugin_binding")


def recorded(run):
    raw = run["inputs_json"]
    return _saved(raw) if raw and '"plugin_binding"' in raw else None


def _gateway(workflow, selection, expected=_NEW):
    graph = workflow.workflow_id, workflow.version
    if not any(graph in plugin.contributions.workflows for plugin in selection.plugins):
        if expected is not _NEW and expected is not None:
            raise VersionImmutable("The recorded dispatch adapter lost its selected workflow")
        return None, None
    name = "@".join(graph)
    entries = [entry["dispatch_preparation"][name] for entry in packages.installation()["packages"]
               if name in entry.get("dispatch_preparation", {})]
    if len(entries) > 1:
        raise VersionImmutable(f"Multiple release dispatch adapters for {name}")
    current = entries[0] if entries else None
    if expected is not _NEW and current != expected:
        raise VersionImmutable(f"Restore the recorded dispatch adapter for {name}")
    if current is None:
        return None, None
    module, _, member = current["function"].partition(":")
    spec = importlib.util.find_spec(module)
    source = Path(spec.origin) if spec and spec.origin else None
    if not member or source is None or not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != current["sha256"]:
        raise VersionImmutable(f"Installed dispatch adapter differs from the release pin for {name}")
    return current, packages.component(current["function"])


def _key(workflow, receipt):
    return (config.HARNESS_ROOT, config.CONFIG_DIR, id(workflow.hooks), ids.canonical_json(receipt))


def _bind(workflow, selection, receipt, gateway):
    from .bindings import bind_hooks
    hooks = bind_hooks(workflow.hooks, selection.plugins, workflow_id=workflow.workflow_id,
                       version=workflow.version, dispatch_preparation=gateway)
    bound = Bound(hooks, receipt)
    with _lock:
        key = _key(workflow, receipt)
        _bound[key] = bound
        _bound.move_to_end(key)
        if len(_bound) > 128:
            _bound.popitem(last=False)
    return bound


def for_start(ctx, workflow, references):
    from ..capabilities import shipped
    from .selection import select
    selection = select(references, workflow.workflow_id, workflow.version)
    adapter, gateway = _gateway(workflow, selection)
    methods = {}
    for plugin in selection.plugins:
        for ref in plugin.contributions.capabilities:
            method = shipped.attach(ctx, ref, package=plugin)
            name = method["capability_version"]
            if name in methods and methods[name] != method:
                raise VersionImmutable(f"Composition has conflicting method bindings for {name}")
            methods[name] = method
    receipt = {"workflow": {"id": workflow.workflow_id, "version": workflow.version,
                            "sha256": workflow.digest()},
               "packages": selection.pins, "capabilities": [methods[k] for k in sorted(methods)]}
    if adapter is not None:
        receipt["dispatch_preparation"] = adapter
    return _bind(workflow, selection, receipt, gateway)


def for_saved(workflow, receipt):
    from .selection import restore
    if receipt.get("workflow") != {"id": workflow.workflow_id, "version": workflow.version, "sha256": workflow.digest()}:
        raise VersionImmutable("Restore the Run's original workflow before executing its plugins")
    key = _key(workflow, receipt)
    with _lock:
        if key in _bound:
            _bound.move_to_end(key)
            return _bound[key]
    try:
        selection = restore(receipt["packages"], workflow.workflow_id, workflow.version)
        _, gateway = _gateway(workflow, selection, receipt.get("dispatch_preparation"))
        return _bind(workflow, selection, receipt, gateway)
    except (SchemaInvalid, KeyError, ImportError, OSError) as exc:
        raise VersionImmutable(f"The Run's original plugin binding could not be restored: {exc}") from exc
