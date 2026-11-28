"""Freeze the Lead's interpretation of each immutable PI Goal at the opening catalog call."""
import json

from .. import config
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied, StaleRevision
from ..persistence import store

EVENT = "research.pi.method_scope"
KINDS = ("only", "exclude", "phase", "preference", "unclear", "release")
HARD = {"only", "exclude", "unclear"}
FIELD = {
    "type": "array",
    "description": (
        "Interpret the current PI Goal during your opening overview. Read the full Goal if clipped. "
        "Supply [] if it has no method restrictions or preferences. Quote only the PI's own words. "
        "only/exclude are explicit whole-study boundaries, phase is ordering, preference is advisory. "
        "unclear holds just its targets. A quoted author's position is not a PI instruction. "
        "Targets are listed collection names or exact method@version. On a new PI Goal use release "
        "with inherited clause IDs only where the new PI wording explicitly permits relaxation."
    ),
    "items": {
        "type": "object", "additionalProperties": False, "required": ["kind", "quote", "targets"],
        "properties": {
            "kind": {"enum": list(KINDS)},
            "quote": {"type": "string", "minLength": 1},
            "targets": {"type": "array", "minItems": 1, "uniqueItems": True,
                        "items": {"type": "string", "minLength": 1}},
        },
    },
}


def freeze_groups(choices, attached):
    """Label exact station-authorized pins once, before the Lead has interpreted any wording."""
    site = json.loads((config.CONFIG_DIR / "science_site_access.json").read_text())
    pins = {choice["method"]: choice.get("provider") for choice in choices}
    for entry in attached:
        owner = entry.get("plugin")
        pins[entry["capability_version"]] = f'{owner["id"]}@{owner["version"]}' if owner else None
    groups = {name: sorted(ref for provider, methods in providers.items() for ref in methods
                           if ref in pins and pins[ref] == provider) for name, providers in site.items()}
    groups["*"] = sorted(pins)
    return groups


def _state(conn, run_id):
    row = conn.execute(
        """SELECT g.goal, g.goal_version, json_extract(r.inputs_json,'$.inputs.pi_method_groups') groups,
                  (SELECT e.payload_json FROM events e WHERE e.run_id=g.run_id AND e.kind=?
                   ORDER BY e.event_id DESC LIMIT 1) record
           FROM v6_goals g JOIN runs r USING(run_id) WHERE g.run_id=?""", (EVENT, run_id)
    ).fetchone()
    if row is None or row["groups"] is None:
        return None
    return row, json.loads(row["groups"]), json.loads(row["record"]) if row["record"] else {}


def ceiling(conn, run_id):
    state = _state(conn, run_id)
    if state is None:
        return None
    row, _, last = state
    return set(last["allowed_methods"]) if row["goal_version"] == last.get("goal_version") else set()


def guidance(conn, run_id):
    state = _state(conn, run_id)
    if not state:
        return []
    row, _, last = state
    if row["goal_version"] != last.get("goal_version"):
        return []
    return [{key: c[key] for key in ("kind", "quote", "targets")}
            for c in last.get("applied", ()) if c["kind"] in ("phase", "preference")]


def require(conn, run_id, method):
    allowed = ceiling(conn, run_id)
    if allowed is not None and method not in allowed:
        raise ScopeDenied("This binding requires a current PI method scope that permits it. The Run Lead "
                          "interprets the PI Goal in its opening gx_query_catalog overview.",
                          details={"reason": "pi_method_scope", "method": method})


def description(conn, run_id):
    row, groups, last = _state(conn, run_id)
    recorded = row["goal_version"] == last.get("goal_version")
    result = {"collections": sorted(groups.keys() - {"*"}), "goal_version": row["goal_version"],
              "status": "recorded" if recorded else "pending"}
    if recorded and last["applied"]:
        result["clauses"] = last["applied"]
    elif not recorded:
        inherited = [c for c in last.get("applied", ()) if c["kind"] in HARD]
        if inherited:
            result["inherited"] = inherited
    return result


def record(conn, packet, clauses, identity):
    # Called only under the authenticated native-call writer, after verifying the current decision lease.
    run_id = identity["run_id"]
    state = _state(conn, run_id)
    if state is None or packet.get("scope") != "run":
        raise ScopeDenied("Only the current Run Lead may interpret an immutable PI Goal")
    row, groups, previous = state
    version = row["goal_version"]
    if version != packet["goal_version"]:
        raise StaleRevision("Read the new PI Goal before interpreting its scope")
    if previous.get("goal_version") == version:
        if clauses != previous["clauses"]:
            raise ScopeDenied("The PI method scope is already fixed for this Goal version")
        return
    inherited = {c["id"]: c for c in previous.get("applied", ()) if c["kind"] in HARD}
    current = {}
    universe = set(groups["*"])
    names = groups.keys() - {"*"}
    for clause in clauses:
        kind, quote = clause["kind"], clause["quote"]
        if not quote.strip() or quote not in row["goal"]:
            raise SchemaInvalid("Each method clause needs an exact quote from the current immutable PI Goal")
        targets = set(clause["targets"])
        if kind == "release":
            if not targets <= inherited.keys():
                raise ScopeDenied("Release must reference an inherited clause from a previous PI Goal")
            for target in targets:
                del inherited[target]
            continue
        if not targets <= names | universe:
            raise SchemaInvalid("Use a frozen collection name or exact method@version",
                                details={"collections": sorted(names)})
        clause_id = "pi-method:" + ids.sha256_of_obj([run_id, version, clause])[:24]
        current[clause_id] = {**clause, "id": clause_id, "goal_version": version}
    applied = [*inherited.values(), *current.values()]
    allowed = universe.copy()
    for clause in applied:
        matches = {ref for name in clause["targets"] for ref in groups.get(name, [name])}
        if clause["kind"] == "only":
            allowed.intersection_update(matches)
        elif clause["kind"] in ("exclude", "unclear"):
            allowed.difference_update(matches)
    store.append_event(conn, run_id=run_id, actor=identity["thread_id"], kind=EVENT,
                       subject_kind="goal", subject_id=run_id,
                       payload={"goal_version": version, "objective_sha256": ids.sha256_of_obj(row["goal"]),
                                "clauses": clauses, "applied": applied, "allowed_methods": sorted(allowed),
                                **{k: identity[k] for k in ("thread_id", "turn_id", "call_id")}})
