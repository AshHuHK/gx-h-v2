"""Select execution behavior from the Run's saved inputs, independent of its graph head."""

import json

from ..persistence import store


class StaticWorkflow:
    decision_tools = ()

    def start_project(self, conn, directory, spec, run_key, current):
        from ..p0.cli import start_workflow
        if type(spec.get("max_rounds")) is not int or spec["max_rounds"] < 1:
            raise ValueError("Supply a positive max_rounds")
        return start_workflow(conn, directory, spec, run_key, current)

    def admit_workflow(self, conn, spec):
        from .service import _admit_workflow
        return _admit_workflow(conn, spec)

    def bootstrap_transaction(self, prepare, run_id):
        pass

    def start_after_commit(self, ctx, run_id, inputs, created, prepare, warnings):
        from . import service
        from ..errors import IllegalTransition, SchemaInvalid
        engine = service._engine(ctx, run_id)
        if inputs.get("p0") is True:
            prepare(run_id)
        if engine.state(run_id)["checkpoint_seq"]:
            return True
        run = service._run(ctx.conn, run_id)
        try:
            started = engine.start(run_id=run_id, workflow_id=run["workflow_id"],
                                   workflow_version=run["workflow_version"],
                                   inputs={**inputs, "objective": run["objective"],
                                           "objective_id": created["objective_id"]}, actor=ctx.actor)
        except (SchemaInvalid, IllegalTransition):
            if engine.state(run_id)["checkpoint_seq"] == 0:
                raise
            return True  # Another caller started the same Run; await its settled response.
        warnings.extend(started.get("warnings") or [])
        return False

    def needs_bootstrap(self, conn, run_id):
        return store.latest_checkpoint(conn, run_id) is None

    def registry(self, ctx, run):
        from .service import _workflow_registry
        return _workflow_registry(ctx, run)

    def hooks(self, conn, run):
        from .service import _workflow_hooks
        return _workflow_hooks(conn, run)

    def advance(self, engine, run, actor, retry_failed):
        return engine._advance_workflow(run["run_id"], actor, retry_failed)

    def node_states(self, engine, run, checkpoint, waits):
        return engine._workflow_node_states(run, checkpoint, waits)

    def react(self, ctx, run, backend):
        from .reactor import _drive_workflow
        return _drive_workflow(ctx, run, backend)

    def after_continue(self, ctx, run_id, args, response):
        from .service import _continue_workflow
        _continue_workflow(ctx, run_id, args, response)

    def waiting_for(self, conn, run, engine_state, idle):
        from .service import _workflow_waiting_for
        return _workflow_waiting_for(conn, run, engine_state, idle=idle)

    def snapshot(self, conn, run, meter):
        return {}

    def latest_decision(self, conn, run_id, supplements):
        latest = conn.execute("SELECT round_index,actions_json,reason,stop_reason,decision_ref "
                              "FROM science_decisions WHERE run_id=? ORDER BY round_index DESC LIMIT 1",
                              (run_id,)).fetchone()
        if latest:
            return {**dict(latest), "pending_source_ids": [s["source_id"] for s in supplements
                                                           if s["disposition"] == "pending"]}

    def decision_task(self, task):
        return False

    def decision_root(self, row):
        return False

    def callback(self, conn, run_id, name):
        return None

    def intent_impact(self, conn, run_id, edits, *, commit):
        return []


class ResearchGoal:
    decision_tools = ("gx_decide_revision", "gx_submit_module_report")

    def start_project(self, conn, directory, spec, run_key, current):
        from ..p0 import v6_goal
        return v6_goal.start(conn, spec, run_key=run_key, current=current)

    def admit_workflow(self, conn, spec):
        return None, None, None, {}

    def bootstrap_transaction(self, prepare, run_id):
        # Admission and the first frozen decision must be visible in the same commit.
        prepare(run_id)

    def start_after_commit(self, ctx, run_id, inputs, created, prepare, warnings):
        # The first checkpoint is written by the first accepted revision.
        return False

    def needs_bootstrap(self, conn, run_id):
        return False

    def registry(self, ctx, run):
        from .registry import WorkflowRegistry
        return WorkflowRegistry()

    def hooks(self, conn, run):
        from dataclasses import replace
        from .hooks import DEFAULT_HOOKS
        from ..p0.v6_goal import supplement
        return replace(DEFAULT_HOOKS, register_supplement=supplement)

    def advance(self, engine, run, actor, retry_failed):
        from .. import config
        from ..domain import machines
        from ..errors import VersionImmutable
        from ..p0 import v6_decisions, v6_goal, v6_jobs, v62_modules
        config.check_holds(engine.hold_scope)
        run_id = run["run_id"]
        work = {}
        if run["status"] in {"ACTIVE", *machines.waiting_states("run")}:
            # Read graph head and checkpoint in the same SQLite snapshot.
            engine.conn.execute("SAVEPOINT v6_execution_head")
            try:
                checkpoint = engine.checkpointer.latest(engine.conn, run_id)
                bound = checkpoint.state.get("head") if checkpoint else None
                if bound != v6_decisions.head(engine.conn, run_id):
                    raise VersionImmutable("The current checkpoint and adopted graph head differ")
                if checkpoint:
                    v6_goal.instance(engine.conn, run_id, checkpoint)
            finally:
                engine.conn.execute("RELEASE v6_execution_head")
            if checkpoint:
                work = v6_jobs.advance(engine.conn, run_id)
            if run["status"] == "ACTIVE":
                v62_modules.advance(engine.conn, run_id)
            task = v6_goal.ensure_task(engine.conn, run_id)
            if task:
                work["decision_task_id"] = task
        return {**engine.state(run_id), "work": work, "warnings": []}

    def node_states(self, engine, run, checkpoint, waits):
        from ..p0 import v6_goal
        graph = v6_goal.instance(engine.conn, run["run_id"], checkpoint)
        states = dict(engine.conn.execute("SELECT node_id,status FROM v6_nodes WHERE run_id=?",
                                          (run["run_id"],)))
        return {n: states[n] for n in graph["nodes"]}

    def react(self, ctx, run, backend):
        from .service import _engine
        return _engine(backend, run["run_id"]).advance(run_id=run["run_id"], actor=backend.actor)

    def after_continue(self, ctx, run_id, args, response):
        from .service import _engine
        _engine(ctx, run_id).advance(run_id=run_id, actor=ctx.actor)

    def waiting_for(self, conn, run, engine_state, idle):
        from ..p0 import v6_goal
        from .service import _workflow_waiting_for
        if run["status"] == "ACTIVE":
            return v6_goal.waiting_for(conn, run["run_id"])
        return _workflow_waiting_for(conn, run, engine_state, idle=idle)

    def snapshot(self, conn, run, meter):
        from ..p0 import v6_goal
        return v6_goal.projection(conn, run, meter)

    def latest_decision(self, conn, run_id, supplements):
        from ..p0 import conversation_science
        return conversation_science.latest_receipt(conn, run_id)

    def decision_task(self, task):
        return task is not None and task["kind"] == "decide_revision"

    def decision_root(self, row):
        from ..p0 import v6_native
        return v6_native.assigned(row)

    def callback(self, conn, run_id, name):
        from ..p0 import v6_native
        return v6_native.callback_for(name, conn=conn, run_id=run_id)

    def intent_impact(self, conn, run_id, edits, *, commit):
        from ..p0 import v6_goal
        return v6_goal.impact(conn, run_id, edits, commit=commit)


STATIC = StaticWorkflow()
GOAL = ResearchGoal()


def for_start(inputs):
    return GOAL if inputs.get("mode") == "goal" else STATIC


def for_run(run):
    # Legacy/static Runs can have no workflow yet. An explicit, saved Goal marker
    # is authoritative. The dynamic execution checkpoint is never a recipe pin.
    if run is None or run["workflow_id"]:
        return STATIC
    return for_start(store.loads(run, "inputs_json", {}).get("inputs", {}))


def specification(args):
    from . import packages
    from ..p0 import v6_goal
    if getattr(args, "goal", None) is not None:
        return v6_goal.cli_spec(args)
    if getattr(args, "historical_replay", False):
        raise ValueError("--historical-replay requires --goal and an explicit historical policy")
    if any(getattr(args, name, None) for name in ("goal_config", "input", "capability", "research_effort")):
        raise ValueError("--goal-config, --input, --capability and --research-effort require --goal")
    return packages.example(args.example) if args.example else json.loads(args.spec.read_text())
