"""Read shared research assignments, human sources and accepted worker judgments."""
from ..artifacts import ArtifactService
from ..domain import clock, ids
from ..errors import SchemaInvalid
from ..p0 import intent
from ..persistence import store


ADVISORY_ITEMS = frozenset({"soft_preference", "priority", "optimization_target", "data_source"})
REVIEW_INSTRUCTION = (
    "Use gx_task_context for the latest task-specific items and sources, including after a notification. "
    "Use the highest version of each item. A removed item no longer applies. "
    "For each active intent_items entry return intent_responses [{item_id,version,reason}]. Apply output "
    "preferences in the actual text. For a decision's priority or optimization response, cite the compared "
    "research artifacts in evidence_refs and explain how the comparison determined your selection. "
    "If departing from a specified path_order, explain priority_exception on that item's response. "
    "Read assigned supplement artifacts as evidence. For each one, independently record source_reviews "
    "(reviewer) or source_decisions (decision worker), entries {source_id,artifact_ref,disposition,reason,"
    "evidence_refs}. Use accepted, rejected or pending. Cite an actual reviewed research artifact in "
    "evidence_refs. Explain the bearing on the claim or chosen action, including unresolved uncertainty. "
    "A decision must cite an independent assessment that covers these sources and the same evidence."
)
_EARLIER_ROLES = {"science_assessment": "review", "science_decision": "decision"}


def assignment_role(inputs):
    return inputs.get("science_role") or _EARLIER_ROLES.get(inputs.get("output_kind"))


def consumes_item(role, item_type, value):
    if item_type == "data_source":
        return role in ("author", "review", "decision") and isinstance(value, dict) and "artifact_ref" in value
    if item_type == "soft_preference":
        return role in ("author", "review", "decision")
    return role in ("author", "decision") and item_type in ("priority", "optimization_target")


def _sources(conn, run_id, *, round_index=None, include_document=False):
    query = ("SELECT source_id,provenance_json FROM sources "
             "WHERE namespace=? AND kind='operator_note' AND origin='human_input'")
    params = [run_id]
    if round_index is not None:
        # Older projects stored human sources before a review round was recorded.
        query += (" AND (json_extract(provenance_json,'$.review_round') IS NULL "
                  "OR json_extract(provenance_json,'$.review_round')=?)")
        params.append(round_index)
    for row in conn.execute(query + " ORDER BY rowid", params):
        meta = store.loads(row, "provenance_json")
        source = {"source_id": row["source_id"], "artifact_ref": meta.get("review_artifact_ref"),
                  "round": meta.get("review_round")}
        if include_document:
            segments = [dict(segment) for segment in conn.execute(
                "SELECT segment_id,text,text_sha256 FROM source_segments WHERE source_id=? ORDER BY ordinal",
                (row["source_id"],))]
            source.update(text="\n".join(segment["text"] for segment in segments), segments=segments,
                          provenance={k: v for k, v in meta.items() if k not in ("review_round", "review_artifact_ref")})
        yield source


def task_context(conn, run_id, task_id):
    row = conn.execute("SELECT input_json FROM tasks WHERE task_id=? AND run_id=?", (task_id, run_id)).fetchone()
    if row is None:
        raise SchemaInvalid("unknown project task")
    inputs = store.loads(row, "input_json", {})
    sources = []
    if assignment_role(inputs):
        sources = list(_sources(conn, run_id, round_index=inputs.get("round", 0), include_document=True))
    return {"task_id": task_id, "intent": intent.snapshot(conn, run_id, "task", task_id),
            "supplement_sources": sources, "instruction": REVIEW_INSTRUCTION}


def _accepted_products(conn, run_id, role):
    rows = conn.execute("SELECT t.task_id,t.input_json,s.submission_id,s.artifact_refs_json "
                        "FROM tasks t JOIN submissions s USING(task_id) WHERE t.run_id=? AND t.status='ACCEPTED' "
                        "AND s.attempt=t.attempt AND s.result='ACCEPTED' ORDER BY s.rowid DESC", (run_id,))
    artifacts = ArtifactService(conn)
    for row in rows:
        inputs = store.loads(row, "input_json", {})
        if assignment_role(inputs) == role:
            for ref in store.loads(row, "artifact_refs_json", []):
                if artifacts.describe(ref)["kind"] == inputs["output_kind"]:
                    yield ({"task_id": row["task_id"], "submission_id": row["submission_id"],
                            "artifact_ref": ref}, inputs, artifacts.read_json(ref))


def source_status(conn, run_id):
    current = {
        source["source_id"]: {**source, "disposition": "pending", "review": None, "decision": None}
        for source in _sources(conn, run_id)
    }
    if current:
        for role, field in (("review", "source_reviews"), ("decision", "source_decisions")):
            for product, _, body in _accepted_products(conn, run_id, role):
                judgments = {entry["source_id"]: entry for entry in body.get(field, [])}
                for source_id, judgment in judgments.items():
                    source = current.get(source_id)
                    if source is not None and source[role] is None:
                        source[role] = {**judgment, "product_ref": product["artifact_ref"],
                                        "submission_id": product["submission_id"], "task_id": product["task_id"]}
                        if role == "decision":
                            source["disposition"] = judgment["disposition"]
    return list(current.values())


def review_history(conn, run_id):
    sources = list(_sources(conn, run_id))
    history = []
    for product, inputs, body in _accepted_products(conn, run_id, "review"):
        round_index = inputs.get("round", 0)
        assigned = {source["source_id"]: source["artifact_ref"] for source in sources
                    if round_index is None or source["round"] in (None, round_index)}
        judgments = body.get("source_reviews", [])
        reviewed = {entry["source_id"]: entry.get("artifact_ref") for entry in judgments}
        prior = [body["evidence_ref"]] if body.get("evidence_ref") else []
        history.append({**product, "round": round_index, "proposal_ref": body.get("proposal_ref"),
                        "evidence_refs": body.get("evidence_refs") or prior, "source_reviews": judgments,
                        "source_ids": [entry["source_id"] for entry in judgments], "covers_current_sources": assigned == reviewed})
    return history


def record_attempt_failure(conn, *, run_id, task_id, attempt, kind, reason, operation_id):
    if kind not in ("agent_execution_failure", "infrastructure_failure"):
        raise SchemaInvalid("invalid worker failure class")
    settlement_id = "failure_" + ids.sha256_of_obj([run_id, task_id, attempt, operation_id])
    conn.execute("INSERT INTO science_settlements(settlement_id,run_id,task_id,attempt,kind,reason,created_at) "
                 "VALUES(?,?,?,?,?,?,?) ON CONFLICT(settlement_id) DO NOTHING",
                 (settlement_id, run_id, task_id, attempt, kind, reason, clock.now_iso()))
