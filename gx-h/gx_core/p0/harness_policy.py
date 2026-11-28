"""Freeze the research control contract independently of scientific evidence grading."""
VERSION = "gx.harness/2"


def enabled(snapshot):
    return (snapshot or {}).get("harness_policy") == VERSION


def active(conn, run_id):
    row = conn.execute(
        "SELECT json_extract(inputs_json, '$.inputs.harness_policy') FROM runs WHERE run_id=?", (run_id,)
    ).fetchone()
    return row is not None and row[0] == VERSION


def inferable_fields(decision_body):
    """Project the caller-owned draft schema; selected inputs are checked before Job admission."""
    for variant in decision_body["properties"]["ops"]["items"]["oneOf"]:
        kind = variant["properties"]["op"]["const"]
        field = {"add_node": "inputs", "close_run": "reason", "open_intent": "intent"}.get(kind)
        if field in variant["required"]:
            variant["required"].remove(field)
        if kind == "close_run":
            variant["properties"]["reason"] = dict(variant["properties"]["reason"],
                                                   description="Defaults to the submitted rationale for a sole close_run.")
        elif kind == "open_intent":
            variant["properties"]["intent"] = dict(variant["properties"]["intent"],
                                                   description="New local: label. Optional when this is the only operation.")
    return decision_body
