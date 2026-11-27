"""Recommission a geometry planner after a confirmed objective change, retaining prior measurements."""
from gx_core.artifacts import ArtifactService
from gx_core.domain import ids
from gx_core.errors import DependencyUnmet
from gx_core.jobs.service import JobService
from gx_core.orchestration import service
from gx_core.persistence import store
from gx_core.teams.service import TeamService, backend_ctx
from . import domain


def _changed(conn, run):
    row = conn.execute("SELECT * FROM runs WHERE run_id=? AND workflow_id='tl1a_geometry'", (run,)).fetchone()
    if not row or row["status"] not in ("PAUSED", "WAITING_CLIENT", "WAITING_EXTERNAL_JOB"):
        return None
    checkpoint = store.latest_checkpoint(conn, run)
    if not checkpoint:
        return None
    state = store.loads(checkpoint, "state_json")
    ref = state.get("outputs", {}).get("proposal", {}).get("proposal_ref")
    if not ref:
        return None
    old = ArtifactService(conn).read_json(ref)
    if domain.matches_intent(conn, run, old):
        return None
    return row, checkpoint, state, ref


def prepare(conn, run):
    if not _changed(conn, run):
        return False
    JobService(conn).reconcile(run)
    with store.transaction(conn):
        changed = _changed(conn, run)
        if not changed:
            return False
        row, checkpoint, state, old_ref = changed
        current = domain.active_inputs(conn, run)
        engine = service._engine(backend_ctx(conn), run)
        workflow = service._pinned_workflow(conn, row)
        service.require_pins(row, workflow)
        if conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND status NOT IN ('SUCCEEDED','FAILED','CANCELLED')", (run,)).fetchone():
            if row["status"] == "PAUSED":
                raise DependencyUnmet("Earlier coordinate Jobs are still outstanding. Reconcile their original receipts before resuming.")
            return False
        if state["round"] + 1 >= state["inputs"]["max_rounds"] or state["round"] >= engine._round_budget():
            if row["status"] != "PAUSED":
                store.transition(conn, machine="run", subject_id=run, to_state="PAUSED", actor="backend",
                                 reason="The confirmed changed objective needs a new round beyond the project's remaining bound.")
                return True
            raise DependencyUnmet("The confirmed changed objective needs a new round beyond the project's remaining bound.")
        reason = f"Replan against objective v{current['objective_item']['version']} and current evaluation {current['evaluation_item']}"
        superseded = TeamService(conn).cancel_open(conn, run_id=run, reason=reason, actor="backend")
        state["scratch"].pop("next_plan", None)
        state["scratch"]["prior_objective_proposal"] = old_ref
        state.update(pending_interrupt=None, interrupt_ref=None)
        state.pop("advance", None)
        if row["status"] != "PAUSED":
            store.transition(conn, machine="run", subject_id=run, to_state="ACTIVE", actor="backend", reason=reason)
        if not engine._rewind(conn, run_id=run, workflow=workflow, state=state, actor="backend", node=checkpoint["node"],
                              target="proposal", reason=reason):
            raise DependencyUnmet("No remaining re-entry for the changed coordinate objective.")
        fresh = store.latest_checkpoint(conn, run)
        if row["status"] == "PAUSED":
            paused = store.loads(row, "paused_json", {}) or {}
            paused["continue_from"] = {"status": "ACTIVE", "node": "proposal", "interrupt_ref": None,
                                       "checkpoint_seq": fresh["seq"]}
            conn.execute("UPDATE runs SET paused_json=? WHERE run_id=?", (ids.canonical_json(paused), run))
        conn.execute("UPDATE runs SET interrupt_ref=NULL WHERE run_id=?", (run,))
        store.append_event(conn, kind="p0.objective_replanned", run_id=run, actor="backend", subject_id=run,
                           subject_kind="run", reason=reason, evidence_level="EXECUTION_OBSERVED",
                           payload={"previous_proposal_ref": old_ref, "previous_checkpoint": checkpoint["checkpoint_id"],
                                    "checkpoint": fresh["checkpoint_id"], "objective": current["objective_item"],
                                    "superseded_task_ids": superseded})
    return True
