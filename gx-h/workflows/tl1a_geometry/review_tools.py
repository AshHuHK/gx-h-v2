"""Independent reviewer-initiated fits of the exact deposited coordinates and assigned comparison."""
from gx_core.artifacts import ArtifactService
from gx_core.errors import ScopeDenied
from gx_core.persistence import store

from . import domain

WORKER_TOOLS = ({"type": "function", "name": "gx_recalculate_geometry",
    "description": "Independently parse the raw mmCIF and fit one assigned atom-selection protocol using "
                   "Biopython SVD. Return all chain-pair results, atom correspondence, differences, and frozen tolerance checks.",
    "inputSchema": {"type": "object", "properties": {"protocol": {"type": "string", "enum": list(domain.PROTOCOLS)}},
                    "required": ["protocol"], "additionalProperties": False}},)


def _scope(conn, row, args):
    task = conn.execute("SELECT input_json,lease_id FROM tasks WHERE task_id=? AND run_id=?",
                        (row["task_id"], row["run_id"])).fetchone()
    if not task or set(args) != {"protocol"} or task["lease_id"] != row["lease_id"]:
        raise ScopeDenied("the current geometry reviewer must specify one assigned protocol")
    inputs = store.loads(task, "input_json")
    if inputs.get("output_kind") != "geometry_assessment" or not domain.matches_intent(conn, row["run_id"], inputs):
        raise ScopeDenied("this numerical check belongs to the current coordinate evidence assessment")
    assets = ArtifactService(conn)
    evidence = assets.read_json(inputs["evidence_ref"])
    for route in evidence["routes"]:
        if route["protocol"] == args["protocol"] and route.get("observations_ref"):
            return {"protocol": args["protocol"], "evidence_ref": inputs["evidence_ref"], "structure_ref": inputs["structure_ref"],
                    "criteria_ref": inputs["criteria_ref"], "observations_ref": route["observations_ref"],
                    "measurement_ref": route.get("rescored_ref") or route["observations_ref"], "job_id": route["job_id"],
                    "reviewer_task_id": row["task_id"], "lease_id": row["lease_id"]}
    raise ScopeDenied("the assigned evidence has no original observation for that protocol")


def recalculate_now(conn, row, args):
    from adapters.scientific.tl1a_geometry.review import independent_check
    if conn.in_transaction:
        raise RuntimeError("perform the independent calculation after releasing the SQLite writer")
    scope = _scope(conn, row, args)
    assets = ArtifactService(conn)
    try:
        result = independent_check(assets.read_bytes(scope["structure_ref"]), assets.read_json(scope["measurement_ref"]),
                                   assets.read_json(scope["criteria_ref"]))
    except (ImportError, ValueError, OSError, ArithmeticError) as exc:
        result = {"status": "unavailable", "protocol": scope["protocol"], "error": f"{type(exc).__name__}: {exc}"}
    return {**scope, "result": result}


def worker_call(conn, kind, args, row, envelope, *, prepared=None):
    if kind != "gx_recalculate_geometry" or not isinstance(prepared, dict):
        raise ScopeDenied("a numerical reviewer fit requires its prepared calculation")
    scope = _scope(conn, row, args)
    if any(prepared.get(k) != v for k, v in scope.items()):
        raise ScopeDenied("the measurement or its reviewer assignment changed during this fit")
    doc = {**prepared["result"], **scope, "reviewer_thread_id": row["thread_id"],
           "reviewer_turn_id": envelope["turn_id"], "reviewer_call_id": envelope["call_id"]}
    ref = domain.publish(conn, row["run_id"], "geometry_checks", doc, task_id=row["task_id"])
    return {**doc, "check_ref": ref}


def reviewed(conn, principal, inputs, checks):
    """Verify each reviewer-supplied reference against its actual call and immutable calculation."""
    assets = ArtifactService(conn)
    calls = conn.execute("SELECT result_json FROM p0_root_calls WHERE thread_id=?", (principal,))
    actual = {r["check_ref"]: r for row in calls if isinstance((r := store.loads(row, "result_json")), dict)
              and r.get("check_ref") and r.get("evidence_ref") == inputs["evidence_ref"]
              and r.get("criteria_ref") == inputs["criteria_ref"]}
    results = []
    for protocol, ref in checks.items():
        receipt = actual.get(ref)
        domain.require(receipt and receipt["protocol"] == protocol, "cite the actual reviewer-initiated fit for each protocol")
        doc = assets.read_json(ref)
        domain.require(doc["reviewer_thread_id"] == principal and doc["status"] == receipt["status"],
                       "the numerical receipt and original review artifact must agree")
        results.append(doc)
    return results
