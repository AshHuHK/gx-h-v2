"""The research assignment's current Intent, operator sources and actual worker judgments."""
from ..artifacts import ArtifactService
from ..domain import clock, ids, machines
from ..errors import SchemaInvalid
from ..orchestration import expansion
from ..persistence import store
from ..teams.service import backend_ctx
from . import intent

ADVISORY = {"soft_preference", "priority", "optimization_target", "data_source"}
DISPOSITIONS = {"accepted", "rejected", "pending"}
ROLES = {"science_assessment": "review", "science_decision": "decision"}
INSTRUCTION = (
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


def _check(ok, message):
    if not ok:
        raise SchemaInvalid(message)


def role_of(inputs):
    return inputs.get("science_role") or ROLES.get(inputs.get("output_kind"))


def consumes(role, kind, value):
    if kind == "data_source":
        return role in ("author", "review", "decision") and isinstance(value, dict) and "artifact_ref" in value
    if kind == "soft_preference":
        return role in ("author", "review", "decision")
    return role in ("author", "decision") and kind in ("priority", "optimization_target")


def unsupported_edit(conn, run, edits):
    for edit in edits:
        kind, value = edit["item_type"], edit["value"]
        if kind not in ADVISORY or value is None:
            continue
        if kind != "data_source" and _original_grid(conn, run):
            return {"status": "unsupported", "item_id": edit["item_id"], "item_type": kind,
                    "message": "The pinned science_grid@1 judgments use the original objective and evaluation. "
                               "science_grid@2 implements task-specific output preferences and route comparisons."}
        supported = isinstance(value, str) and bool(value.strip())
        if kind == "priority" and isinstance(value, dict) and set(value) == {"path_order"}:
            order = value["path_order"]
            supported = (isinstance(order, list) and bool(order)
                         and all(isinstance(x, str) and x.strip() for x in order)
                         and len(set(order)) == len(order))
        if kind == "data_source":
            supported = isinstance(value, dict) and (value.get("kind") == "replacement_proposal"
                        or isinstance(value.get("artifact_ref"), str) and conn.execute(
                            "SELECT 1 FROM artifacts WHERE artifact_ref=? AND (run_id=? OR run_id IS NULL)",
                            (value["artifact_ref"], run)).fetchone() is not None)
        if not supported:
            scope = {"soft_preference": "a textual output preference",
                     "optimization_target": "a textual comparison target for the research planner",
                     "priority": "a textual selection preference or an ordered list of route IDs under path_order",
                     "data_source": "a project-readable artifact_ref or a replacement_proposal"}[kind]
            return {"status": "unsupported", "item_id": edit["item_id"], "item_type": kind,
                    "message": f"For {kind}, this project can use {scope}."}
    return None


def _original_grid(conn, run):
    return conn.execute("SELECT 1 FROM runs WHERE run_id=? AND workflow_id='science_grid' "
                        "AND workflow_version='1' AND workflow_sha256 IS NOT NULL", (run,)).fetchone() is not None


def _items(conn, run, role):
    return {key: {k: item[k] for k in ("item_id", "item_type", "version", "status", "value")}
            for key, item in intent.active_items(conn, run, ADVISORY).items()
            if consumes(role, item["item_type"], item["value"])}


def bind_task(conn, run, task_id, role=None):
    if role is None:
        task = conn.execute("SELECT input_json FROM tasks WHERE run_id=? AND task_id=?", (run, task_id)).fetchone()
        role = role_of(store.loads(task, "input_json", {}))
    return intent.bind_dependencies(conn, run, "task", task_id, {k: "context" for k in _items(conn, run, role)})


def sources_for(conn, run, round_index=None):
    sql = ("SELECT source_id,provenance_json FROM sources WHERE namespace=? AND kind='operator_note'"
           " AND origin='human_input'")
    params = [run]
    if round_index is not None:
        # Before this release the original ingress saved sources without a round. Keep
        # these visible to the next independent review until their actual use is recorded.
        sql += " AND (json_extract(provenance_json,'$.review_round')=? OR json_extract(provenance_json,'$.review_round') IS NULL)"
        params.append(round_index)
    rows = []
    for row in conn.execute(sql + " ORDER BY rowid", params):
        meta = store.loads(row, "provenance_json")
        rows.append({"source_id": row["source_id"], "artifact_ref": meta.get("review_artifact_ref"),
                     "round": meta.get("review_round")})
    return rows


def _document(conn, run, source):
    row = conn.execute("SELECT provenance_json FROM sources WHERE source_id=? AND namespace=?", (source, run)).fetchone()
    _check(row, "source does not belong to this project")
    meta = store.loads(row, "provenance_json")
    segments = [dict(r) for r in conn.execute("SELECT segment_id,text,text_sha256 FROM source_segments "
                                             "WHERE source_id=? ORDER BY ordinal", (source,))]
    return {"source_id": source, "text": "\n".join(seg["text"] for seg in segments), "segments": segments,
            "provenance": {k: v for k, v in meta.items() if k not in ("review_round", "review_artifact_ref")}}


def save_decision(conn, run, round_index, submission_id, reviewer_submission_id, decision_ref, actions, reason, stop):
    """Keep the current round decision queryable and its superseded ledger entries immutable."""
    with store.transaction(conn):
        current = conn.execute("SELECT * FROM science_decisions WHERE run_id=? AND round_index=?",
                               (run, round_index)).fetchone()
        if current and current["decision_ref"] == decision_ref:
            return
        body = ArtifactService(conn).read_json(decision_ref)
        if {s["source_id"] for s in sources_for(conn, run, round_index)} != _reviews(body, "source_decisions").keys():
            return
        if current:
            store.append_event(conn, run_id=run, actor="backend", kind="science.decision_reconsidered",
                               subject_kind="run", subject_id=run, evidence_level="EXECUTION_OBSERVED",
                               payload={"previous_decision": dict(current), "successor_ref": decision_ref})
        conn.execute("INSERT INTO science_decisions VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,round_index) "
                     "DO UPDATE SET submission_id=excluded.submission_id,reviewer_submission_id=excluded.reviewer_submission_id,"
                     "decision_ref=excluded.decision_ref,actions_json=excluded.actions_json,reason=excluded.reason,"
                     "stop_reason=excluded.stop_reason,created_at=excluded.created_at",
                     (run, round_index, submission_id, reviewer_submission_id, decision_ref,
                      ids.canonical_json(actions), reason, stop, clock.now_iso()))


def _reopen_closing_judgment(conn, run, checkpoint):
    if not checkpoint:
        return
    state = store.loads(checkpoint, "state_json")
    project = conn.execute("SELECT status,phase,workflow_id,workflow_version FROM runs WHERE run_id=?", (run,)).fetchone()
    if project["status"] != "ACTIVE" or project["workflow_id"] not in ("science_grid", "tl1a_geometry"):
        return
    completed = state.get("completed", [])
    closed_decision = "decision" in completed
    if not closed_decision and not (project["phase"] == "decision" and state.get("advance")):
        return
    # An advance between the worker's hand-in and the final report must observe this
    # write. Discard only that advance token. Its historical checkpoint stays intact.
    state.pop("advance", None)
    if closed_decision:
        state["completed"] = [name for name in completed if name not in ("decision", "report")]
        for node in ("decision", "report"):
            state.get("outputs", {}).pop(node, None)
        scratch = state.get("scratch", {})
        if "history" in scratch:
            scratch["history"] = [row for row in scratch["history"] if row["round"] != state["round"]]
    conn.execute("UPDATE runs SET phase='decision' WHERE run_id=?", (run,))
    store.save_checkpoint(conn, run_id=run, workflow_id=project["workflow_id"], workflow_version=project["workflow_version"],
                          node="decision", state=state)


def with_context(conn, run, inputs):
    role = role_of(inputs)
    if not role:
        return inputs
    result = {**inputs, "intent_items": _items(conn, run, role),
              "supplement_sources": sources_for(conn, run, inputs.get("round", 0))}
    if INSTRUCTION not in result.get("instruction", ""):
        result["instruction"] = (result.get("instruction", "") + " " + INSTRUCTION).strip()
    return result


def task_context(conn, run_id, task_id):
    task = conn.execute("SELECT input_json FROM tasks WHERE run_id=? AND task_id=?", (run_id, task_id)).fetchone()
    _check(task is not None, "unknown project task")
    inputs = store.loads(task, "input_json", {})
    scope = sources_for(conn, run_id, inputs.get("round", 0)) if role_of(inputs) else []
    return {"task_id": task_id, "intent": intent.snapshot(conn, run_id, "task", task_id),
            "supplement_sources": [{**s, **_document(conn, run_id, s["source_id"])} for s in scope],
            "instruction": INSTRUCTION}


def register_supplement(conn, run_id, source_id):
    """Freeze this source's provenance and destination before acknowledging its human message."""
    from .impact import _release_attempt
    checkpoint = store.latest_checkpoint(conn, run_id)
    round_index = store.loads(checkpoint, "state_json", {}).get("round", 0) if checkpoint else 0
    document = _document(conn, run_id, source_id)
    ref = ArtifactService(conn).publish_bytes(run_id=run_id, namespace=f"{run_id}/sources",
        rel_path=f"{source_id}.json", kind="operator_source", data=ids.canonical_json(document),
        media_type="application/json").artifact_ref
    conn.execute("UPDATE sources SET provenance_json=json_set(provenance_json,'$.review_round',?,"
                 "'$.review_artifact_ref',?) WHERE source_id=? AND namespace=?", (round_index, ref, source_id, run_id))
    if _original_grid(conn, run_id):
        return {"round": round_index, "artifact_ref": ref, "review_status": "pending", "reason": "pinned_version",
                "message": "The original science_grid@1 review contract remains pinned. This source is saved "
                           "for a source-aware judgment and has no accepted review."}
    _reopen_closing_judgment(conn, run_id, checkpoint)
    source = {"source_id": source_id, "artifact_ref": ref}
    topic = "p0.supplement.review"
    event = store.enqueue_outbox(conn, topic=topic, idempotency_key=f"{topic}:{source_id}",
                                payload={"run_id": run_id, **source})
    notices = []
    for task in conn.execute("SELECT * FROM tasks WHERE run_id=? AND status IN "
                             "('CLAIMED','RUNNING','SUBMITTED','VALIDATING')", (run_id,)).fetchall():
        inputs = store.loads(task, "input_json", {})
        role = role_of(inputs)
        if not role or inputs.get("round", 0) != round_index:
            continue
        action = "interrupt" if role == "decision" else "notify"
        if action == "interrupt":
            _release_attempt(conn, task, source_id, [], reason="new evidence needs independent review before the decision")
        notice = "p0.intent.notification"
        notices.append(store.enqueue_outbox(conn, topic=notice,
            idempotency_key=f"{notice}:{source_id}:{task['lease_id']}", payload={
                "run_id": run_id, "task_id": task["task_id"], "attempt": task["attempt"], "lease_id": task["lease_id"],
                "action": action, "source": source, "instruction": INSTRUCTION}))
    return {"round": round_index, "artifact_ref": ref, "review_status": "pending",
            "review_outbox_id": event, "notification_ids": notices}


def _reviews(body, field):
    return {s["source_id"]: s for s in body.get(field, [])}


def review_covers(conn, run, round_index, review_body):
    current = {s["source_id"]: s["artifact_ref"] for s in sources_for(conn, run, round_index)}
    reviewed = {k: row.get("artifact_ref") for k, row in _reviews(review_body, "source_reviews").items()}
    return current == reviewed


def validate_product(conn, task, body, evidence_refs):
    inputs = task["input"]
    role, run = role_of(inputs), task["run_id"]
    if not role:
        return
    active = _items(conn, run, role)
    replies = body.get("intent_responses", [])
    _check(isinstance(replies, list) and all(isinstance(r, dict) and isinstance(r.get("item_id"), str)
           and type(r.get("version")) is int and isinstance(r.get("reason"), str) and r["reason"].strip()
           for r in replies), "supply item IDs, versions and reasons in intent_responses")
    versions = {r["item_id"]: r["version"] for r in replies}
    _check(len(versions) == len(replies), "supply a single acknowledgment per Intent item")
    _check(all(versions.get(key) == item["version"] for key, item in active.items()),
           "apply the current task's required Intent versions; read gx_task_context")
    extra = versions.keys() - active.keys()
    if extra:
        # The brief also exposes dependencies such as the objective and Job limit.
        # Authors may acknowledge them without turning them into advisory criteria.
        bound = intent.snapshot(conn, run, "task", task["task_id"])
        for key in extra:
            _check(key in bound and bound[key]["version"] == versions[key],
                   "acknowledge only the Intent versions assigned to this task")
            current = intent.read_item(conn, run, key)
            _check(current["status"] == "active" and current["version"] == versions[key],
                   "the acknowledged contextual Intent version is no longer current")
    allowed = set(evidence_refs)
    for reply in replies:
        if reply["item_id"] not in active:
            continue
        item = active[reply["item_id"]]
        if item["item_type"] == "soft_preference" and item["value"].strip().casefold() in {
                "中文解释每项结果", "用中文解释每项结果", "explain every result in chinese"}:
            text = [r.get("reason") for r in body.get("findings", [])] if role == "review" else [body.get("reason")]
            if text:
                _check(all(isinstance(s, str) and any("\u4e00" <= char <= "\u9fff" for char in s) for s in text),
                       "explain each finding or decision in the requested Chinese text")
        if role != "decision" or item["item_type"] not in ("priority", "optimization_target"):
            continue
        refs = reply.get("evidence_refs")
        _check(isinstance(refs, list) and refs and all(isinstance(r, str) and r in allowed for r in refs),
               "cite the actual compared evidence for the priority or optimization decision")
        if item["item_type"] != "priority" or not isinstance(item["value"], dict):
            continue
        order = item["value"]["path_order"]
        choices = body.get("continue_paths", body.get("next_protocols", []))
        if choices and choices[0] != order[0]:
            _check(isinstance(reply.get("priority_exception"), str) and reply["priority_exception"].strip(),
                   "explain the decision to depart from the assigned route priority")
    if role not in ("review", "decision"):
        return
    field = "source_reviews" if role == "review" else "source_decisions"
    rows = body.get(field, [])
    _check(isinstance(rows, list) and all(isinstance(r, dict) and isinstance(r.get("source_id"), str) for r in rows),
           f"invalid {field}")
    reviewed = _reviews(body, field)
    _check(len(reviewed) == len(rows), "a source must have a single disposition in this submission")
    scope = {s["source_id"]: s["artifact_ref"] for s in sources_for(conn, run, inputs.get("round", 0))}
    # A reviewer who was assigned the older packet can hand it in as historical. They
    # can also cover a newly steered source. Never grant coverage from assignment alone.
    assigned = {s["source_id"] for s in inputs.get("supplement_sources", [])}
    required = assigned if role == "review" else set(scope)
    _check(required <= reviewed.keys() <= scope.keys(), "account for each assigned source by its exact source_id")
    for key, row in reviewed.items():
        refs = row.get("evidence_refs")
        _check(row.get("artifact_ref") == scope[key] and row.get("disposition") in DISPOSITIONS
               and isinstance(row.get("reason"), str) and row["reason"].strip()
               and isinstance(refs, list) and refs and all(isinstance(ref, str) and ref in allowed for ref in refs),
               "cite the source, actual reviewed research artifacts, disposition and scientific reason")
    if role == "decision" and scope:
        assessment_ref = inputs.get("assessment_ref")
        _check(assessment_ref and review_covers(conn, run, inputs.get("round", 0),
                                                ArtifactService(conn).read_json(assessment_ref)),
               "decision is waiting for independent review of the current sources")


def accepted_products(conn, run, role, round_index=None):
    rows = conn.execute("SELECT t.task_id,t.stage,t.input_json,s.submission_id,s.artifact_refs_json "
                        "FROM tasks t JOIN submissions s USING(task_id) WHERE t.run_id=? AND t.status='ACCEPTED' "
                        "AND s.attempt=t.attempt AND s.result='ACCEPTED' ORDER BY s.rowid DESC", (run,))
    assets = ArtifactService(conn)
    for row in rows:
        inputs = store.loads(row, "input_json", {})
        if role_of(inputs) != role or (round_index is not None and inputs.get("round", 0) != round_index):
            continue
        for ref in store.loads(row, "artifact_refs_json", []):
            if assets.describe(ref)["kind"] == inputs["output_kind"]:
                yield {"task_id": row["task_id"], "submission_id": row["submission_id"], "artifact_ref": ref,
                       "input": inputs, "stage": row["stage"], "body": assets.read_json(ref)}


def source_status(conn, run_id):
    """Read what independent reviewers and decision workers actually cited, also while work is in flight."""
    sources = sources_for(conn, run_id)
    result = {s["source_id"]: {**s, "disposition": "pending", "review": None, "decision": None} for s in sources}
    if not result:
        return []
    for role, field in (("review", "source_reviews"), ("decision", "source_decisions")):
        for product in accepted_products(conn, run_id, role):
            for key, judgment in _reviews(product["body"], field).items():
                if key not in result or result[key][role] is not None:
                    continue
                result[key][role] = {**judgment, "product_ref": product["artifact_ref"],
                                     "submission_id": product["submission_id"], "task_id": product["task_id"]}
                if role == "decision":
                    result[key]["disposition"] = judgment["disposition"]
    return list(result.values())


def review_history(conn, run_id):
    history = []
    for product in accepted_products(conn, run_id, "review"):
        body, inputs = product["body"], product["input"]
        judgments = body.get("source_reviews", [])
        history.append({"task_id": product["task_id"], "submission_id": product["submission_id"],
                        "artifact_ref": product["artifact_ref"], "round": inputs.get("round", 0),
                        "proposal_ref": body.get("proposal_ref"),
                        "evidence_refs": body.get("evidence_refs") or ([body["evidence_ref"]] if body.get("evidence_ref") else []),
                        "source_ids": [entry["source_id"] for entry in judgments], "source_reviews": judgments,
                        "covers_current_sources": review_covers(conn, run_id, inputs.get("round", 0), body)})
    return history


def reconcile_supplement(conn, run_id, source_id):
    """An accepted decision can race the registered source. Reopen just that judgment."""
    with store.transaction(conn):
        run = conn.execute("SELECT phase,status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if run["status"] in machines.terminal_states("run"):
            # A post-completion note remains visibly pending; replay never resurrects a closed Job.
            return True
        source = next((s for s in sources_for(conn, run_id) if s["source_id"] == source_id), None)
        _check(source, "unknown supplemental source")
        prior = next(accepted_products(conn, run_id, "decision", source["round"]), None)
        if prior is None:
            return False
        inputs = prior["input"]
        required = sources_for(conn, run_id, inputs.get("round", 0))
        if {s["source_id"] for s in required} == _reviews(prior["body"], "source_decisions").keys():
            return True
        if prior["stage"] != run["phase"] or run["status"] == "PAUSED":
            return False
        for task in conn.execute("SELECT input_json FROM tasks WHERE run_id=? AND stage=? "
                                 "AND status NOT IN ('ACCEPTED','CANCELLED','REJECTED','EXHAUSTED')",
                                 (run_id, prior["stage"])):
            candidate = store.loads(task, "input_json")
            if role_of(candidate) == "decision" and candidate.get("round") == inputs.get("round"):
                return False
        old = conn.execute("SELECT * FROM tasks WHERE task_id=?", (prior["task_id"],)).fetchone()
        updated = with_context(conn, run_id, inputs)
        spec = {"kind": old["kind"], "role": old["role"], "stage": old["stage"], "bundle_kind": old["bundle_kind"],
                "input": updated, "required_artifacts": store.loads(old, "required_artifacts_json", []),
                "acceptance": store.loads(old, "acceptance_json", {})}
        key = expansion.round_key(run_id, old["stage"], source["round"], "supplemented_decision",
                                  ids.sha256_of_obj([prior["artifact_ref"], required]))
        product = expansion.expand(backend_ctx(conn), run_id, rule="decision_on_supplement", key=key,
            trigger={"previous_decision": prior["artifact_ref"]}, stage=old["stage"], round_index=source["round"], tasks=[spec])
        new_id = product["task_ids"][0]
        deps = intent.snapshot(conn, run_id, "task", prior["task_id"])
        intent.bind_dependencies(conn, run_id, "task", new_id,
                                 {k: v["role"] for k, v in deps.items() if v["status"] == "active"})
        bind_task(conn, run_id, new_id)
        return False
