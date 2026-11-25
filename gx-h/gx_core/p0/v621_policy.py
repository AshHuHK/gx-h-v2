"""Evidence rules are pinned once at Goal admission. Stored Runs keep their version."""
from ..errors import SchemaInvalid


VERSION = "gx.evidence/6.2.4"
V623 = "gx.evidence/6.2.3"
V622 = "gx.evidence/6.2.2"
PREVIOUS = "gx.evidence/6.2.1"
LEGACY = "gx.evidence/6.2"
LIVE_SCIENCE = (V623, VERSION)
TYPED = (V622, *LIVE_SCIENCE)
GOVERNED = (PREVIOUS, *TYPED)


def for_start(spec):
    policy = spec.get("evidence_policy", VERSION)
    if policy not in (*GOVERNED, LEGACY):
        raise SchemaInvalid("Unknown Goal evidence policy")
    return policy


def admit(spec):
    policy = for_start(spec)
    replay = spec.get("historical_replay", False)
    if type(replay) is not bool or replay != (policy != VERSION):
        raise SchemaInvalid("New research uses the current evidence policy. To reproduce an older policy "
                            "select its exact version and request historical replay.")
    return policy


def pinned(conn, run_id):
    row = conn.execute("SELECT json_extract(inputs_json,'$.inputs.evidence_policy') "
                       "FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return (row[0] if row is not None else None) or LEGACY


def enabled(conn, run_id):
    return pinned(conn, run_id) in GOVERNED


def current(conn, run_id):
    """Run supports the typed evidence contract introduced in V6.2.2."""
    return pinned(conn, run_id) in TYPED


def live_science(conn, run_id):
    return pinned(conn, run_id) in LIVE_SCIENCE


def science_execution(conn, run_id):
    return pinned(conn, run_id) == VERSION
