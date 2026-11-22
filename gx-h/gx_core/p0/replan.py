"""Resume an existing paused grid investigation from a human-committed replacement proposal."""
import math

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import DependencyUnmet
from ..jobs.service import JobService
from ..orchestration import service
from ..persistence import store
from ..teams.service import TeamService, backend_ctx
from . import intent, science


def _selected_plan(items, objective, criterion):
    refs = {key: {"item_id": item["item_id"], "version": item["version"]}
            for key, item in (("objective", objective), ("evaluation", criterion))}
    plans = [item for item in items.values() if item["item_type"] == "data_source"
             and isinstance(item["value"], dict) and item["value"].get("kind") == "replacement_proposal"
             and all(item["value"].get(key) == ref for key, ref in refs.items())]
    if len(plans) != 1:
        raise DependencyUnmet("Submit one active data_source replacement_proposal for the current objective "
                              "and evaluation item versions before resuming the project.", details=refs)
    return plans[0]


def _proposal(conn, run, plan, objective, criterion):
    from workflows.science_grid.nodes import _validate_grid
    body = plan["value"].get("proposal")
    frozen = science.freeze_spec(conn, run, criterion["value"])
    cutoff = science._spec(conn, frozen)["threshold"]
    science.require(isinstance(body, dict) and type(body.get("threshold")) in (float, int) and
                    body["threshold"] == cutoff, "replacement proposal must use the active frozen evaluation cutoff")
    paths = body.get("paths")
    science.require(isinstance(paths, list) and bool(paths), "the replacement proposal requires research paths")
    declared, seen = [], set()
    for p in paths:
        science.require(isinstance(p, dict) and all(isinstance(p.get(k), str) and p[k].strip()
                        for k in ("id", "prediction")) and p["id"] not in seen and
                        type(p.get("expected_reaches_threshold", True)) is bool,
                        "each replacement path needs a unique ID, prediction, and boolean cutoff expectation")
        grid = p.get("grid")
        _validate_grid(grid)
        science.require(all(isinstance(point, dict) and isinstance(point.get("candidate_id"), str)
                            and type(point.get("x")) in (int, float) and math.isfinite(point["x"])
                            for point in grid["candidates"]), "the replacement grid requires finite named sample points")
        # An unchanged local route name under a new objective is a different scientific claim.
        hypothesis_key = ids.sha256_of_obj([objective["item_id"], objective["version"], p.get("hypothesis_id", p["id"])])
        declared.append({**p, "hypothesis_id": hypothesis_key})
        seen.add(p["id"])
    return {"threshold": cutoff, "paths": declared}


def _previous(conn, run_id):
    run = conn.execute("SELECT * FROM runs WHERE run_id=? AND workflow_id='science_grid'", (run_id,)).fetchone()
    if not run or run["status"] != "PAUSED":
        return None
    checkpoint = store.latest_checkpoint(conn, run_id)
    if not checkpoint:
        return None
    state = store.loads(checkpoint, "state_json")
    proposal_ref = state.get("outputs", {}).get("proposal", {}).get("proposal_ref")
    if not proposal_ref:
        return None
    proposal = ArtifactService(conn).read_json(proposal_ref)
    pin = proposal.get("objective_item")
    if not pin:
        return None
    current = intent.read_item(conn, run_id, pin["item_id"])
    if current["status"] == "active" and current["version"] == pin["version"]:
        return None
    if current["status"] != "active":
        raise DependencyUnmet("The objective was removed. Commit its replacement and a corresponding research proposal.")
    return run, checkpoint, state, proposal_ref, proposal, current


def prepare_replan(conn, run_id):
    """Before run_control continues a paused project, prepare its confirmed successor if needed.

    Return False for an ordinary pause and for a replay of a previously committed replan.
    History, prior expenses and original observations remain addressable through the old checkpoint.
    """
    run = conn.execute("SELECT workflow_id FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if run and run["workflow_id"] == "tl1a_geometry":
        from workflows.tl1a_geometry.replan import prepare
        return prepare(conn, run_id)
    if not _previous(conn, run_id):
        return False
    JobService(conn).reconcile(run_id)
    with store.transaction(conn):
        pending = _previous(conn, run_id)
        if not pending:
            return False
        run, old, state, proposal_ref, prior, objective = pending
        items = intent.active_items(conn, run_id)
        criterion = items.get((prior.get("evaluation_item") or {}).get("item_id"))
        if not criterion:
            raise DependencyUnmet("The replacement requires the selected evaluation item to be active.")
        source = _selected_plan(items, objective, criterion)
        plan = _proposal(conn, run_id, source, objective, criterion)
        ctx = backend_ctx(conn)
        engine = service._engine(ctx, run_id)
        workflow = service._pinned_workflow(conn, run)
        service.require_pins(run, workflow)
        round_index = state["round"]
        if round_index + 1 >= state["inputs"]["max_rounds"] or round_index >= engine._round_budget():
            raise DependencyUnmet("The existing round budget has no remaining round for the proposed research.")
        if engine._budget_exhausted(engine._budget_snapshot(run_id)):
            raise DependencyUnmet("The project must have remaining resources before resuming.")
        if conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')",
                        (run_id,)).fetchone():
            raise DependencyUnmet("The earlier Job receipts are still outstanding. Reconcile before resuming.")
        if old["node"] not in ("experiment", "assessment", "decision"):
            raise DependencyUnmet("The current workflow checkpoint cannot re-enter its proposal node.")
        reason = f"Human replacement proposal {source['item_id']} v{source['version']} for objective v{objective['version']}"
        superseded = TeamService(conn).cancel_open(conn, run_id=run_id, reason=reason, actor="backend")
        # All prior checkpoints and artifacts remain immutable. This scratch history is only the
        # current objective's final report; record the former authoritative checkpoint in the event.
        state["scratch"] = {"next_proposal": plan}
        state.update(pending_interrupt=None, interrupt_ref=None)
        state.pop("advance", None)
        rewound = engine._rewind(conn, run_id=run_id, workflow=workflow, state=state, actor="backend",
                                 node=old["node"], target="proposal", reason=reason)
        if not rewound:
            raise DependencyUnmet("The workflow reentry budget was exhausted before the new round.")
        fresh = store.latest_checkpoint(conn, run_id)
        pause = store.loads(run, "paused_json", {}) or {}
        pause["continue_from"] = {"status": "ACTIVE", "node": "proposal", "interrupt_ref": None, "checkpoint_seq": fresh["seq"]}
        conn.execute("UPDATE runs SET paused_json=?,interrupt_ref=NULL WHERE run_id=?", (ids.canonical_json(pause), run_id))
        intent.bind_dependencies(conn, run_id, "workflow", run_id, {source["item_id"]: "context"})
        store.append_event(conn, kind="p0.objective_replanned", actor="backend", run_id=run_id, subject_kind="run",
            subject_id=run_id, evidence_level="EXECUTION_OBSERVED", reason=reason, payload={
                "previous_checkpoint": old["checkpoint_id"], "previous_proposal_ref": proposal_ref,
                "checkpoint": fresh["checkpoint_id"], "previous_objective": prior["objective_item"],
                "objective": source["value"]["objective"], "evaluation": source["value"]["evaluation"],
                "proposal_source": {"item_id": source["item_id"], "version": source["version"], "change_id": source["change_id"]},
                "superseded_task_ids": superseded})
        return True
