"""Pin publication and execution rules once, when the operator creates a Goal."""
from ..errors import SchemaInvalid


VERSION = "gx.evidence/6.2.1"
LEGACY = "gx.evidence/6.2"


def for_start(spec):
    version = spec.get("evidence_policy", VERSION)
    if version not in (VERSION, LEGACY):
        raise SchemaInvalid("Unknown Goal evidence policy")
    return version


def enabled(conn, run_id):
    row = conn.execute("SELECT json_extract(inputs_json,'$.inputs.evidence_policy') "
                       "FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return bool(row and row[0] == VERSION)
