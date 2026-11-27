"""Module plans, traceable worker findings, and the lead's reading of those findings."""
from gx_core import contracts
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import SchemaInvalid
from gx_core.persistence import store


PLAN, WORK, SYNTHESIS = "module_plan", "module_work", "module_synthesis"
KINDS = {PLAN: PLAN, WORK: "module_result", SYNTHESIS: SYNTHESIS}

BRIEFS = {
    PLAN: ('As module lead, choose independently actionable work items, their roles, and questions. '
           'On a return, inspect the previous handoff and request just the missing work. '
           'Publish module_plan {"items":[{"key":"check","role":"researcher","question":"Which observations answer this question?"}]}. '
           'A returned module may submit an empty items list to reinterpret its existing measurements.'),
    WORK: ('Answer the assigned item. Read the original inputs via scope_ref. Reuse appropriate existing workflows, '
           'sources or evaluations by exact reference. Distinguish actual results from proposed work. '
           'Publish module_result {"finding":"Report the observation, method, evidence and limits in your own words."}. '
           'Name other run-owned output artifacts in the same submission when appropriate.'),
    SYNTHESIS: ('As module lead, inspect the original worker products including prior results. Resolve or explicitly '
                'retain discrepancies. Publish module_synthesis {"summary":"Your interpretation","result_refs":'
                '["every original module_result reference"],"open_gaps":[],"conflicts":[]}. '
                'Use empty lists when justified. Read the complete assignment if it is supplied by reference.'),
}
HAND_IN = (' Publish your own product with gx_artifact_put using the assigned task_id and JSON content. '
           'Submit its artifact_ref with your lease through gx_task_submit. The scope_ref provides the '
           'original inputs via gx_artifact_get.')


def text(value):
    return isinstance(value, str) and bool(value.strip())


def entries(values, *, role_key, allow_empty=False):
    if not isinstance(values, list) or (not values and not allow_empty):
        raise SchemaInvalid("provide a list of keyed questions")
    seen = set()
    for entry in values:
        if not isinstance(entry, dict) or not all(text(entry.get(k)) for k in ("key", "question")):
            raise SchemaInvalid("each entry needs a nonempty key and question")
        if entry["key"] in seen:
            raise SchemaInvalid("keys must be unique within the list")
        seen.add(entry["key"])
        role = entry.get(role_key, "planner" if role_key == "lead_role" else None)
        if not isinstance(role, str) or not contracts.is_label("worker_role", role) or role == "coordinator":
            raise SchemaInvalid(f"{role_key} must name an installed worker role")


def publish(ctx, kind, body, *, name=None):
    asset = ArtifactService(ctx.conn).publish_bytes(
        namespace=f"{ctx.run_id}/modules/r{ctx.round}", rel_path=f"{name or kind}.json", kind=kind,
        data=ids.canonical_json(body), media_type="application/json", run_id=ctx.run_id, actor=ctx.actor)
    return asset.artifact_ref


def product(conn, stage, task_id, refs, *, owner=None):
    service = ArtifactService(conn)
    artifacts = [service.describe(ref) for ref in refs]
    kind = KINDS[stage]
    match = [item for item in artifacts if item["kind"] == kind]
    if len(match) != 1 or match[0]["task_id"] != task_id:
        raise SchemaInvalid(f"publish one {kind} under this task_id")
    if owner is not None and match[0]["put_by"] != owner:
        raise SchemaInvalid(f"the submitting worker must publish its {kind}")
    return match[0]["artifact_ref"]


def validate(conn, task, bundle):
    stage = task["input"]["assignment"]
    lease = conn.execute("SELECT owner,principal FROM leases WHERE lease_id=?", (task["lease_id"],)).fetchone()
    ref = product(conn, stage, task["task_id"], task["submission_artifact_refs"], owner=lease["owner"])
    service = ArtifactService(conn)
    body = service.read_json(ref)
    if not isinstance(body, dict):
        raise SchemaInvalid(f"{KINDS[stage]} must be a JSON object")
    inputs = task["input"]
    if "assignment_ref" in inputs:
        inputs = service.read_json(inputs["assignment_ref"])
    root = conn.execute("SELECT owner FROM coordinator_leases WHERE run_id=?", (task["run_id"],)).fetchone()
    if lease["principal"] == root["owner"]:
        raise SchemaInvalid("a module product must be authored by its assigned lead or worker")
    if stage == WORK and lease["principal"] == inputs["lead_subject"]:
        raise SchemaInvalid("the module lead must delegate each new work item to a worker")
    if stage in (PLAN, SYNTHESIS) and inputs.get("lead_subject") not in (None, lease["principal"]):
        replacement = conn.execute(
            "SELECT 1 FROM task_assignments a JOIN teams t ON t.team_id=a.team_id WHERE a.task_id=? "
            "AND a.subject=? AND (a.assignment_generation>1 OR t.leader_subject=a.subject)",
            (task["task_id"], lease["principal"])).fetchone()
        if replacement is None:
            raise SchemaInvalid("assign the original module lead or use the existing team handoff for its successor")
    if stage == PLAN:
        entries(body.get("items"), role_key="role", allow_empty=bool(inputs.get("previous_handoff_ref")))
    elif stage == WORK:
        if not text(body.get("finding")):
            raise SchemaInvalid("the assigned work item needs its finding")
    elif stage == SYNTHESIS:
        expected = inputs["result_refs"]
        refs = body.get("result_refs")
        if (not isinstance(refs, list) or not all(text(ref) for ref in refs)
                or sorted(refs) != sorted(expected)):
            raise SchemaInvalid("result_refs must cover exactly this module's original worker results")
        if not text(body.get("summary")) or not all(
                isinstance(body.get(k), list) and all(text(v) for v in body[k]) for k in ("conflicts", "open_gaps")):
            raise SchemaInvalid("provide a synthesis summary, conflicts and open_gaps")


def accepted(conn, run_id, task_id, stage):
    row = conn.execute(
        "SELECT s.submission_id,s.artifact_refs_json,l.principal FROM tasks t JOIN submissions s "
        "ON t.task_id=s.task_id JOIN leases l ON s.lease_id=l.lease_id WHERE t.run_id=? AND t.task_id=? "
        "AND t.stage='modules' AND json_extract(t.input_json,'$.assignment')=? AND t.status='ACCEPTED' AND s.result='ACCEPTED'", (run_id, task_id, stage)).fetchone()
    if row is None:
        return None
    refs = store.loads(row, "artifact_refs_json", [])
    return {"task_id": task_id, "submission_id": row["submission_id"], "author": row["principal"],
            "artifact_ref": product(conn, stage, task_id, refs), "artifact_refs": refs}
