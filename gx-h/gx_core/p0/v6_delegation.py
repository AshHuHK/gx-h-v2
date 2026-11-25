"""A Lead can assign one branch decision against its current immutable brief."""

from dataclasses import dataclass
import json

from ..domain import ids
from ..errors import BudgetExceeded, IdempotencyConflict, SchemaInvalid, ScopeDenied, StaleRevision
from ..orchestration import seams
from ..persistence import store
from . import v6_decisions, v6_goal


TOOL = {
    "type": "function", "name": "gx_delegate_branch",
    "description": ("Assign one decision on an open branch. Its Jobs use that branch’s fixed resource limits. "
                    "The Run pays for the worker task and native turn, subject to any further branch ceilings."),
    "inputSchema": {"type": "object", "additionalProperties": False,
        "properties": {
            "branch_id": {"type": "string", "description": "Durable ID of an open branch in this brief."},
            "question": {"type": "string", "description": "Scientific question for the branch owner to decide."}},
        "required": ["branch_id", "question"]},
}


@dataclass(frozen=True)
class Delegation:
    identity: dict
    key: str
    request_sha: str
    brief: dict
    context_ref: str
    cursor: int


def require_grant(conn, run_id, task_id, scope):
    row = conn.execute(
        "SELECT e.payload_json FROM events e JOIN v6_decision_tasks d ON d.task_id=e.subject_id "
        "WHERE e.run_id=? AND e.kind='v6.branch.delegated' AND d.run_id=e.run_id AND d.task_id=? AND d.scope=?",
        (run_id, task_id, scope),
    ).fetchone()
    if not row or json.loads(row[0])["scope"] != scope:
        raise ScopeDenied("The branch decision requires a recorded Lead delegation")


def _replay(conn, run_id, key, request_sha):
    row = conn.execute(
        "SELECT e.payload_json FROM v6_decision_tasks d JOIN events e ON e.subject_id=d.task_id "
        "AND e.run_id=d.run_id AND e.kind='v6.branch.delegated' WHERE d.run_id=? AND d.event_key=?",
        (run_id, key),
    ).fetchone()
    if row:
        answer = json.loads(row[0])
        if answer["request_sha256"] != request_sha:
            raise IdempotencyConflict("The delegation call already carried different content or identity")
        return answer
    return None


def _branch_brief(conn, task, branch, question):
    """Project only the parent snapshot's shared inputs and this branch's recorded evidence."""
    parent = store.loads(task, "snapshot_json")
    rid, scope, branch_id = task["run_id"], "branch:" + branch["branch_id"], branch["branch_id"]
    scoped = {key: parent[key] for key in ("run_id", "goal", "goal_version", "head", "intent", "materials", "capabilities")}
    nodes = [{key: n[key] for key in ("node_id", "branch_id", "version", "status", "job_id", "result_ref")}
             for n in parent["nodes"] if n["branch_id"] == branch_id]
    claims = [c for c in parent["claims"] if c["decision_scope"] == scope]
    # Source aliases are allowed to overlap across scopes. Filter by the original event,
    # before exposing either its artifact, its alias or the actual observed values.
    sources = conn.execute(
        "SELECT event_id,subject_id,json_extract(payload_json,'$.artifact_ref') AS ref FROM events "
        "WHERE run_id=? AND kind='v6.observation.added' AND event_id<=? "
        "AND json_extract(payload_json,'$.scope') IN ('run',?)", (rid, task["event_cursor"], branch_id),
    ).fetchall()
    authorized_events = {r["event_id"] for r in sources}
    observations = [o for o in parent["observations"] if o["event_id"] in authorized_events or
                    (o["event"] == "v6.node.settled" and o["branch_id"] == branch_id)]
    refs = {m["artifact_ref"] for m in parent["materials"]}
    refs.update(n["result_ref"] for n in nodes if n["result_ref"])
    refs.update(r[key] for r in sources for key in ("subject_id", "ref"))
    refs.update(c[key] for c in claims for key in ("claim_ref", "claim_revision_id"))
    scoped.update(scope=scope, delegation_question=question, native_turn_budget_scope="run_with_explicit_branch_ceilings",
                  branches=[branch], nodes=nodes, claims=claims,
                  observations=observations, evidence_refs=[r for r in parent["evidence_refs"] if r in refs])
    limits = branch["spec"]["budget_limits"]
    from .v6_branch_budget import exposure
    used = exposure(conn, branch_id)
    scoped["budget"] = {"limits": limits, "held_or_spent": {k: used[k] for k in limits},
                        "available": {k: max(0, cap - used[k]) for k, cap in limits.items()}}
    return scoped


def prepare(conn, *, run_id, task_id, lease_id, thread_id, turn_id, call_id, payload):
    if conn.in_transaction:
        raise RuntimeError("Prepare the branch brief outside the writer")
    if (not isinstance(payload, dict) or set(payload) != {"branch_id", "question"} or
            any(not isinstance(v, str) or not v.strip() for v in payload.values())):
        raise SchemaInvalid("Delegation requires an existing branch_id and a scientific question")
    identity = dict(run_id=run_id, task_id=task_id, lease_id=lease_id, thread_id=thread_id, turn_id=turn_id)
    key = "v6.delegate:" + ids.sha256_of_obj([thread_id, call_id])
    sha = ids.sha256_of_obj([identity, call_id, payload])
    replay = _replay(conn, run_id, key, sha)
    if replay:
        return replay
    _, task, _, _ = v6_decisions.authority(conn, **identity)
    if task["scope"] != "run":
        raise ScopeDenied("Only the Run Lead may delegate a branch decision")
    parent = store.loads(task, "snapshot_json")
    branch = next((b for b in parent["branches"] if b["branch_id"] == payload["branch_id"] and b["status"] == "OPEN"), None)
    if branch is None:
        raise ScopeDenied("Choose an open branch from this frozen Lead brief")
    brief = _branch_brief(conn, task, branch, payload["question"])
    return Delegation(dict(identity, call_id=call_id), key, sha, brief, task["context_ref"], task["event_cursor"])


def commit(conn, plan):
    if isinstance(plan, dict):
        return plan
    identity = dict(plan.identity)
    call_id = identity.pop("call_id")
    run_id, scoped = identity["run_id"], plan.brief
    branch_id = scoped["branches"][0]["branch_id"]
    with store.transaction(conn, transaction_owner="ScientificLead", operation="delegate_branch", run_id=run_id):
        replay = _replay(conn, run_id, plan.key, plan.request_sha)
        if replay:
            return replay
        _, parent, goal, head = v6_decisions.authority(conn, **identity)
        if parent["scope"] != "run" or parent["context_ref"] != plan.context_ref or head != scoped["head"]:
            raise StaleRevision("The delegating Lead's scope or frozen premises changed")
        branch = conn.execute("SELECT status FROM v6_branches WHERE run_id=? AND branch_id=?", (run_id, branch_id)).fetchone()
        if not branch or branch[0] != "OPEN":
            raise StaleRevision("The requested branch has ceased accepting decisions")
        marks = ",".join("?" for _ in v6_goal.ACTIVE_TASKS)
        if conn.execute("SELECT 1 FROM v6_decision_tasks JOIN tasks USING(task_id) WHERE v6_decision_tasks.run_id=? "
                        f"AND scope=? AND status IN ({marks})", (run_id, scoped["scope"], *v6_goal.ACTIVE_TASKS)).fetchone():
            raise ScopeDenied("This branch already has an active decision task")
        limits = scoped["budget"]["limits"]
        if "worker_tasks" in limits:
            from .v6_branch_budget import exposure
            if exposure(conn, branch_id)["worker_tasks"] + 1 > limits["worker_tasks"]:
                raise BudgetExceeded("Branch worker_tasks allowance is exhausted",
                                     details={"branch_id": branch_id, "limit": limits["worker_tasks"]})
        account = conn.execute("SELECT budget_ref FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
        tid = v6_goal.persist_brief(conn, run_id, scoped["scope"], scoped, head, goal["goal_version"],
                                   plan.key, plan.cursor, account)
        charge = seams.charge_worker_task(conn, budget_ref=account, task_id=tid, actor=identity["thread_id"])
        ref = conn.execute("SELECT context_ref FROM v6_decision_tasks WHERE task_id=?", (tid,)).fetchone()[0]
        answer = dict(run_id=run_id, scope=scoped["scope"], branch_id=branch_id, task_id=tid, context_ref=ref,
                      base=head, goal_version=goal["goal_version"], budget_limits=limits,
                      worker_reservation_ref=charge["reservation_ref"],
                      parent_task_id=parent["task_id"], parent_context_ref=plan.context_ref,
                      parent_lease_id=identity["lease_id"], thread_id=identity["thread_id"], turn_id=identity["turn_id"],
                      call_id=call_id, request_sha256=plan.request_sha)
        store.append_event(conn, run_id=run_id, actor=identity["thread_id"], subject_kind="task", subject_id=tid,
                           kind="v6.branch.delegated", payload=answer, evidence_level="EXECUTION_OBSERVED")
        return answer


def retire_stale(conn, run_id):
    """Retire obsolete decision authority. Admitted Jobs continue with their own bindings."""
    if not conn.in_transaction:
        raise RuntimeError("Supersede old briefs in the graph adoption transaction")
    head = v6_decisions.head(conn, run_id)
    marks = ",".join("?" for _ in v6_goal.ACTIVE_TASKS)
    rows = conn.execute("SELECT d.task_id,d.scope FROM v6_decision_tasks d JOIN tasks t USING(task_id) "
                        f"WHERE d.run_id=? AND d.base_json IS NOT ? AND t.status IN ({marks})",
                        (run_id, store._j(head), *v6_goal.ACTIVE_TASKS)).fetchall()
    for row in rows:
        v6_goal.cancel_decision(conn, run_id, row["task_id"], reason="A new graph head superseded this frozen decision")
        kind = "v6.decision.superseded" if row["scope"] == "run" else "v6.branch.delegation_ended"
        store.append_event(conn, run_id=run_id, actor=v6_goal.ACTOR, kind=kind, subject_kind="task", subject_id=row["task_id"],
                           payload={"scope": row["scope"], "head": head}, evidence_level="EXECUTION_OBSERVED")
