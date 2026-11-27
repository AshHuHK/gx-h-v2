"""Per-Goal method selection. A revision changes future admissions, never historical runs or Jobs."""
import json
import re

from ..capabilities import manifest, shipped
from ..domain import ids
from ..errors import DependencyUnmet, SchemaInvalid, ScopeDenied, StaleRevision, VersionImmutable
from ..persistence import store
from ..plugins import goal

EVENT = "research.capabilities.changed"
DISCOVERY_POLICY = "gx.catalog/1"


def latest(conn, run_id):
    row = conn.execute("SELECT payload_json FROM events WHERE run_id=? AND kind=? ORDER BY event_id DESC LIMIT 1",
                       (run_id, EVENT)).fetchone()
    return json.loads(row[0]) if row else None


def current(conn, run_id):
    row = conn.execute("""SELECT capabilities_json,(SELECT payload_json FROM events
        WHERE run_id=g.run_id AND kind=? ORDER BY event_id DESC LIMIT 1) AS change
        FROM v6_goals g WHERE run_id=?""", (EVENT, run_id)).fetchone()
    if row is None:
        raise ScopeDenied("Capability selection needs an existing Goal")
    if row["change"]:
        change = json.loads(row["change"])
        return {key:change[key] for key in ("revision", "digest", "active")}
    pins = json.loads(row["capabilities_json"])
    return {"revision":0, "digest":ids.sha256_of_obj(pins), "active":pins}


def historical_method(conn, run_id, ref):
    """Look up the exact earlier binding for result projection or re-selection."""
    history = conn.execute("""SELECT c.value FROM events e,json_each(e.payload_json,'$.catalog') c
        WHERE e.run_id=? AND e.kind=? AND json_extract(c.value,'$.capability_version')=?
        ORDER BY e.event_id DESC LIMIT 1""", (run_id, EVENT, ref)).fetchone()
    if history:
        return json.loads(history[0])
    original = conn.execute("""SELECT c.value FROM runs r,json_each(r.inputs_json,'$.inputs.capability_catalog') c
        WHERE r.run_id=? AND json_extract(c.value,'$.capability_version')=?""", (run_id, ref)).fetchone()
    return json.loads(original[0]) if original else None


def require_for_node(conn, run_id, capability):
    change = latest(conn, run_id)
    if change and capability not in change["active"]:
        raise DependencyUnmet(f"Method {capability} was disabled before Job admission; select another method",
                              details={"reason":"capability_disabled", "capability":capability,
                                       "revision":change["revision"]})


def options(spec):
    items = spec.get("capability_options", [])
    if not isinstance(items, list):
        raise SchemaInvalid("capability_options must be a list of exact method choices")
    result = {}
    for item in items:
        if (not isinstance(item, dict) or set(item) - {"method", "provider", "digest", "provider_digest"} or
                not isinstance(item.get("method"), str) or
                not re.fullmatch("[0-9a-f]{64}", str(item.get("digest", "")))):
            raise SchemaInvalid("Each capability option needs an exact method and portable SHA-256 digest")
        ref = item["method"]
        manifest.parse_capability_version(ref)
        if ref in result or ("provider" in item and
                (not isinstance(item["provider"], str) or item["provider"].count("@") != 1)):
            raise SchemaInvalid("Declare each method option once with an exact provider id@version")
        if "provider_digest" in item and ("provider" not in item or
                not re.fullmatch("[0-9a-f]{64}", str(item["provider_digest"]))):
            raise SchemaInvalid("A provider digest needs an exact provider and package SHA-256")
        result[ref] = item
    return result


def goal_options(inputs):
    """The installation can change after admission. Read the Goal's saved choices."""
    return options(inputs.get("method_scope", inputs.get("p0_spec", {})))


def freeze_scope(spec):
    """Expand authorized collections once, without attaching or enabling a method."""
    explicit = options(spec)
    policy = spec.get("discovery_policy")
    if policy not in (None, DISCOVERY_POLICY):
        raise SchemaInvalid("Unknown method discovery policy")
    if spec.get("historical_replay"):
        if policy or "capability_collections" in spec:
            raise SchemaInvalid("Historical replay uses the original method scope")
        return None
    if policy is None and "capability_collections" not in spec:
        return None
    names = spec.get("capability_collections")
    if "capability_collections" not in spec:
        names = [] if "capabilities" in spec or "capability_options" in spec else ["public_sources"]
    if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
        raise SchemaInvalid("capability_collections must be a list of collection names")
    from . import catalog_index
    installed = catalog_index.installed_methods() if explicit or names else ()
    choices = {}
    unresolved = []
    for ref, choice in explicit.items():
        matching = [r for r in installed if r["capability_version"] == ref and
                    r["portable_sha256"] == choice["digest"] and
                    ("provider" not in choice or r.get("provider") == choice["provider"])]
        if len(matching) > 1:
            raise SchemaInvalid(f"Multiple providers offer {ref}; specify its provider in capability_options")
        if not matching:
            if not choice.get("provider_digest"):
                unresolved.append(dict(choice))
                continue
            # A successor can retain an exact package pin while its deployment is unavailable.
            choices[ref] = dict(choice)
        elif provider := matching[0].get("provider"):
            digest = matching[0]["provider_sha256"]
            choices[ref] = {"provider": provider, "provider_digest": digest, **choice}
        else:
            choices[ref] = dict(choice)
    if names:
        from .. import config

        release = json.loads((config.CONFIG_DIR / "workflow_packages.json").read_text())
        collections = release.get("research_method_collections", {})
        known = {"public_sources", "public_databases", "cpu_analysis", "archived_structure"} | collections.keys()
        if len(set(names)) != len(names) or set(names) - known:
            raise SchemaInvalid("Choose distinct capability_collections from " + ", ".join(sorted(known)))
        for row in installed:
            ref, provider = row["capability_version"], row.get("provider")
            if ref in explicit:
                continue
            labels = row.get("collections", ())
            if not any((name in labels or ref in collections.get(name, {}).get(provider, ()))
                       and (name != "cpu_analysis" or row.get("cost_class") == "local_cpu") for name in names):
                continue
            pin = {"method": ref, "digest": row["portable_sha256"]}
            if provider:
                pin["provider"] = provider
                pin["provider_digest"] = row["provider_sha256"]
            if ref in choices and choices[ref] != pin:
                raise SchemaInvalid(f"Multiple providers offer {ref} in this scope; pin one in capability_options")
            choices[ref] = pin
    return {"capability_collections": names, "capability_options": [choices[ref] for ref in sorted(choices)],
            **({"unavailable_options": unresolved} if unresolved else {})}


def change(conn, run_id, *, enable=None, disable=None, provider=None, expected_digest=None, expected_provider_digest=None,
           expected_revision, request_id, actor="operator", authority=None):
    """Prepare immutable bytes outside SQLite and atomically change one selected method."""
    from . import intent
    from .driver import context
    from .v6_claims import lookup_replay

    if (bool(enable) == bool(disable) or type(expected_revision) is not int or expected_revision < 0
            or not isinstance(request_id, str) or not request_id.strip()):
        raise SchemaInvalid("Select one method to enable or disable and supply the base revision and request ID")
    ref = enable or disable
    manifest.parse_capability_version(ref)
    key = "goal.capabilities:" + ids.sha256_of_obj([run_id, request_id])
    request = dict(enable=enable, disable=disable, provider=provider, digest=expected_digest, revision=expected_revision)
    if expected_provider_digest is not None:
        request["provider_digest"] = expected_provider_digest
    replay = lookup_replay(conn, key, request)
    if replay is not None:
        return replay
    state = current(conn, run_id)
    if expected_revision != state["revision"]:
        replay = lookup_replay(conn, key, request)
        if replay is not None:
            return replay
        raise StaleRevision("Capability view moved; read the current revision", details=state)
    prior = list(goal.catalog(conn, run_id))
    active = dict(state["active"])
    selected = next((row for row in prior if row["capability_version"] == ref), None)
    if enable:
        if selected is None:
            selected = historical_method(conn, run_id, ref)
            if selected:
                prior.append(selected)
        if selected is not None:
            owner = selected.get("plugin")
            if provider and (not owner or provider != owner["id"]+"@"+owner["version"]):
                raise VersionImmutable("Selected capability already binds a different provider")
        else:
            rows, _ = goal.for_start(context(conn), [ref], [provider] if provider else [], module_catalog={"methods":True},
                                     required_providers={ref: expected_provider_digest})
            selected = rows[0]
            if not selected.get("plugin") and ref not in shipped.MANIFESTS:
                raise ScopeDenied("Selecting a new method requires an installed package or a shipped release pin")
            prior.append(selected)
        if expected_digest is not None and selected["portable_sha256"] != expected_digest:
            raise VersionImmutable("Requested digest differs from installed method")
        if expected_provider_digest is not None and selected.get("plugin", {}).get("sha256") != expected_provider_digest:
            raise VersionImmutable("Installed provider differs from the package frozen for this Goal")
        active[ref] = selected["portable_sha256"]
    else:
        if ref not in active:
            raise SchemaInvalid(f"Cannot disable unselected method {ref}")
        del active[ref]
        prior = [r for r in prior if r["capability_version"] != ref]
    prior.sort(key=lambda r:r["capability_version"])
    goal.tools_for_catalog(prior)
    with store.transaction(conn, transaction_owner="GoalCapabilities", operation="select", run_id=run_id):
        idem = store.begin_idempotent(conn, key, "goal.capabilities", request)
        if idem.replay:
            return idem.response
        if authority:
            authority()
        actual = current(conn, run_id)
        if actual["revision"] != expected_revision:
            raise StaleRevision("Concurrent capability selection; read the current revision", details=actual)
        status = conn.execute("SELECT status,goal_version FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
        if status["status"] != "OPEN":
            raise ScopeDenied("A closed Goal cannot select new capabilities")
        if enable and intent.forbidden_capability(*ref.rsplit("@", 1), intent.active_items(conn, run_id, intent.BAN_TYPES)):
            raise ScopeDenied("Current human intent forbids the requested method")
        if active == actual["active"]:
            store.complete_idempotent(conn, key, actual)
            return actual
        answer = {"revision": expected_revision + 1, "active":active,
                  "digest":ids.sha256_of_obj({"active":active, "catalog":prior})}
        conn.execute("UPDATE v6_goals SET capabilities_json=? WHERE run_id=?", (store._j(active), run_id))
        store.append_event(conn, run_id=run_id, kind=EVENT, actor=actor, subject_kind="capability",
                           subject_id=ref, payload={**answer, "goal_version":status["goal_version"],
                           "enabled":enable, "disabled":disable, "catalog":prior})
        if disable:
            from . import v6_jobs
            # Same writer as JobService admission. An admitted Job already owns its ID;
            # a pending assignment instead delivers a specific gap to its decision owner.
            nodes = conn.execute("""SELECT node_id FROM v6_nodes WHERE run_id=? AND status='PENDING'
                AND job_id IS NULL AND json_extract(spec_json,'$.capability')=?""", (run_id, ref)).fetchall()
            for node in nodes:
                v6_jobs._finish(conn, v6_jobs._node(conn, run_id, node[0]), "BLOCKED",
                                reason=f"Method {ref} disabled before Job admission",
                                dependency={"reason":"capability_disabled", "capability":ref,
                                            "revision":answer["revision"]})
        store.complete_idempotent(conn, key, answer)
        return answer
