"""Bind exact installed capability providers to one Goal's explicit method grants."""

from functools import lru_cache
import json

from .. import config, contracts
from ..capabilities import manifest, registry, shipped
from ..errors import SchemaInvalid, VersionImmutable
from . import loader
from .catalog import Pin
from .selection import _references


def _pin(data):
    return Pin(data["id"], data["version"], data["sha256"])


def _document(pin):
    return {"id": pin.id, "version": pin.version, "sha256": pin.digest}


def _compatibility():
    path = config.CONFIG_DIR / "goal_provider_compatibility.json"
    if not path.is_file():
        return {}
    return {_pin(row["from"]): _pin(row["to"]) for row in json.loads(path.read_text())["providers"]}


def _load(roots, closure=None):
    loaded = loader.bootstrap(selected=roots, frozen_closure=closure)
    if loaded.failures:
        raise VersionImmutable("Goal provider unavailable: " + "; ".join(loaded.failures.values()))
    return loaded


def _methods(plugin):
    return loader.capability_references(plugin, plugin.contributions)


def _discover(requested, references, *, module_providers=()):
    if not isinstance(references, list):
        raise SchemaInvalid("plugins must be a list of exact provider id@version references")
    explicit = _references(references)
    if not requested:
        if set(explicit) - set(module_providers):
            raise SchemaInvalid("Selecting a Goal provider also requires a capability grant")
        return {}
    path = config.CONFIG_DIR / "workflow_packages.json"
    roots = explicit or [tuple(row["path"].split("/")) for row in json.loads(path.read_text())["plugins"]]
    available = loader.bootstrap(selected=roots)
    if explicit and available.failures:
        raise SchemaInvalid("Requested Goal provider unavailable: " + "; ".join(available.failures.values()))
    legacy = _compatibility()
    release = json.loads((config.CONFIG_DIR / "shipped.json").read_text())
    standalone = release.get("standalone_goal_methods", ())
    provided = {}
    used = set(module_providers)
    for cap in requested:
        matches = []
        for pkg in available.plugins.values():
            ref = _methods(pkg).get(cap)
            if ref is None:
                continue
            # Static workflow packages may refer to a shipped method without providing its behavior.
            if not explicit and pkg.contributions.workflows and ref == cap and cap not in pkg.contributions.goal_methods:
                continue
            matches.append((pkg, ref))
        if not explicit:
            matches = [(pkg, ref) for pkg, ref in matches if pkg.pin not in legacy or
                       all(candidate.pin != legacy[pkg.pin] for candidate, _ in matches)]
        if len(matches) > 1:
            labels = [f"{p.pin.id}@{p.pin.version}" for p, _ in matches]
            raise SchemaInvalid(f"Multiple installed providers for {cap}: {', '.join(labels)}. Select an exact plugin")
        if matches:
            pkg, _ = provided[cap] = matches[0]
            used.add((pkg.pin.id, pkg.pin.version))
        elif cap in release["capabilities"] and cap not in standalone:
            raise SchemaInvalid(f"No installed Goal provider for {cap}; its shipped worker needs a provider")
    if set(explicit) - used:
        raise SchemaInvalid("Selected Goal plugin supplies none of the requested capabilities: "
                            + ", ".join("@".join(key) for key in sorted(set(explicit) - used)))
    return provided


def for_start(ctx, requested, references, *, module_catalog=None, required_providers=None):
    """Attach approved bytes, then persist the composition with Run creation before Job admission."""
    owners = [_pin(m["plugin"]) for m in (module_catalog or {}).get("modules", [])]
    providers = _discover(requested, references, module_providers=[(p.id, p.version) for p in owners])
    roots = {package.pin for package, _ in providers.values()}
    pins = sorted(_load(roots).plugins, key=lambda p: (p.id, p.version)) if roots else []
    catalog = []
    receipts = []
    for cap in requested:
        manifest.parse_capability_version(cap)
        package, ref = providers.get(cap, (None, cap))
        required = (required_providers or {}).get(cap)
        if required and (not package or package.pin.digest != required):
            raise VersionImmutable("Installed provider differs from the package frozen for this Goal")
        receipt = shipped.attach(ctx, ref, package=package)
        view = registry.candidate(ctx.conn, cap)
        method = view["manifest"]
        descriptor = {"method": method.get("method_card"), "inputs": manifest.input_roles(method),
                      "outputs": method["outputs"], "limitations": method.get("limitations")}
        if method.get("applicable_domain"):
            descriptor["applicable_domain"] = method["applicable_domain"]
        if module_catalog:
            descriptor.update(manifest.cost_card(method["resources"]))
            descriptor["family"] = method["method_card"]["family"]
            descriptor["output_summary"] = [{k: v for k, v in item.items() if k in ("name", "kind", "nature")}
                                            for item in method["outputs"]]
        provider = {}
        if package:
            hooks = package.contributions.goal_methods.get(cap)
            if hooks and hooks.describe:
                descriptor.update(hooks.describe(cap))
            provider = {"plugin": _document(package.pin), "provider_closure": [_document(p) for p in pins]}
        catalog.append({**descriptor, **receipt, **provider})
        receipts.append(receipt)
    # Verify native tool names before the first Task can be issued.
    tools_for_catalog(catalog)
    closure = set(pins) | {_pin(p) for row in (module_catalog or {}).get("modules", [])
                            for p in row["provider_closure"]}
    return catalog, {"kind": "goal", "packages": [_document(p) for p in sorted(closure, key=lambda x: (x.id, x.version))],
                     "capabilities": receipts}


@lru_cache(maxsize=128)
def _restore(install_root, catalog_path, owner, closure, adapter):
    """The process keeps verified immutable code. A cold host verifies installed bytes again."""
    held = _load([owner], closure)
    package = held.get(owner.id, owner.version, owner.digest)
    if adapter:
        held = _load([adapter])
        package = held.get(adapter.id, adapter.version, adapter.digest)
    return package, held


def _provider(row):
    if not row.get("plugin"):
        return None
    owner = _pin(row["plugin"])
    closure = tuple(_pin(p) for p in row.get("provider_closure", ()))
    adapter = _compatibility().get(owner)
    return _restore(config.HARNESS_ROOT, config.CONFIG_DIR, owner, closure or None, adapter)[0]


@lru_cache(maxsize=128)
def _effective_provider(install_root, catalog_path, owner, frozen, adapter):
    # Include only packages used by this provider. A seat may have unrelated plugins installed.
    package, loaded = _restore(install_root, catalog_path, owner, frozen or None, adapter)
    seen = set()
    def visit(plugin):
        if plugin.pin in seen:
            return
        seen.add(plugin.pin)
        for dep in plugin.manifest.dependencies:
            candidate = next((p for p in loaded.plugins.values()
                              if p.pin.id == dep.id and p.pin.version == dep.version), None)
            if candidate:
                visit(candidate)
    visit(package)
    return {"provider": _document(package.pin), "dependencies":
            [_document(pin) for pin in sorted(seen - {package.pin}, key=lambda p: (p.id, p.version, p.digest))]}


def effective_provider(row):
    if not row.get("plugin"):
        return None
    owner = _pin(row["plugin"])
    return _effective_provider(config.HARNESS_ROOT, config.CONFIG_DIR, owner,
                               tuple(_pin(p) for p in row.get("provider_closure", ())),
                               _compatibility().get(owner))


def catalog(conn, run_id):
    from ..p0.capability_views import latest
    revised = latest(conn, run_id)
    if revised is not None:
        return revised["catalog"]
    row = conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(row[0]).get("inputs", {}).get("capability_catalog", ()) if row else ()


def method(conn, run_id, capability, *, historical=False, module_binding=None):
    rows = [module_binding["descriptor"]] if module_binding else catalog(conn, run_id)
    for row in rows:
        if row["capability_version"] == capability:
            plugin = _provider(row)
            return plugin.contributions.goal_methods.get(capability) if plugin else None
    if historical:
        from ..p0.capability_views import historical_method
        prior = historical_method(conn, run_id, capability)
        if prior:
            plugin = _provider(prior)
            return plugin.contributions.goal_methods.get(capability) if plugin else None
    return None


def tools_for_catalog(rows):
    result = {}
    reserved = set(contracts.native_goal_tool_names()) | {"gx_read_artifact", "gx_task_context"}
    for row in rows:
        plugin = _provider(row)
        if plugin:
            for name, tool in plugin.contributions.goal_tools.items():
                if tool.capability == row["capability_version"]:
                    if name in reserved:
                        raise SchemaInvalid(f"Goal plugin tool {name} conflicts with a native tool")
                    if name in result:
                        raise SchemaInvalid(f"Duplicate Goal tool {name}")
                    result[name] = tool
    return result


def tools(conn, run_id):
    return tools_for_catalog(catalog(conn, run_id))
