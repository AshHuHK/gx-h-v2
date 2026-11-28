"""Freeze the research control contract independently of scientific evidence grading."""
VERSION = "gx.harness/2"


def enabled(snapshot):
    return (snapshot or {}).get("harness_policy") == VERSION


def active(conn, run_id):
    row = conn.execute(
        "SELECT json_extract(inputs_json, '$.inputs.harness_policy') FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    return row is not None and row[0] == VERSION


def inferable_inputs(decision_body):
    """Project the caller-owned draft schema; selected inputs are checked before Job admission."""
    for variant in decision_body["properties"]["ops"]["items"]["oneOf"]:
        if variant["properties"]["op"]["const"] == "add_node" and "inputs" in variant["required"]:
            variant["required"].remove("inputs")
    return decision_body
