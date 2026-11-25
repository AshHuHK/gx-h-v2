"""Question-first project admission, frozen Lead briefs and event-driven decisions."""

import json
from pathlib import Path
import uuid

from .. import budgets
from ..artifacts import ArtifactService
from ..domain import clock, ids, machines
from ..errors import SchemaInvalid, VersionImmutable
from ..persistence import records, store
from . import intent

INSTANCE_ID = "gx.v6.instance"
ACTOR = "gx-v6"
ACTIVE_TASKS = ("PENDING", "READY", "CLAIMED", "RUNNING", "BLOCKED", "RETRYABLE", "SUBMITTED", "VALIDATING")


def cli_spec(args):
    raw = json.loads(args.goal_config.read_text()) if args.goal_config else {}
    allowed = {
        "project",
        "run_budget",
        "workspace_budget",
        "budget_profile",
        "turn_token_budget",
        "initial_job_limit",
        "input_files",
        "capabilities",
        "plugins",
        "workspace",
        "intent_items",
    }
    if not isinstance(raw, dict) or set(raw) - allowed:
        raise SchemaInvalid("Goal config accepts only " + ", ".join(sorted(allowed)))
    spec = {"project": "gx-research", **raw, "mode": "goal", "objective": args.goal}
    files = raw.get("input_files", [])
    if not isinstance(files, list):
        raise SchemaInvalid("input_files must be an array")
    base = args.goal_config.resolve().parent if args.goal_config else Path.cwd()
    prepared = []
    for entry in files:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or set(entry) - {"path", "role", "sha256"}
        ):
            raise SchemaInvalid("A material is {path, role?, sha256?}")
        prepared.append({**entry, "path": str((base / entry["path"]).resolve())})
    prepared.extend({"path": str(p.resolve())} for p in args.input or ())
    spec["input_files"] = prepared
    if args.capability:
        if "capabilities" in raw:
            raise SchemaInvalid("Select capabilities in the config or on the command line")
        spec["capabilities"] = args.capability
    return spec


def start(conn, spec, *, run_key, current):
    from ..capabilities import registry
    from ..plugins import goal as goal_plugins
    from ..plugins.runtime import Bound
    from ..orchestration import service
    from ..orchestration.hooks import DEFAULT_HOOKS, basic_research_start
    from . import operator, workspace
    from .cli import initial_action
    from .driver import context, snapshot

    if "initial_job_limit" in spec:
        limit = spec["initial_job_limit"]
        if type(limit) is not int or limit < 0:
            raise SchemaInvalid("initial_job_limit must be a nonnegative integer")
    if "workflow" in spec:
        raise SchemaInvalid("A goal-only run selects capabilities; its scientific recipe starts empty")
    ctx = context(conn)
    files = []
    for item in spec.get("input_files", []):
        path = Path(item["path"]).resolve()
        if service._forbidden(path) is not None:
            raise SchemaInvalid("Input material is outside the allowed input scope")
        observed = ids.sha256_of_file(path)
        if "sha256" in item and item["sha256"] != observed:
            raise VersionImmutable("Authorized material differs from its configured SHA-256")
        files.append({**item, "path": str(path), "sha256": observed})
    if current:
        prior = store.loads(current, "inputs_json")
        if any(a["sha256"] != b["sha256"] for a, b in zip(files, prior["files"])):
            raise VersionImmutable("This run key granted different material bytes; supply a new run key")
        return snapshot(conn, current["run_id"])

    requested = spec.get("capabilities")
    if requested is not None and (
        not isinstance(requested, list)
        or any(not isinstance(k, str) for k in requested)
        or len(requested) != len(set(requested))
    ):
        raise SchemaInvalid("capabilities must be a list of distinct, versioned methods")
    public, composition = goal_plugins.for_start(ctx, requested or [], spec.get("plugins", []))
    inputs = {"p0": True, "mode": "goal", "p0_spec": spec, "capability_catalog": public}
    action = initial_action(spec, basic_research_start(spec))

    def bootstrap(rid):
        first = workspace.bootstrap_action(conn, rid, action)
        if first["items"]:
            mid = str(uuid.uuid5(uuid.NAMESPACE_URL, "gx-p0-bootstrap/" + rid))
            if operator.apply_bootstrap(conn, rid, first, mid).get("status") != "committed":
                raise SchemaInvalid("Initial Goal did not commit")
        # Package-owned methods require an exact saved composition in the Job owner;
        # a workspace registration on its own does not grant them to this Run.
        from .manifest import check_capability

        for entry in public:
            cap = entry["capability_version"]
            check_capability(conn, rid, cap)
            registry.resolve(conn, cap, run_id=rid)
        run = conn.execute("SELECT * FROM runs WHERE run_id=?", (rid,)).fetchone()
        objective = intent.read_item(conn, rid, "objective")
        if objective["value"] != spec["objective"]:
            raise SchemaInvalid("This workspace already has a different project Goal; use its Intent editor")
        refs = [f["artifact_ref"] for f in store.loads(run, "inputs_json")["files"]]
        if None in refs:
            raise SchemaInvalid("Goal inputs must fit the immutable input artifact size bound")
        limits = budgets.state(conn, run["budget_ref"])["limits"]
        conn.execute(
            "INSERT INTO v6_goals(run_id,goal,goal_version,capabilities_json,materials_json,limits_json) "
            "VALUES(?,?,?,?,?,?)",
            (
                rid,
                objective["value"],
                objective["version"],
                store._j({k["capability_version"]: k["portable_sha256"] for k in public}),
                store._j(refs),
                store._j(limits),
            ),
        )
        for state in ("PLANNING", "ACTIVE"):
            store.transition(
                conn,
                machine="run",
                subject_id=rid,
                to_state=state,
                actor=ACTOR,
                reason="Human Goal authorized; a Lead decision is required",
                evidence_level="EXECUTION_OBSERVED",
            )
        ensure_task(conn, rid)

    outcome = service.run_start(
        ctx,
        {
            "objective": spec["objective"],
            "project_ref": spec["project"],
            "budget_profile": spec.get("budget_profile", "p0"),
            "inputs": inputs,
            "input_files": files,
            "idempotency_key": workspace.start_key(spec["project"], run_key),
        },
        prepare_p0=bootstrap,
        plugin_binding=Bound(DEFAULT_HOOKS, composition),
        workspace_key=run_key,
    )
    return snapshot(conn, outcome["run_id"])


def _is_json_artifact(meta):
    media = (meta["media_type"] or "").partition(";")[0].strip().lower()
    return media == "application/json" or media.endswith("+json") or (
        not media and Path(meta["rel_path"]).suffix.lower() == ".json"
    )


def record_observation(conn, run_id, artifact_ref, *, source, scope="run", observation=None):
    """Record a citation to verified, run-local material in its original media type."""
    assets = ArtifactService(conn)
    meta = assets.describe(artifact_ref)
    if meta["run_id"] != run_id or not source or not isinstance(source, str):
        raise SchemaInvalid("The observation needs a run-local artifact and a source ID")
    if (
        scope != "run"
        and not conn.execute(
            "SELECT 1 FROM v6_branches WHERE run_id=? AND branch_id=?", (run_id, scope)
        ).fetchone()
    ):
        raise SchemaInvalid("The observation scope is unknown in this run")
    if _is_json_artifact(meta):
        content = assets.read_json(artifact_ref)
        if observation is not None and ids.canonical_json(observation) != ids.canonical_json(content):
            raise SchemaInvalid("The proposed observation differs from its artifact")
    elif observation is not None:
        raise SchemaInvalid("A non-JSON observation is registered by its immutable artifact reference")
    else:
        verified = assets.verify(artifact_ref)
        if not verified["ok"]:
            raise SchemaInvalid("Observation bytes differ from the registered artifact", details=verified)
    payload = {"source": source, "artifact_ref": artifact_ref, "scope": scope, "sha256": meta["sha256"]}

    def save(tx):
        eid = store.append_event(
            tx,
            run_id=run_id,
            actor=ACTOR,
            kind="v6.observation.added",
            subject_kind="source",
            subject_id=source,
            evidence_level="EXECUTION_OBSERVED",
            payload=payload,
        )
        return {"event_id": eid, **payload}

    key = "v6-observation:" + ids.sha256_of_obj([run_id, scope, source])
    return store.run_idempotent(conn, key, "v6.observation", payload, save)[0]


def supplement(conn, run_id, source_id):
    from .science_inputs import _document

    document = _document(conn, run_id, source_id)
    out = ArtifactService(conn).publish_bytes(
        namespace=run_id + "/sources",
        rel_path=source_id + ".json",
        kind="operator_source",
        data=ids.canonical_json(document),
        run_id=run_id,
        actor="operator",
        media_type="application/json",
        provenance={"source_id": source_id},
        origin="INGEST",
    )
    record_observation(conn, run_id, out.artifact_ref, source=source_id)
    conn.execute(
        "UPDATE sources SET provenance_json=json_set(provenance_json,'$.review_artifact_ref',?) "
        "WHERE source_id=?",
        (out.artifact_ref, source_id),
    )
    return {"artifact_ref": out.artifact_ref, "review_status": "pending"}


def instance(conn, rid, checkpoint):
    """Resolve the execution checkpoint against this Run's exact immutable graph revision."""
    if checkpoint.workflow_id != INSTANCE_ID:
        raise VersionImmutable("Goal execution checkpoint has an unknown representation")
    row = conn.execute(
        "SELECT version,sha256,graph_json FROM v6_graph_versions WHERE run_id=? AND version=?",
        (rid, checkpoint.workflow_version),
    ).fetchone()
    if row is None or checkpoint.state.get("head") != {"version": row["version"], "sha256": row["sha256"]}:
        raise VersionImmutable("Goal checkpoint has no matching version and digest")
    graph = store.loads(row, "graph_json")
    if ids.sha256_of_obj(graph) != row["sha256"]:
        raise VersionImmutable("Goal execution graph bytes differ from its pinned digest")
    return graph


def _events(conn, rid, after, budget_ref=None):
    # A workspace-level change can have no run_id. Include only this account's
    # actual ancestry. Debits for ordinary model turns must not wake another turn.
    events = conn.execute(
        """
        WITH RECURSIVE accounts(ref) AS (
            SELECT coalesce(?, budget_ref) FROM runs WHERE run_id=?
            UNION SELECT b.parent_ref FROM budgets b JOIN accounts a ON b.budget_ref=a.ref
                  WHERE b.parent_ref IS NOT NULL
        )
        SELECT event_id,kind,subject_id,payload_json FROM events
        WHERE event_id>? AND (
          (run_id=? AND (kind IN ('v6.node.settled','v6.observation.added','v6.intent.changed',
                                  'v6.claim.adopted','v6.decision.superseded') OR
          (kind='run.transition' AND prev_state='PAUSED' AND json_extract(payload_json,'$.action')='continue')))
          OR (kind IN ('budget.tightened','budget.raised','budget.allotment_changed') AND
              subject_kind='budget' AND subject_id IN (SELECT ref FROM accounts))
        ) ORDER BY event_id""",
        (budget_ref, rid, after, rid),
    ).fetchall()

    captured = []
    for row in events:
        if row["kind"] == "v6.claim.adopted":
            payload = store.loads(row, "payload_json")
            # Early receipts identify the decision directly; recover its scope from the
            # immutable task even if the event itself did not yet carry that field.
            if "scope" not in payload:
                delegation = conn.execute(
                    "SELECT t.scope FROM v6_decisions d JOIN v6_decision_tasks t USING(task_id) "
                    "WHERE d.run_id=? AND d.decision_ref=?",
                    (rid, payload["decision_ref"]),
                ).fetchone()
                if delegation:
                    payload["scope"] = delegation["scope"]
                    if delegation["scope"].startswith("branch:"):
                        payload["branch_id"] = delegation["scope"].removeprefix("branch:")
                    row = {**dict(row), "payload_json": store._j(payload)}
        captured.append(row)
    return captured


def _matches(row, watches, rid):
    payload = store.loads(row, "payload_json")
    kind = row["kind"]
    events = {
        "v6.decision.superseded": set(),
        "v6.intent.changed": {"goal_changed"},
        "v6.node.settled": {"evidence_added", "job_settled"},
        "v6.observation.added": {"evidence_added"},
        "v6.claim.adopted": {"claim_changed"},
        "run.transition": {"operator_reopened"},
    }.get(kind, {"budget_changed"})
    subjects = {
        "run",
        rid,
        row["subject_id"],
        payload.get("node_id"),
        payload.get("branch_id"),
        payload.get("scope"),
        payload.get("result_ref"),
        payload.get("artifact_ref"),
        payload.get("claim_revision_id"),
        payload.get("claim_ref"),
        payload.get("key"),
    }
    if kind == "v6.intent.changed":
        subjects.update(item["item_id"] for item in payload["items"])
    return any(w["event"] in events and w["subject_ref"] in subjects for w in watches)


def _branch_watch_hits(conn, rid, goal_version, events):
    """An active branch's last adopted watch can request one Run Lead judgment."""
    hits = {}
    if not events:
        return hits
    decisions = conn.execute(
        "SELECT d.decision_ref,d.receipt_json,t.scope,c.event_cursor,b.branch_id,"
        "json_extract(d.request_json,'$.decision.rationale') AS rationale FROM v6_decisions d "
        "JOIN v6_decision_tasks t USING(task_id) JOIN research_contexts c ON c.context_ref=t.context_ref "
        "JOIN v6_branches b ON b.run_id=d.run_id "
        "AND t.scope='branch:'||b.branch_id WHERE b.status='OPEN' AND d.rowid IN ("
        "SELECT max(x.rowid) FROM v6_decisions x JOIN v6_decision_tasks s USING(task_id) "
        "WHERE x.run_id=? AND s.goal_version=? AND s.scope LIKE 'branch:%' GROUP BY s.scope) "
        "ORDER BY b.branch_id", (rid, goal_version)
    )
    for decision in decisions:
        watches = store.loads(decision, "receipt_json")["reconsider_when"]
        for event in events:
            payload = store.loads(event, "payload_json")
            event_scope = payload.get("branch_id") or payload.get("scope") or "run"
            if (event["event_id"] <= decision["event_cursor"]
                    or event_scope not in ("run", decision["scope"], decision["branch_id"])
                    or payload.get("decision_ref") == decision["decision_ref"] and event["kind"] == "v6.claim.adopted"):
                continue
            matched = [w for w in watches if _matches(event, [w], rid)]
            if matched:
                hits.setdefault(event["event_id"], []).append({
                    "scope": decision["scope"], "branch_id": decision["branch_id"],
                    "decision_ref": decision["decision_ref"], "previous_rationale": decision["rationale"],
                    "matched_watches": matched,
                })
    return hits


def _operator_evidence(conn, rid, row):
    if row["kind"] != "v6.observation.added":
        return False
    return conn.execute(
        "SELECT 1 FROM sources WHERE namespace=? AND source_id=? AND origin='human_input' "
        "AND kind='operator_note' AND json_extract(provenance_json,'$.review_artifact_ref')=?",
        (rid, row["subject_id"], store.loads(row, "payload_json")["artifact_ref"]),
    ).fetchone() is not None


def _run_wakes(conn, rid, prior, events):
    watches = store.loads(prior, "receipt_json")["reconsider_when"]
    for row in events:
        payload = store.loads(row, "payload_json")
        if row["kind"] == "v6.claim.adopted" and payload.get("decision_ref") == prior["decision_ref"]:
            continue
        if (row["kind"] == "v6.intent.changed" or _operator_evidence(conn, rid, row)
                or row["kind"] == "v6.decision.superseded" and payload.get("scope") == "run"
                or _matches(row, watches, rid)):
            return True
    return False


def _open(conn, rid):
    marks = ",".join("?" for _ in ACTIVE_TASKS)
    return conn.execute(
        "SELECT t.task_id,d.base_json FROM v6_decision_tasks d JOIN tasks t USING(task_id) "
        f"WHERE d.run_id=? AND d.scope='run' AND t.status IN ({marks}) LIMIT 1",
        (rid, *ACTIVE_TASKS),
    ).fetchone()


def _latest(conn, rid):
    return conn.execute(
        "SELECT d.decision_ref,d.request_json,d.receipt_json,c.event_cursor,c.version FROM v6_decisions d "
        "JOIN research_contexts c USING(context_ref) JOIN v6_decision_tasks t USING(task_id) "
        "WHERE d.run_id=? AND t.scope='run' ORDER BY d.rowid DESC LIMIT 1",
        (rid,),
    ).fetchone()


def _brief(conn, run, goal, head, events, cursor, prior):
    from ..plugins import goal as goal_plugins
    from . import v6_claims

    rid = run["run_id"]
    data = store.loads(run, "inputs_json")
    files = [
        {"name": Path(f["path"]).name, **{k: f.get(k) for k in ("artifact_ref", "sha256", "role")}}
        for f in data["files"]
    ]
    observations = []
    refs = [f["artifact_ref"] for f in files]
    for row in events:
        p = store.loads(row, "payload_json")
        ref = p.get("result_ref") or p.get("artifact_ref")
        facts = {
            "id": p.get("source", row["subject_id"]),
            "subject_ref": row["subject_id"],
            "event": row["kind"],
            "event_id": row["event_id"],
            "source_job_id": p.get("job_id"),
            "node_id": p.get("node_id"),
            "branch_id": p.get("branch_id"),
            "status": p.get("status"),
            "artifact_ref": ref,
        }
        if ref:
            meta = ArtifactService(conn).describe(ref)
            if meta["run_id"] != rid:
                raise SchemaInvalid("Observation artifact belongs to another run")
            facts["sha256"] = meta["sha256"]
            refs.append(ref)
            if row["kind"] == "v6.observation.added":
                refs.append(facts["id"])
            if meta["bytes"] <= 32_768 and _is_json_artifact(meta):
                content = ArtifactService(conn).read_json(ref)
                job = (
                    conn.execute(
                        "SELECT capability_id,capability_version FROM jobs WHERE job_id=? AND run_id=?",
                        (p.get("job_id"), rid),
                    ).fetchone()
                    if row["kind"] == "v6.node.settled"
                    else None
                )
                if job:
                    provider = goal_plugins.method(conn, rid, f"{job[0]}@{job[1]}")
                    if provider and provider.project:
                        content = provider.project(content)
                facts["observation"] = content
            else:
                facts.update(read_with="gx_read_artifact", media_type=meta["media_type"], bytes=meta["bytes"])
        observations.append(facts)
    nodes = [
        dict(r)
        for r in conn.execute(
            "SELECT n.node_id,branch_id,version,status,job_id,result_ref,spec_json,exec_key,w.wait_json "
            "FROM v6_nodes n LEFT JOIN v6_node_waits w USING(node_id) WHERE run_id=? ORDER BY n.rowid",
            (rid,),
        )
    ]
    for node in nodes:
        node["wait"] = json.loads(node.pop("wait_json") or "null")
        spec = json.loads(node.pop("spec_json"))
        node["commission"] = {
            k: spec[k] for k in ("capability", "inputs", "depends_on", "parameters", "question") if k in spec
        }
    branches = [
        dict(r)
        for r in conn.execute(
            "SELECT branch_id,spec_json,status FROM v6_branches WHERE run_id=? " "ORDER BY rowid", (rid,)
        )
    ]
    for branch in branches:
        branch["spec"] = json.loads(branch.pop("spec_json"))
    refs.extend(n["result_ref"] for n in nodes if n["result_ref"])
    # Previous observations stay available for citation and scoped reads on continuation.
    for older in conn.execute(
        "SELECT subject_id,json_extract(payload_json,'$.artifact_ref') AS ref "
        "FROM events WHERE run_id=? AND kind='v6.observation.added' AND event_id<=?",
        (rid, cursor),
    ):
        refs.extend([older["subject_id"], older["ref"]])
    catalog = [
        {k: v for k, v in row.items() if k != "content_artifact_ref"}
        for row in data["inputs"]["capability_catalog"]
    ]
    account = budgets.state(conn, run["budget_ref"])
    claims = v6_claims.current(conn, rid)
    for claim in claims:
        refs.extend(x for x in (claim.get("claim_ref"), claim.get("claim_revision_id")) if x)
    previous = None
    watches = []
    if prior:
        body = store.loads(prior, "request_json")["decision"]
        receipt = store.loads(prior, "receipt_json")
        watches = receipt["reconsider_when"]
        previous = {
            "decision_ref": prior["decision_ref"],
            "scope": receipt["scope"],
            "outcome": receipt["outcome"],
            "rationale": body["rationale"],
            "evidence_refs": body["evidence_refs"],
            "head": receipt["result_head"],
            "reconsider_when": watches,
            "consumed_cursor": prior["event_cursor"],
        }
    deltas = [
        {
            "event_id": row["event_id"],
            "event": row["kind"],
            "subject_ref": row["subject_id"],
            "scope": store.loads(row, "payload_json").get("scope")
            or store.loads(row, "payload_json").get("branch_id")
            or "run",
            "matched_watches": [w for w in watches if _matches(row, [w], rid)],
        }
        for row in events
    ]
    budget = {k: account[k] for k in ("limits", "spent", "reserved", "available")}
    grant = data.get("inputs", {}).get("p0_spec", {})
    if head is None and "initial_job_limit" in grant:
        budget["initial_job_limit"] = grant["initial_job_limit"]
    return {
        "run_id": rid,
        "scope": "run",
        "previous_decision": previous,
        "trigger": {
            "since_cursor": prior["event_cursor"] if prior else 0,
            "through_cursor": cursor,
            "changes": deltas,
        },
        "goal": goal["goal"],
        "goal_version": goal["goal_version"],
        "head": head,
        "intent": {
            k: {"value": i["value"], "version": i["version"]}
            for k, i in intent.active_items(conn, rid).items()
        },
        "materials": files,
        "capabilities": catalog,
        "branches": branches,
        "nodes": nodes,
        "claims": claims,
        "observations": observations,
        "evidence_refs": list(dict.fromkeys(refs)),
        "budget": budget,
    }


def ensure_task(conn, run_id):
    """Coalesce eligible evidence; cursors advance only when its exact decision is adopted."""
    from . import v6_decisions

    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    goal = conn.execute("SELECT * FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    if not goal or goal["status"] != "OPEN" or run["status"] != "ACTIVE":
        return None
    open_task = _open(conn, run_id)
    if open_task:
        if json.loads(open_task["base_json"] or "null") == v6_decisions.head(conn, run_id):
            return None
        with store.transaction(
            conn, transaction_owner="ScientificLead", operation="supersede_base", run_id=run_id
        ):
            task = _open(conn, run_id)
            if task and json.loads(task["base_json"] or "null") != v6_decisions.head(conn, run_id):
                cancel_decision(
                    conn, run_id, task["task_id"], reason="Another scope committed a new graph head"
                )
                store.append_event(
                    conn,
                    kind="v6.decision.superseded",
                    run_id=run_id,
                    subject_kind="task",
                    subject_id=task["task_id"],
                    actor=ACTOR,
                    evidence_level="EXECUTION_OBSERVED",
                    payload={"scope": "run"},
                )
            else:
                return None
    # Use one WAL read snapshot for goal, graph, events, budget and artifact references.
    # A later observation gets a greater cursor and survives this capture for its next decision.
    conn.execute("SAVEPOINT v6_capture_brief")
    try:
        planned = _prepare_brief(conn, run_id)
    finally:
        conn.execute("RELEASE v6_capture_brief")
    if planned is None:
        return None
    run, goal, prior, head, cursor, key, brief = planned
    with store.transaction(
        conn, transaction_owner="ScientificLead", operation="freeze_brief", run_id=run_id
    ) as tx:
        current = tx.execute(
            "SELECT g.goal_version,g.status,r.status AS run_status FROM v6_goals g JOIN runs r "
            "USING(run_id) WHERE g.run_id=?",
            (run_id,),
        ).fetchone()
        latest = _latest(tx, run_id)
        if (
            (latest["decision_ref"] if latest else None) != (prior["decision_ref"] if prior else None)
            or current["goal_version"] != goal["goal_version"]
            or current["status"] != "OPEN"
            or current["run_status"] != "ACTIVE"
            or v6_decisions.head(tx, run_id) != head
            or _open(tx, run_id)
            or tx.execute(
                "SELECT 1 FROM events WHERE run_id=? AND kind='v6.intent.changed' AND event_id>? LIMIT 1",
                (run_id, cursor),
            ).fetchone()
        ):
            return None
        return persist_brief(
            tx, run_id, "run", brief, head, goal["goal_version"], key, cursor, run["budget_ref"]
        )


def _prepare_brief(conn, run_id):
    from . import v6_decisions

    run = conn.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    goal = conn.execute("SELECT * FROM v6_goals WHERE run_id=?", (run_id,)).fetchone()
    prior = _latest(conn, run_id)
    last = prior["event_cursor"] if prior else 0
    events = _events(conn, run_id, last)
    changed = (
        conn.execute(
            "SELECT 1 FROM v6_decision_tasks WHERE run_id=? AND scope='run' AND goal_version=? LIMIT 1",
            (run_id, goal["goal_version"]),
        ).fetchone()
        is None
    )
    branch_hits = _branch_watch_hits(conn, run_id, goal["goal_version"], events)
    if prior and not changed and not branch_hits and not _run_wakes(conn, run_id, prior, events):
        return None
    head = v6_decisions.head(conn, run_id)
    cursor = events[-1]["event_id"] if events else last
    key = ids.sha256_of_obj([goal["goal_version"], head, cursor])
    if conn.execute(
        "SELECT 1 FROM v6_decision_tasks WHERE run_id=? AND scope='run' AND event_key=?", (run_id, key)
    ).fetchone():
        return None
    brief = _brief(conn, run, goal, head, events, cursor, prior)
    for change in brief["trigger"]["changes"]:
        hits = branch_hits.get(change["event_id"], [])
        if hits:
            change["branch_watches"] = hits
            brief["evidence_refs"].extend(hit["decision_ref"] for hit in hits)
    brief["evidence_refs"] = list(dict.fromkeys(brief["evidence_refs"]))
    return run, goal, prior, head, cursor, key, brief


def persist_brief(tx, run_id, scope, brief, head, goal_version, event_key, cursor, budget_ref):
    """Save one scoped delegation and its frozen context under its caller's CAS transaction."""
    if not tx.in_transaction:
        raise RuntimeError("Freezing a decision task requires its owning transaction")
    previous = tx.execute("SELECT * FROM research_context_heads WHERE run_id=?", (run_id,)).fetchone()
    ref = ids.new_id("research_context")
    version = previous["version"] + 1 if previous else 1
    tx.execute(
        "INSERT INTO research_contexts(context_ref,run_id,version,previous_context_ref,snapshot_json,"
        "sha256,event_cursor,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (
            ref,
            run_id,
            version,
            previous["context_ref"] if previous else None,
            store._j(brief),
            ids.sha256_of_obj(brief),
            cursor,
            ACTOR,
            clock.now_iso(),
        ),
    )
    tx.execute(
        "INSERT INTO research_context_heads(run_id,context_ref,version) VALUES(?,?,?) "
        "ON CONFLICT(run_id) DO UPDATE SET context_ref=excluded.context_ref,version=excluded.version",
        (run_id, ref, version),
    )
    tid = records.create_task(
        tx,
        run_id=run_id,
        kind="decide_revision",
        role="planner",
        actor=ACTOR,
        budget_ref=budget_ref,
        task_input={"context_ref": ref, "scope": scope, "brief": brief},
    )
    tx.execute(
        "INSERT INTO v6_decision_tasks(task_id,run_id,scope,context_ref,goal_version,base_json,"
        "visible_refs_json,event_key) VALUES(?,?,?,?,?,?,?,?)",
        (tid, run_id, scope, ref, goal_version, store._j(head), store._j(brief["evidence_refs"]), event_key),
    )
    store.transition(
        tx,
        machine="task",
        subject_id=tid,
        to_state="READY",
        actor=ACTOR,
        run_id=run_id,
        reason="Frozen Lead brief is ready",
        evidence_level="EXECUTION_OBSERVED",
    )
    return tid


def cancel_decision(conn, rid, tid, *, reason):
    for lease in conn.execute("SELECT lease_id FROM leases WHERE task_id=? AND released_at IS NULL", (tid,)):
        store.release_lease(conn, lease[0])
    store.transition(
        conn,
        machine="task",
        subject_id=tid,
        to_state="CANCELLED",
        actor=ACTOR,
        run_id=rid,
        reason=reason,
        evidence_level="EXECUTION_OBSERVED",
    )


def impact(conn, rid, edits, *, commit):
    """Fence the previous brief and unaccepted delegations in the PI's Intent transaction."""
    goal = conn.execute("SELECT * FROM v6_goals WHERE run_id=?", (rid,)).fetchone()
    if goal is None:  # initial operator bootstrap inside run creation
        return []
    objective = next((e for e in edits if e["item_id"] == "objective"), None)
    nodes = []
    if objective:
        nodes = conn.execute(
            "SELECT node_id,status FROM v6_nodes WHERE run_id=? AND job_id IS NULL "
            "AND status='PENDING' ORDER BY node_id",
            (rid,),
        ).fetchall()
    report = [
        {
            "object_type": "node",
            "object_id": n["node_id"],
            "status": n["status"],
            "action": "withdraw",
            "consequence": "This unsubmitted delegation will be withdrawn.",
        }
        for n in nodes
    ]
    open_tasks = conn.execute(
        "SELECT t.task_id FROM v6_decision_tasks d JOIN tasks t USING(task_id) "
        "WHERE d.run_id=? AND t.status NOT IN ('CANCELLED','ACCEPTED') ORDER BY t.task_id",
        (rid,),
    ).fetchall()
    report.extend(
        {
            "object_type": "task",
            "object_id": t["task_id"],
            "action": "interrupt",
            "consequence": "The previous Scientific Lead brief will be superseded.",
        }
        for t in open_tasks
    )
    if not commit:
        return report
    if objective:
        if not isinstance(objective["new_value"], str) or not objective["new_value"].strip():
            raise SchemaInvalid("An active research Goal needs a nonempty objective")
        conn.execute(
            "UPDATE v6_goals SET goal=?,goal_version=? WHERE run_id=?",
            (objective["new_value"], objective["new_version"], rid),
        )
        conn.executemany(
            "UPDATE v6_nodes SET status='CANCEL_REQUESTED' WHERE node_id=? AND status='PENDING' "
            "AND job_id IS NULL",
            [(n["node_id"],) for n in nodes],
        )
    for task in open_tasks:
        cancel_decision(conn, rid, task["task_id"], reason="Formal Intent superseded the decision brief")
    store.append_event(
        conn,
        run_id=rid,
        kind="v6.intent.changed",
        subject_kind="run",
        subject_id=rid,
        actor="operator",
        evidence_level="EXECUTION_OBSERVED",
        payload={"items": [{"item_id": e["item_id"], "version": e["new_version"]} for e in edits]},
    )
    return report


def waiting_for(conn, rid):
    marks = ",".join("?" for _ in ACTIVE_TASKS)
    open_tasks = [
        dict(row)
        for row in conn.execute(
            "SELECT d.task_id, d.scope, d.context_ref, t.status FROM v6_decision_tasks d JOIN tasks t "
            f"USING(task_id) WHERE d.run_id=? AND t.status IN ({marks}) ORDER BY t.rowid",
            (rid, *ACTIVE_TASKS),
        )
    ]
    if open_tasks:
        return {"kind": "decision", "tasks": open_tasks, "continue_with": "gx-p0 run"}
    nodes = [
        dict(row)
        for row in conn.execute(
            "SELECT n.node_id,n.branch_id,n.status,n.job_id,w.wait_json "
            "FROM v6_nodes n LEFT JOIN v6_node_waits w USING(node_id) WHERE n.run_id=? "
            "AND n.status IN ('PENDING','RUNNING','CANCEL_REQUESTED') ORDER BY n.rowid",
            (rid,),
        )
    ]
    if nodes:
        for node in nodes:
            reason = node.pop("wait_json")
            if reason:
                node["wait"] = json.loads(reason)
        return {"kind": "nodes", "nodes": nodes, "continue_with": "gx-p0 run"}
    prior = _latest(conn, rid)
    if prior:
        version = conn.execute("SELECT goal_version FROM v6_goals WHERE run_id=?", (rid,)).fetchone()[0]
        events = _events(conn, rid, prior["event_cursor"])
        watches = store.loads(prior, "receipt_json")["reconsider_when"]
        if _run_wakes(conn, rid, prior, events) or _branch_watch_hits(conn, rid, version, events):
            return {"kind": "research_event", "through_cursor": events[-1]["event_id"],
                    "decision_ref": prior["decision_ref"], "watches": watches, "continue_with": "gx-p0 run"}
        return {
            "kind": "operator",
            "reason": "No runnable research node or decision remains. The persisted watches await new input.",
            "decision_ref": prior["decision_ref"],
            "watches": watches,
            "continue_with": "gx-p0 message --supplement <new evidence>, or revise the formal Goal",
        }
    return None


def projection(conn, run, meter):
    from . import v6_claims, v6_costs, v6_decisions

    rid = run["run_id"]
    g = conn.execute("SELECT * FROM v6_goals WHERE run_id=?", (rid,)).fetchone()
    return {
        "mode": "goal",
        "goal": g["goal"],
        "goal_version": g["goal_version"],
        "graph_head": v6_decisions.head(conn, rid),
        "goal_status": g["status"],
        "waiting_for": waiting_for(conn, rid) if run["status"] == "ACTIVE" else None,
        "capabilities": store.loads(g, "capabilities_json"),
        "materials": store.loads(g, "materials_json"),
        "resources": v6_costs.resource_report(conn, rid, meter=meter),
        "claims": v6_claims.current(conn, rid),
        "decisions": [
            {
                **store.loads(r, "receipt_json"),
                "rationale": r["rationale"],
                "evidence_refs": json.loads(r["refs"]),
            }
            for r in conn.execute(
                "SELECT receipt_json,json_extract(request_json,'$.decision.rationale') AS rationale,"
                "json_extract(request_json,'$.decision.evidence_refs') AS refs "
                "FROM v6_decisions WHERE run_id=? ORDER BY rowid",
                (rid,),
            )
        ],
        "node_attempts": [
            dict(r)
            for r in conn.execute(
                "SELECT node_id,branch_id,version,status,operation_key,job_id,result_ref,exec_key FROM "
                "v6_nodes WHERE run_id=? ORDER BY rowid",
                (rid,),
            )
        ],
    }
