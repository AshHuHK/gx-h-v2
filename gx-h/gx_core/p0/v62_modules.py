"""Durable research modules use many ordinary, immutable Goal decision tasks."""

import json
from dataclasses import dataclass

from .. import budgets
from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import (
    ArtifactHashMismatch,
    BudgetExceeded,
    GxError,
    IdempotencyConflict,
    SchemaInvalid,
    ScopeDenied,
    StaleRevision,
)
from ..orchestration import seams
from ..persistence import store
from . import research_dialogue, v6_decisions, v6_goal, v6_jobs, v62b_handoff

OWNER = "ResearchModule"


def _module(conn, run_id, scope):
    row = conn.execute("SELECT * FROM v62_modules WHERE run_id=? AND scope=?", (run_id, scope)).fetchone()
    if row is None:
        raise ScopeDenied("This Run has no such delegated module")
    return row


def _list(value, name):
    if (
        not isinstance(value, list)
        or any(not isinstance(v, str) or not v.strip() for v in value)
        or len(set(value)) != len(value)
    ):
        raise SchemaInvalid(f"{name} must be a list of distinct strings")
    return value


def _strings(payload, keys):
    if any(not isinstance(payload.get(k), str) or not payload[k].strip() for k in keys):
        raise SchemaInvalid("Required module fields are nonempty strings: " + ", ".join(keys))


def _goal(conn, rid):
    return conn.execute("SELECT * FROM v6_goals WHERE run_id=?", (rid,)).fetchone()


def _assert_sources(conn, run_id, goal_version, grant, snapshot, *, for_projection=False, opening=False):
    from .manifest import check_capability

    goal = _goal(conn, run_id)
    objective = conn.execute(
        "SELECT version,status FROM p0_intent_versions "
        "WHERE run_id=? AND item_id='objective' ORDER BY version DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if (
        not goal
        or (not for_projection and goal["status"] != "OPEN")
        or goal["goal_version"] != goal_version
        or not objective
        or tuple(objective) != (goal_version, "active")
    ):
        raise StaleRevision("The objective or version of the module delegation changed")
    inputs = json.loads(
        conn.execute("SELECT inputs_json FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
    )["inputs"]
    catalog = inputs.get("research_catalog") or {}
    if not any(
        r["module_id"] == snapshot["module_id"]
        and r["version"] == snapshot["module_version"]
        and r["plugin"] == grant["plugin"]
        and r["declaration_sha256"] == grant["declaration_sha256"]
        for r in catalog.get("modules", [])
    ):
        raise StaleRevision("The research module declaration is no longer in this Run's grant")
    live_inputs = set(json.loads(goal["materials_json"]))
    live_methods = json.loads(goal["capabilities_json"])
    v62b_handoff.require_materials(conn, run_id, live_inputs, snapshot["materials"])
    v62b_handoff.require_claims(conn, run_id, snapshot.get("target_claims", ()))
    from . import capability_views
    for cap, pin in snapshot["method_pins"].items():
        if not opening and cap not in live_methods and capability_views.latest(conn, run_id):
            # A disabled method closes future admission. The module can still read
            # an already admitted result, report it, or use its other granted methods.
            continue
        if live_methods.get(cap) != pin:
            raise StaleRevision("A delegated method pin changed")
        try:
            check_capability(conn, run_id, cap)
        except GxError as exc:
            raise StaleRevision("A delegated method is no longer authorized") from exc
    if v6_decisions._capability_pins(conn, grant["capabilities"]) != {
        k: tuple(v) if v is not None else None for k, v in snapshot["asset_pins"].items()
    }:
        raise StaleRevision("A delegated method's registered version or approval changed")


def require_active(conn, run_id, scope):
    row = _module(conn, run_id, scope)
    if row["status"] != "OPEN":
        raise ScopeDenied(
            "This module is no longer open", details={"scope": scope, "status": row["status"]}
        )
    _assert_sources(
        conn, run_id, row["goal_version"], json.loads(row["grant_json"]), json.loads(row["snapshot_json"])
    )
    return row


def _nodes(conn, row):
    return [
        dict(n)
        for n in conn.execute(
            """SELECT n.node_id,n.branch_id,n.version,n.status,n.job_id,n.result_ref,n.exec_key,a.reused_from
           FROM v6_nodes n LEFT JOIN v6_job_attempts a USING(node_id)
           WHERE n.run_id=? AND n.branch_id=? ORDER BY n.rowid""",
            (row["run_id"], row["branch_id"]),
        )
    ]


def _operations(conn, row):
    from .v62_native import operation_receipts

    return operation_receipts(conn, row["run_id"], row["module_instance_id"])


def visible_refs(conn, row, *, verified_jobs=None):
    from . import v621_assignment
    refs = list(json.loads(row["grant_json"])["input_refs"])
    refs.extend(m["artifact_ref"] for m in v621_assignment.for_module(conn, row))
    refs.extend(n["result_ref"] for n in _nodes(conn, row) if n["result_ref"])
    if verified_jobs is None:
        # Authority is attached to the collector's frozen task, including after a cold claim.
        # The grant and the current short task/lease are checked by the native tool owner.
        context = conn.execute(
            "SELECT c.snapshot_json FROM v6_decision_tasks d JOIN research_contexts c USING(context_ref) "
            "WHERE d.run_id=? AND d.scope=? ORDER BY d.rowid DESC LIMIT 1", (row["run_id"], row["scope"])
        ).fetchone()
        verified_jobs = json.loads(context[0]).get("verified_jobs", ()) if context else ()
    for job in verified_jobs:
        refs.extend(item["artifact_ref"] for item in job.get("outputs", ()))
    for operation in _operations(conn, row):
        refs.extend(operation.get("artifact_refs", []))
    return list(dict.fromkeys(refs))


def recovery_context(conn, run_id, packet):
    """Rebuild confirmed local tool receipts for a replacement lease without rewriting its original context."""
    module = require_active(conn, run_id, packet["scope"])
    return {**packet, "operations": _operations(conn, module), "evidence_refs": visible_refs(conn, module)}


def job_outputs(conn, run_id, node, execution):
    """Select only the actual, bound scientific publications of this node's sealed Job."""
    from . import v621_policy, v62_outputs

    if v621_policy.enabled(conn, run_id):
        actual, _logs = v62_outputs.recorded_outputs(
            conn, run_id, node["job_id"], record=execution, check_publications=True
        )
        if any(item.get("runner_sha256", item["sha256"]) != item["sha256"] for item in actual):
            raise ArtifactHashMismatch("A published Job output differs from its runner receipt")
        cards = [{k: item[k] for k in ("output_name", "artifact_ref", "sha256", "kind", "nature")} for item in actual]
        declarations = {d["name"]: d for d in execution["admission"].get("output_declarations", ())}
        for item, card in zip(actual, cards):
            declared = declarations.get(item.get("declaration_name", item["output_name"]), {})
            if declared.get("metric_specs"):
                card["metric_ids"] = sorted(declared["metric_specs"])
    else:
        cards = [{"artifact_ref": item["artifact_ref"], "sha256": item["sha256"]}
                 for item in execution["completion"]["outputs"] if item["artifact_ref"] == node["result_ref"]]
    if not any(item["artifact_ref"] == node["result_ref"] for item in cards):
        raise StaleRevision("The completed module Job has no bound primary result")
    return cards


def _verified_jobs(conn, module, nodes):
    """Validate previous Job identities, recorded environment, inputs and completed bytes outside the writer."""
    from . import manifest

    artifacts = ArtifactService(conn)
    proofs = []
    for node in nodes:
        if node["status"] not in ("SUCCEEDED", "REUSED"):
            continue
        job_id, ref = node["job_id"], node["result_ref"]
        evidence = manifest.read_manifest(conn, job_id)
        identity = v6_jobs.confirmed_result_identity(conn, node["node_id"], evidence, verify_inputs=True)
        outputs = job_outputs(conn, module["run_id"], node, evidence)
        for output in outputs:
            checked = artifacts.verify(output["artifact_ref"])
            if not checked["ok"] or checked["observed_sha256"] != output["sha256"]:
                raise StaleRevision("An artifact in a confirmed module Job changed")
        primary = next(item for item in outputs if item["artifact_ref"] == ref)
        manifest.require_current(conn, job_id)
        proofs.append(
            dict(
                node_id=node["node_id"],
                job_id=job_id,
                **identity,
                artifact_ref=ref,
                output_sha256=primary["sha256"],
                **({"outputs": outputs} if "output_name" in primary else {}),
                admission_sha256=evidence["admission_hash"],
                completion_sha256=evidence["manifest_hash"],
            )
        )
    return proofs


def _task(conn, row):
    return conn.execute(
        """SELECT t.task_id,t.status,t.attempt,t.lease_id,d.context_ref
        FROM v6_decision_tasks d JOIN tasks t USING(task_id)
        WHERE d.run_id=? AND d.scope=? ORDER BY d.rowid DESC LIMIT 1""",
        (row["run_id"], row["scope"]),
    ).fetchone()


def _brief(conn, row, nodes, proofs):
    from ..jobs import diagnostics
    from . import v621_assignment
    nodes = [dict(n) for n in nodes]
    for node in nodes:
        diagnostics.attach_to_node(conn, row["run_id"], node)
    grant, snapshot = (json.loads(row[k]) for k in ("grant_json", "snapshot_json"))
    fields = (
        "module_instance_id",
        "module_id",
        "module_version",
        "round",
        "scope",
        "branch_id",
        "focus",
        "expected_delivery",
        "goal_version",
    )
    module = {k: row[k] for k in fields}
    funds = budgets.state(conn, row["budget_ref"])
    operations = _operations(conn, row)
    # A new decision receives confirmed references and field summaries, never another scope's materials.
    from . import v621_policy
    policy = v621_policy.pinned(conn, row["run_id"])
    from . import capability_views
    revision = capability_views.latest(conn, row["run_id"])
    methods = [c for c in grant["capabilities"] if revision is None or c in revision["active"]]
    dialogue = research_dialogue.packet(conn, row["run_id"], row["scope"], row["goal_version"])
    from . import research_handoff
    handoff = research_handoff.active(conn, row["run_id"])
    return {
        **({"pi_dialogue": dialogue} if dialogue else {}),
        **({"researcher_handoff": True, "peer_hints": research_handoff.inbox(conn, row["run_id"], row["scope"])}
           if handoff else {}),
        "run_id": row["run_id"],
        **({"evidence_policy": policy} if policy in v621_policy.GOVERNED else {}),
        "scope": row["scope"],
        "goal": snapshot["goal"],
        "module": module,
        "module_id": row["module_id"],
        "module_version": row["module_version"],
        "goal_version": row["goal_version"],
        "head": v6_decisions.head(conn, row["run_id"]),
        "intent": snapshot["intent"],
        "module_instructions": snapshot["instructions"],
        "expected_delivery": row["expected_delivery"],
        "allowed_tools": grant["tools"],
        "allowed_method_ids": methods,
        "materials": [{k: v for k, v in item.items() if k != "publication"} for item in
                      [*snapshot["materials"], *v621_assignment.for_module(conn, row)]],
        **({"catalog_access": grant["catalog_access"]} if "catalog_access" in grant else {}),
        "capabilities": [c for c in snapshot["capabilities"] if c["capability_version"] in methods],
        **({"capability_view":{"revision":revision["revision"], "digest":revision["digest"]}} if revision else {}),
        "budget": {k: funds[k] for k in ("limits", "spent", "reserved", "available")},
        "branches": [{"branch_id": row["branch_id"], "status": "OPEN", "budget_limits": funds["limits"]}],
        "nodes": nodes,
        "claims": [{k: target[k] for k in ("claim_revision_id", "statement", "scope")}
                   for target in snapshot.get("target_claims", ())],
        **({"target_claim_refs": grant["target_claim_refs"]} if grant.get("target_claim_refs") else {}),
        "operations": operations,
        "observations": [n for n in nodes if n["result_ref"]],
        "evidence_refs": visible_refs(conn, row, verified_jobs=proofs),
    }


def _new_task(tx, row, *, previous=None, verified_nodes=None, proofs=()):
    rid, scope = row["run_id"], row["scope"]
    if research_dialogue.blocked(tx, rid, scope=scope):
        return None
    current = _task(tx, row)
    if current and (current["status"] in v6_goal.ACTIVE_TASKS or current["task_id"] != previous):
        return None
    nodes = _nodes(tx, row)
    if any(n["status"] not in v6_jobs.NODE_TERMINAL for n in nodes):
        return None
    if verified_nodes is not None and verified_nodes != nodes:
        return None
    brief = _brief(tx, row, nodes, proofs)
    brief["verified_jobs"] = list(proofs)
    cursor = tx.execute("SELECT coalesce(max(event_id),0) FROM events WHERE run_id=?", (rid,)).fetchone()[0]
    key = "v62.task:" + ids.sha256_of_obj([row["module_instance_id"], previous])
    tid = v6_goal.persist_brief(
        tx, rid, scope, brief, brief["head"], row["goal_version"], key, cursor, row["budget_ref"]
    )
    seams.charge_worker_task(tx, budget_ref=row["budget_ref"], task_id=tid, actor=OWNER)
    return _task(tx, row)


@dataclass(frozen=True)
class Opening:
    identity: dict
    payload: dict
    request_sha: str
    grant: dict
    snapshot: dict
    policy: dict
    context_ref: str
    goal_version: int


def _lead(conn, identity):
    _, task, goal, _ = v6_decisions.authority(conn, **identity)
    if task["scope"] != "run":
        raise ScopeDenied("A Run Lead decision is required to manage delegated modules")
    return task, goal


def _open_authority(conn, identity, key, digest):
    """Only the acknowledged parent may replay its call after its assignment ends."""
    row = conn.execute(
        "SELECT request_sha256,receipt_json FROM v62_modules WHERE run_id=? AND open_key=?",
        (identity["run_id"], key),
    ).fetchone()
    receipt = json.loads(row["receipt_json"]) if row else None
    original = receipt is not None and all(
        receipt[field] == identity[name] for field, name in (
            ("parent_task_id", "task_id"), ("parent_lease_id", "lease_id"),
            ("thread_id", "thread_id"), ("turn_id", "turn_id")
        )
    )
    task, goal = (None, None) if original else _lead(conn, identity)
    if row and row["request_sha256"] != digest:
        raise IdempotencyConflict("Module operation key was already used with different content")
    return receipt, task, goal


def prepare_open(conn, *, run_id, task_id, lease_id, thread_id, turn_id, call_id, payload):
    from . import v62_catalog, v62_rounds

    if conn.in_transaction:
        raise RuntimeError("Prepare module inputs and provider declaration before acquiring the writer")
    required = {
        "module_id",
        "module_version",
        "focus",
        "expected_delivery",
        "round",
        "input_refs",
        "capabilities",
        "sub_budget",
        "operation_key",
    }
    if (
        not isinstance(payload, dict)
        or not required <= payload.keys()
        or payload.keys() - required - {"wake_on", "tools", "target_claim_refs"}
    ):
        raise SchemaInvalid("Invalid fields for a module delegation")
    _strings(payload, ["module_id", "module_version", "focus", "expected_delivery", "operation_key"])
    refs = _list(payload["input_refs"], "input_refs")
    caps = _list(payload["capabilities"], "capabilities")
    target_refs = _list(payload.get("target_claim_refs", []), "target_claim_refs")
    watches = _list(payload.get("wake_on", []), "wake_on")
    if set(watches) - {"counterevidence", "gap", "partial"}:
        raise SchemaInvalid("Unknown module early-wake condition")
    if type(payload["round"]) is not int or payload["round"] < 1:
        raise SchemaInvalid("A research round must be a positive integer")
    budgets.check_amounts(payload["sub_budget"], what="module sub_budget")
    ident = dict(run_id=run_id, task_id=task_id, lease_id=lease_id, thread_id=thread_id, turn_id=turn_id)
    sha = ids.sha256_of_obj(payload)
    prior, task, goal = _open_authority(conn, ident, payload["operation_key"], sha)
    if prior is not None:
        return prior
    v62_rounds.require_exploration(conn, run_id)
    policy = v62_catalog.policy(conn, run_id)
    if policy["effort"] == "quick" or payload["round"] > policy["max_rounds"]:
        raise ScopeDenied("This research effort does not grant this module round")
    decl = v62_catalog.resolve(conn, run_id, payload["module_id"], payload["module_version"])
    tools = _list(payload["tools"], "tools") if "tools" in payload else decl["tools"]
    if set(tools) - set(decl["tools"]):
        raise ScopeDenied("The delegated module requests a tool outside its declaration")
    if "gx_submit_module_report" not in tools:
        raise SchemaInvalid("A delegated research worker requires its report submission route")
    frozen = json.loads(task["snapshot_json"])
    if set(caps) - set(decl["capabilities"]):
        raise ScopeDenied("Requested module methods exceed the parent and declaration grants")
    materials = v62b_handoff.select_materials(conn, task, frozen, refs, decl["input_roles"],
                                             published_only=decl.get("published_inputs_only", False))
    if "gx_review_evidence" in tools and frozen.get("catalog_discovery"):
        from . import research_handoff
        research_handoff.review_materials(conn, run_id, materials)
    targets = v62b_handoff.select_claims(conn, task, frozen, target_refs)
    from ..plugins import goal as providers

    methods = {
        m["capability_version"]: v62_catalog._method_summary(m) for m in providers.catalog(conn, run_id)
    }
    if set(caps) - set(methods):
        raise ScopeDenied("Some delegated methods are absent from the frozen Lead brief")
    grant = dict(
        input_refs=[material["artifact_ref"] for material in materials],
        capabilities=caps,
        tools=tools,
        plugin=decl["plugin"],
        declaration_sha256=decl["declaration_sha256"],
    )
    if decl.get("method_profiles"):
        grant["method_profiles"] = decl["method_profiles"]
    if decl.get("catalog_access") == "public_methods" and "gx_query_catalog" in tools:
        grant["catalog_access"] = "public_methods"
    if targets:
        grant["target_claim_refs"] = [target["claim_revision_id"] for target in targets]
    snapshot = dict(
        module_id=payload["module_id"],
        module_version=payload["module_version"],
        materials=materials,
        capabilities=[methods[c] for c in caps],
        method_pins={k: json.loads(goal["capabilities_json"])[k] for k in caps},
        asset_pins=v6_decisions._capability_pins(conn, caps),
        goal=frozen["goal"],
        intent={k: v for k, v in frozen["intent"].items() if k == "objective"},
        instructions=decl["instructions"],
        opened_base=frozen["head"],
    )
    if targets:
        snapshot["target_claims"] = targets
    return Opening(
        dict(ident, call_id=call_id),
        json.loads(store._j(payload)),
        sha,
        grant,
        snapshot,
        policy,
        task["context_ref"],
        task["goal_version"],
    )


def commit_open(conn, plan):
    from . import v62_budget, v62_rounds, v621_goal_scope

    if isinstance(plan, dict):
        return plan
    ident = dict(plan.identity)
    call_id = ident.pop("call_id")
    p, rid = plan.payload, ident["run_id"]
    with store.transaction(conn, transaction_owner=OWNER, operation="open_module", run_id=rid) as tx:
        prior, parent, goal = _open_authority(tx, ident, p["operation_key"], plan.request_sha)
        if prior is not None:
            return prior
        v62_rounds.require_exploration(tx, rid)
        if parent["context_ref"] != plan.context_ref:
            raise StaleRevision("The delegating context changed")
        _assert_sources(tx, rid, plan.goal_version, plan.grant, plan.snapshot, opening=True)
        version = v621_goal_scope.active(tx, rid)
        if (
            tx.execute(
                "SELECT count(*) FROM v62_modules WHERE run_id=? AND status='OPEN' "
                "AND (? IS NULL OR goal_version=?)", (rid, version, version)
            ).fetchone()[0]
            >= plan.policy["max_parallel"]
        ):
            raise BudgetExceeded("The maximum concurrent module count was reached")
        latest = tx.execute(
            "SELECT round,parent_task_id FROM v62_modules WHERE run_id=? AND (? IS NULL OR goal_version=?) "
            "ORDER BY round DESC LIMIT 1",
            (rid, version, version),
        ).fetchone()
        if latest and (p["round"] < latest["round"] or
                       (p["round"] == latest["round"] and ident["task_id"] != latest["parent_task_id"])):
            raise StaleRevision("This module round has already been sealed")
        instance = "module_" + ids.sha256_of_obj([rid, p["operation_key"]])[:32]
        scope, branch = "module:" + instance, "branch_" + ids.sha256_of_obj([rid, instance])[:32]
        account = v62_budget.open_child(tx, rid, instance, p["sub_budget"], ident["thread_id"])
        tx.execute(
            """INSERT INTO v62_modules(module_instance_id,run_id,scope,branch_id,module_id,module_version,
            goal_version,round,focus,expected_delivery,status,parent_task_id,parent_context_ref,open_key,
            request_sha256,receipt_json,grant_json,budget_ref,budget_json,snapshot_json,wake_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,'OPEN',?,?,?,?,?,?,?,?,?,?)""",
            (
                instance,
                rid,
                scope,
                branch,
                p["module_id"],
                p["module_version"],
                plan.goal_version,
                p["round"],
                p["focus"],
                p["expected_delivery"],
                ident["task_id"],
                plan.context_ref,
                p["operation_key"],
                plan.request_sha,
                "{}",
                store._j(plan.grant),
                account,
                store._j(p["sub_budget"]),
                store._j(plan.snapshot),
                store._j(p.get("wake_on", [])),
            ),
        )
        task = _new_task(tx, _module(tx, rid, scope))
        answer = dict(
            module_instance_id=instance,
            module_id=p["module_id"],
            module_version=p["module_version"],
            scope=scope,
            run_id=rid,
            round=p["round"],
            goal_version=goal["goal_version"],
            branch_id=branch,
            operation_key=p["operation_key"],
            task_id=task["task_id"],
            context_ref=task["context_ref"],
            parent_task_id=ident["task_id"],
            parent_context_ref=plan.context_ref,
            parent_lease_id=ident["lease_id"],
            thread_id=ident["thread_id"],
            turn_id=ident["turn_id"],
            call_id=call_id,
            budget_ref=account,
            effective_grant=plan.grant,
            request_sha256=plan.request_sha,
        )
        tx.execute(
            "UPDATE v62_modules SET receipt_json=? WHERE module_instance_id=?", (store._j(answer), instance)
        )
        store.append_event(
            tx,
            run_id=rid,
            kind="v62.module.opened",
            actor=ident["thread_id"],
            subject_kind="module",
            subject_id=instance,
            payload=answer,
            evidence_level="EXECUTION_OBSERVED",
        )
        return answer


def open_module(conn, **native):
    return commit_open(conn, prepare_open(conn, **native))


def _end(tx, row, status, reason):
    from . import v62_budget, v62_reports, v621_policy

    if row["status"] != "OPEN":
        return False
    instance, rid = row["module_instance_id"], row["run_id"]
    tx.execute("UPDATE v62_modules SET status=? WHERE module_instance_id=?", (status, instance))
    task = _task(tx, row)
    if task and task["status"] in v6_goal.ACTIVE_TASKS:
        v6_goal.cancel_decision(tx, rid, task["task_id"], reason=reason)
    # The original Job owner reconciles any accepted external execution and its cost.
    tx.execute(
        "UPDATE v6_nodes SET status='CANCEL_REQUESTED' WHERE run_id=? AND branch_id=? "
        "AND status IN ('PENDING','RUNNING')",
        (rid, row["branch_id"]),
    )
    store.append_event(
        tx,
        run_id=rid,
        kind="v62.module.invalidated" if status == "STALE" else "v62.module.cancelled",
        actor=OWNER,
        subject_kind="module",
        subject_id=instance,
        prev_state="OPEN",
        new_state=status,
        reason=reason,
        payload={
            "module_instance_id": instance,
            "scope": "run",
            "module_scope": row["scope"],
            **({"goal_version": row["goal_version"]} if v621_policy.enabled(tx, rid) else {}),
            "reason": reason,
        },
    )
    v62_reports.release_round(tx, row)
    v62_budget.close_if_settled(tx, row["budget_ref"])
    return True


def cancel_for_graph(conn, run_id, branch_id=None, *, reason):
    if not conn.in_transaction:
        raise RuntimeError("Graph withdrawal and module withdrawal share a transaction")
    rows = conn.execute(
        "SELECT * FROM v62_modules WHERE run_id=? AND status='OPEN' " "AND (? IS NULL OR branch_id=?)",
        (run_id, branch_id, branch_id),
    ).fetchall()
    for row in rows:
        _end(conn, row, "CANCELLED", reason)


def cancel_module(conn, *, run_id, task_id, lease_id, thread_id, turn_id, call_id, payload):
    if not isinstance(payload, dict) or set(payload) != {"module_instance_id", "operation_key", "reason"}:
        raise SchemaInvalid("Module cancellation needs its instance, reason and operation key")
    _strings(payload, list(payload))
    ident = dict(run_id=run_id, task_id=task_id, lease_id=lease_id, thread_id=thread_id, turn_id=turn_id)
    key = "v62.cancel:" + ids.sha256_of_obj([run_id, payload["operation_key"]])
    with store.transaction(conn, transaction_owner=OWNER, operation="cancel_module", run_id=run_id) as tx:
        _lead(tx, ident)
        record = store.begin_idempotent(tx, key, run_id, payload)
        if record.replay:
            return record.response
        row = _module(tx, run_id, "module:" + payload["module_instance_id"])
        if not _end(tx, row, "CANCELLED", payload["reason"]):
            raise ScopeDenied("Only an open module can be cancelled")
        receipt = {
            **payload,
            "scope": row["scope"],
            "run_id": run_id,
            "status": "CANCELLED",
            "thread_id": thread_id,
            "turn_id": turn_id,
            "call_id": call_id,
            "task_id": task_id,
            "lease_id": lease_id,
        }
        store.complete_idempotent(tx, key, receipt)
        return receipt


def _progress(conn, row):
    from . import v62_budget, v62_reports

    rid, scope = row["run_id"], row["scope"]
    try:
        require_active(conn, rid, scope)
    except (StaleRevision, ScopeDenied):
        with store.transaction(
            conn, transaction_owner=OWNER, operation="reconcile_module_grant", run_id=rid
        ) as tx:
            try:
                require_active(tx, rid, scope)
            except StaleRevision as confirmed:
                return _end(tx, _module(tx, rid, scope), "STALE", str(confirmed))
            except ScopeDenied:
                return False
    if research_dialogue.blocked(conn, rid, scope=scope):
        return False
    task = _task(conn, row)
    reason = v62_budget.exhausted(conn, row) or v62_reports.exhausted_report_repairs(conn, task)
    if not reason and task and task["status"] in v6_goal.ACTIVE_TASKS:
        return False
    if not reason and any(n["status"] not in v6_jobs.NODE_TERMINAL for n in _nodes(conn, row)):
        return False
    if not reason and v62_budget.remaining(conn, row["budget_ref"]).get("worker_tasks", 0) < 1:
        reason = "module worker task allowance exhausted"
    nodes = _nodes(conn, row)
    try:
        if reason:
            return bool(v62_reports.partial(conn, row, reason))
        proofs = _verified_jobs(conn, row, nodes)
    except (StaleRevision, SchemaInvalid, ArtifactHashMismatch) as exc:
        with store.transaction(
            conn, transaction_owner=OWNER, operation="mark_module_evidence_stale", run_id=rid
        ) as tx:
            return _end(tx, _module(tx, rid, scope), "STALE", str(exc))
    with store.transaction(conn, transaction_owner=OWNER, operation="continue_module", run_id=rid) as tx:
        current = _module(tx, rid, scope)
        if current["status"] != "OPEN":
            return False
        try:
            require_active(tx, rid, scope)
        except StaleRevision as exc:
            return _end(tx, current, "STALE", str(exc))
        return bool(
            _new_task(
                tx, current, previous=task["task_id"] if task else None, verified_nodes=nodes, proofs=proofs
            )
        )


def advance(conn, run_id):
    """Reconcile admitted work before funding a fresh short decision or a model-free handback."""
    from . import v62_budget, v62_reports

    changed = False
    rows = conn.execute(
        """SELECT m.* FROM v62_modules m JOIN runs r USING(run_id)
                           WHERE m.run_id=? AND m.status='OPEN' AND r.status='ACTIVE' ORDER BY m.rowid""",
        (run_id,),
    ).fetchall()
    for row in rows:
        changed = _progress(conn, row) or changed
    changed = v62_reports.reconcile_rounds(conn, run_id) or changed
    return v62_budget.release_finished(conn, run_id) or changed


def submit_report(conn, **native):
    from .v62_reports import submit_report

    return submit_report(conn, **native)


def report_projection(conn, run_id, **limits):
    from .v62_reports import report_projection

    return report_projection(conn, run_id, **limits)
