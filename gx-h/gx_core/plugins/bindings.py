"""Adapt verified contributions to the existing GX WorkflowHooks and Root interfaces."""
from dataclasses import replace

from .. import schema
from ..errors import GxError, SchemaInvalid, ScopeDenied
from ..orchestration.dispatch_preparation import DispatchPreparation
from ..orchestration.hooks import ResearchStart, WorkerTools
from .runtime_context import CallScope, TaskContext


def _research_start(handler):
    def start(spec):
        value = handler(spec)
        if (not isinstance(value, dict) or not {"inputs", "files", "items", "roles"} <= value.keys()
                or value.keys() - {"inputs", "files", "items", "roles", "fixed_items"}
                or not all(isinstance(value[key], dict) for key in ("inputs", "roles"))
                or not all(isinstance(value.get(key, ()), (list, tuple)) for key in ("files", "items", "fixed_items"))):
            raise SchemaInvalid("research_start must return inputs, files, items, roles and optional fixed_items")
        return ResearchStart(value["inputs"], tuple(value["files"]), tuple(value["items"]), value["roles"],
                             tuple(value.get("fixed_items", ())))
    return start


def _hook(name, handler):
    if name == "research_start":
        return _research_start(handler)
    if name == "run_inputs" or type(handler) is bool:
        return handler
    if name == "build_worker_tools":
        raise SchemaInvalid("register worker tool schemas and handlers through ctx.tools")

    def call(conn, run_id, *args):
        task_id = attempt = None
        if name == "build_task_context":
            task_id, attempt = args
        elif name == "prepare_dispatch":
            task, = args
            task_id = task["task_id"]
            args = ({key: task[key] for key in ("task_id", "role", "kind", "stage", "attempt")},)
        scope = CallScope(conn, run_id, task_id=task_id, attempt=attempt)
        try:
            result = handler(scope.context, *args)
            if name == "build_task_context":
                if not isinstance(result, dict) or not {"intent", "supplement_sources"} <= result.keys():
                    raise SchemaInvalid("a task context needs its bound intent and supplement_sources")
                return TaskContext(result, scope.citations)
            if name == "prepare_dispatch" and type(result) is not bool:
                raise SchemaInvalid("prepare_dispatch must return a readiness boolean")
            return result
        finally:
            scope.close()

    if name == "reevaluate":
        return lambda conn, payload: call(conn, payload["run_id"], payload)
    return call


class _WorkerCalls:
    def __init__(self, base, tools):
        self.base = base
        self.tools = tools
        self.plugin_names = frozenset(tools)

    def __call__(self, row, name, args, envelope, *, prepared=None):
        if name in self.plugin_names or self.base.call is None:
            raise ScopeDenied("this tool requires a fenced GX worker callback")
        if self.base.prepare is None:
            return self.base.call(row, name, args, envelope)
        return self.base.call(row, name, args, envelope, prepared=prepared)

    def invoke(self, root, row, name, args, envelope):
        tool, input_schema = self.tools[name]
        problems = schema.validate(args, input_schema)
        if problems:
            raise SchemaInvalid(f"invalid arguments to {name}: " + ", ".join(problems))
        scope = CallScope(root.conn, row["run_id"], task_id=row["task_id"], attempt=row["attempt"],
                          root=root, binding=row, envelope=envelope)
        try:
            try:
                result = tool.handler(scope.context, args)
            except GxError:
                raise
            except (Exception, SystemExit) as exc:
                raise SchemaInvalid(f"plugin tool {name} failed: {type(exc).__name__}: {exc}") from exc
            return scope.finish_tool(result)
        finally:
            scope.close()


def bind_hooks(base_hooks, loaded_plugins, *, workflow_id, version, dispatch_preparation=None):
    """Build callbacks scoped to the selected graph and exact package closure."""
    from ..p0.roots import WORKER_TOOLS
    key = workflow_id, version
    overrides, tools = {}, {}
    specs = []
    reserved = {item["name"] for item in WORKER_TOOLS} | {"gx_project_state", "gx_propose_intent"}
    for plugin in loaded_plugins:
        contribution = plugin.contributions.workflows.get(key)
        if contribution is not None:
            for name, value in contribution.hooks.items():
                if name in overrides:
                    raise SchemaInvalid(f"multiple plugins contribute hook {name!r} to {workflow_id}@{version}")
                overrides[name] = _hook(name, value)
        for name, tool in plugin.contributions.tools.items():
            if name in reserved or name in tools:
                raise SchemaInvalid(f"plugin tool {name!r} conflicts with an existing tool in this composition")
            spec = tool.as_spec()
            tools[name] = (tool, spec["inputSchema"])
            specs.append(spec)

    if dispatch_preparation is not None:
        external_check = overrides.get("prepare_dispatch")
        def prepare_dispatch(conn, run_id, task):
            result = dispatch_preparation(conn, run_id, task)
            if external_check is None or not result:
                return result
            if isinstance(result, DispatchPreparation):
                return replace(result, apply=lambda prepared: result.apply(prepared) and external_check(conn, run_id, task))
            return external_check(conn, run_id, task)
        overrides["prepare_dispatch"] = prepare_dispatch

    if tools:
        def worker_tools(conn, run_id):
            base = base_hooks.build_worker_tools(conn, run_id)
            overlap = tools.keys() & {spec["name"] for spec in base.specs}
            if overlap:
                raise SchemaInvalid(f"worker tool names already belong to this graph: {sorted(overlap)}")
            return WorkerTools((*base.specs, *specs), base.prepare, _WorkerCalls(base, tools))
        overrides["build_worker_tools"] = worker_tools
    return replace(base_hooks, **overrides)
