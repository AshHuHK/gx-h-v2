"""Behavior of an installed workflow version at GX's existing owner boundaries."""

from dataclasses import dataclass, fields
from typing import Callable

from ..domain import ids
from ..errors import DependencyUnmet, SchemaInvalid


@dataclass(frozen=True)
class WorkerTools:
    specs: tuple = ()
    prepare: Callable | None = None
    call: Callable | None = None

    def __post_init__(self):
        names = [spec.get("name") for spec in self.specs]
        if (not all(isinstance(n, str) and n for n in names) or len(set(names)) != len(names)
                or self.specs and not callable(self.call) or self.prepare is not None and not callable(self.prepare)):
            raise SchemaInvalid("Workflow worker tools require distinct names and a callable handler")


@dataclass(frozen=True)
class ResearchStart:
    """Package contributions to the public operator start specification."""

    inputs: dict
    files: tuple
    items: tuple
    roles: dict
    fixed_items: tuple = ()


def basic_research_start(spec):
    return ResearchStart({}, (), (), {"objective": "context"})


def bind_initial_items(conn, run_id, items, roles):
    """The default start needs no run-scoped artifacts before binding Intent."""


def standard_evaluation(value):
    if not isinstance(value, dict):
        return False
    return (set(value) == {"eval_spec_id", "version"} and
            all(isinstance(v, str) and v.strip() for v in value.values()) or
            "threshold" in value and set(value) <= {"threshold", "evaluator"} and
            type(value["threshold"]) in (int, float))


def validate_standard_intent(conn, run_id, edits):
    for edit in edits:
        if (edit["item_type"] == "evaluation_spec" and edit["value"] is not None and
                not standard_evaluation(edit["value"])):
            raise SchemaInvalid("This workflow accepts a frozen evaluation reference or a numeric threshold")


def no_worker_tools(conn, run_id):
    return WorkerTools()


def dispatch_ready(conn, run_id, task):
    """Ordinary READY tasks proceed to the existing scheduler and lease checks."""
    return True


def current_task_context(conn, run_id, task_id, attempt):
    from ..p0 import intent
    return {"task_id": task_id, "intent": intent.snapshot(conn, run_id, "task", task_id),
            "supplement_sources": []}


def reevaluation_unavailable(conn, payload):
    raise DependencyUnmet("The pinned workflow has no reevaluation method for these observations")


def keep_checkpoint(conn, run_id):
    return False


def preserve_job(conn, run_id, proposal):
    """JobService owns capability, lease, budget and resource admission."""


def preserve_inputs(inputs):
    return inputs


def retain_source(conn, run_id, source_id):
    return {"review_status": "pending", "reason": "no_workflow_source_review",
            "message": "The source was saved. This workflow has no automatic supplemental review."}


def no_source_delivery(conn, run_id, source_id):
    raise DependencyUnmet("The pinned workflow has no supplemental review delivery")


@dataclass(frozen=True)
class WorkflowHooks:
    """Each function has one owner. Missing behavior has an explicit default.

    prepare_dispatch and build_task_context run under the claim/callback transaction.
    prepare_job validates an exact new Job request before admission, outside a writer.
    A hook may reject the request. Job creation, evaluation and formal Intent writes
    remain the responsibility of their respective GX services.
    """

    validate_intent: Callable = validate_standard_intent
    build_worker_tools: Callable = no_worker_tools
    prepare_dispatch: Callable = dispatch_ready
    build_task_context: Callable = current_task_context
    reevaluate: Callable = reevaluation_unavailable
    replan: Callable = keep_checkpoint
    prepare_job: Callable = preserve_job
    research_start: Callable = basic_research_start
    bootstrap_intent: Callable = bind_initial_items
    run_inputs: Callable = preserve_inputs
    register_supplement: Callable = retain_source
    reconcile_supplement: Callable = no_source_delivery
    auto_advance: bool = False
    mechanical_continuation: bool = False

    def content(self):
        from .engine import module_sources
        implementation, functions = {}, []
        for field in fields(self):
            value = getattr(self, field.name)
            if field.name in {"auto_advance", "mechanical_continuation"} and type(value) is bool:
                implementation[field.name] = value
            elif callable(value) and getattr(value, "__qualname__", None):
                implementation[field.name] = f"{value.__module__}.{value.__qualname__}"
                functions.append(value)
            else:
                raise SchemaInvalid(f"Invalid workflow hook {field.name}")
        return {"bindings": implementation, "sources": module_sources(functions)}

    def digest(self):
        return ids.sha256_of_obj(self.content())


DEFAULT_HOOKS = WorkflowHooks()
