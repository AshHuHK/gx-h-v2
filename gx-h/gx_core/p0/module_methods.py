"""Provider selection on one delegated module, with immutable receipts for its admitted nodes."""
import json

from ..domain import ids
from ..errors import DependencyUnmet, SchemaInvalid, ScopeDenied, StaleRevision, VersionImmutable
from ..persistence import store
from ..plugins import goal
from . import capability_views, intent

EVENT = "v62.module.method_selected"


def _grant(conn, run, scope):
    row = conn.execute("SELECT grant_json FROM v62_modules WHERE run_id=? AND scope=?", (run, scope)).fetchone()
    return json.loads(row[0]) if row else {}


def _available(conn, run, choices):
    if not choices:
        return {}
    withdrawn = {row[0] for row in conn.execute("""SELECT e.subject_id FROM events e
        WHERE e.run_id=? AND e.kind=? AND json_extract(e.payload_json,'$.disabled')=e.subject_id
        AND NOT EXISTS (SELECT 1 FROM events newer WHERE newer.run_id=e.run_id AND newer.kind=e.kind
                        AND newer.subject_id=e.subject_id AND newer.event_id>e.event_id)""",
        (run, capability_views.EVENT))}
    return {ref: value for ref, value in choices.items() if ref not in withdrawn}


def options(conn, run, scope):
    return _available(conn, run, _grant(conn, run, scope).get("method_options", {}))


def revision(conn, run, scope):
    return _grant(conn, run, scope).get("local_binding_revision", 0)


def receipt(conn, run, branch, cap, event_id):
    """Only GX graph compilation can associate an accepted selection with a node."""
    row = conn.execute("""SELECT e.event_id,e.payload_json,m.scope FROM events e JOIN v62_modules m
             ON m.run_id=e.run_id AND m.module_instance_id=e.subject_id
             WHERE e.event_id=? AND e.kind=? AND e.run_id=? AND m.branch_id=?""",
                       (event_id, EVENT, run, branch)).fetchone()
    if row:
        data = json.loads(row["payload_json"])
        if data["action"] == "enable" and data["method"] == cap:
            return {"event_id": row["event_id"], "scope": row["scope"],
                    "revision": data["revision"], "descriptor": data["descriptor"]}
    raise ScopeDenied("The method binding does not belong to this module branch and capability")


def for_spec(conn, run, spec):
    event = spec.get("module_method_event")
    return receipt(conn, run, spec["branch"], spec["capability"], event) if event is not None else None


def for_request(conn, request):
    node_id = (request.get("binding") or {}).get("v6_node_id")
    if node_id is None:
        return None
    row = conn.execute("SELECT spec_json FROM v6_nodes WHERE run_id=? AND node_id=?",
                       (request["run_id"], node_id)).fetchone()
    if row:
        spec = json.loads(row[0])
        if spec["capability"] == request["capability_version"]:
            return for_spec(conn, request["run_id"], spec)
    return None


def catalog(conn, run, scope):
    row = conn.execute("SELECT grant_json,branch_id FROM v62_modules WHERE run_id=? AND scope=?",
                       (run, scope)).fetchone()
    if row is None:
        return []
    bindings = json.loads(row["grant_json"]).get("local_bindings", {})
    return [receipt(conn, run, row["branch_id"], cap, event)["descriptor"]
            for cap, event in _available(conn, run, bindings).items()]


def pins(conn, run, scope):
    grant = _grant(conn, run, scope)
    active = capability_views.current(conn, run)["active"]
    inherited = {k: active[k] for k in grant.get("capabilities", ()) if k in active}
    inherited.update({r["capability_version"]: r["portable_sha256"] for r in catalog(conn, run, scope)})
    return inherited


def require_available(conn, run, cap):
    """A Run-wide withdrawal also stops unadmitted local work for the same method."""
    latest = conn.execute("""SELECT json_extract(payload_json,'$.disabled') FROM events
                 WHERE run_id=? AND kind=? AND subject_id=? ORDER BY event_id DESC LIMIT 1""",
                          (run, capability_views.EVENT, cap)).fetchone()
    if latest and latest[0] == cap:
        raise DependencyUnmet(f"Method {cap} was disabled for this Run",
                              details={"reason": "capability_disabled", "capability": cap})


def prepare(conn, native, args, packet, row):
    from . import v62_native
    from .driver import context
    run, scope = native["run_id"], packet["scope"]
    instance = v62_native.authorize_read(conn, row, packet, "gx_select_capability", native["turn_id"])
    grant = json.loads(instance["grant_json"])
    if args["revision"] != grant.get("local_binding_revision", 0):
        raise StaleRevision("Local capability choices changed",
                            details={"revision": grant.get("local_binding_revision", 0), "scope": scope})
    choice = grant.get("method_options", {}).get(args["method"])
    if not choice:
        raise ScopeDenied("Method is outside this module's delegated choices")
    if args["action"] == "disable":
        return None
    if intent.forbidden_capability(*args["method"].rsplit("@", 1), intent.active_items(conn, run, intent.BAN_TYPES)):
        raise ScopeDenied("Current human intent forbids the requested method")
    require_available(conn, run, args["method"])
    if args["method"] in grant.get("local_bindings", {}):
        return None
    provider = choice.get("provider")
    records, _ = goal.for_start(context(conn), [args["method"]], [provider] if provider else [],
                                module_catalog={"methods": True},
                                required_providers={args["method"]: choice.get("provider_digest")})
    descriptor = records[0]
    if descriptor["portable_sha256"] != choice["digest"]:
        raise VersionImmutable("Installed method differs from the delegated content pin")
    return descriptor


def commit(conn, native, args, packet, row, descriptor):
    from . import v62_native
    instance = v62_native.authorize_read(conn, row, packet, "gx_select_capability", native["turn_id"])
    grant = json.loads(instance["grant_json"])
    rev = grant.get("local_binding_revision", 0)
    ref, action = args["method"], args["action"]
    if ref not in grant.get("method_options", {}):
        raise ScopeDenied("Method is outside this module's delegated choices")
    if rev != args["revision"]:
        raise StaleRevision("Local capability choices changed", details={"revision": rev, "scope": instance["scope"]})
    bindings = dict(grant.get("local_bindings", {}))
    if action == "enable" and intent.forbidden_capability(*ref.rsplit("@", 1),
            intent.active_items(conn, native["run_id"], intent.BAN_TYPES)):
        raise ScopeDenied("Current human intent forbids the requested method")
    if action == "enable":
        require_available(conn, native["run_id"], ref)
    if action == "disable" and ref not in bindings:
        raise SchemaInvalid("Only a method locally bound by this module can be disabled")
    if action == "enable" and ref in bindings:
        event = bindings[ref]
    else:
        rev += 1
        event = store.append_event(conn, run_id=native["run_id"], actor=native["thread_id"],
                  kind=EVENT, subject_kind="module", subject_id=instance["module_instance_id"],
                  payload={"action": action, "method": ref, "revision": rev,
                           **({"descriptor": descriptor} if descriptor is not None else {})})
        if action == "enable":
            bindings[ref] = event
        else:
            del bindings[ref]
        grant.update(local_bindings=bindings, local_binding_revision=rev)
        conn.execute("UPDATE v62_modules SET grant_json=? WHERE module_instance_id=?",
                     (store._j(grant), instance["module_instance_id"]))
    active = pins(conn, native["run_id"], instance["scope"])
    return {"scope": instance["scope"], "revision": rev, "event_id": event,
            "active": active, "digest": ids.sha256_of_obj(active)}
