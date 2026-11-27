"""Run-scoped operations available to Goal method binders and Lead tool callbacks."""
from __future__ import annotations

from dataclasses import dataclass
import json

from .. import schema
from ..artifacts import ArtifactService
from ..capabilities import environments, manifest as capabilities, registry
from ..domain import ids
from ..errors import EnvNotReady, SchemaInvalid, ScopeDenied
from ..persistence import store
from ..plugins import goal
from . import manifest, science, v6_claims


@dataclass(frozen=True)
class Evaluation:
    value: dict
    sources: tuple[str, ...]
    folder: str
    kind: str


class MethodContext:
    def __init__(self, conn, run_id, refs, *, writable=False, tool_capability=None, authority=None):
        self._conn = conn
        self._run = run_id
        self._writable = writable
        self._tool_capability = tool_capability
        self._authority = authority
        self._verified_outputs = set()
        self._refs = set(refs)
        self._assets = ArtifactService(conn)

    def describe(self, ref):
        if ref not in self._refs:
            raise ScopeDenied("The method may read only its assigned inputs and their recorded sources")
        row = self._assets.describe(ref)
        if row["run_id"] != self._run:
            raise SchemaInvalid("Method input belongs to a different Run")
        return row

    def read_bytes(self, ref, *, expected_sha256=None):
        info = self.describe(ref)
        return self._assets.read_bytes(ref, expected_sha256=expected_sha256 or info["sha256"])

    def read_json(self, ref):
        return json.loads(self.read_bytes(ref))

    def job_output(self, ref, capability, output_name):
        row = self.describe(ref)
        if row["origin"] != "JOB_OUTPUT" or not row["job_id"]:
            raise SchemaInvalid("Expected an original Job output")
        receipt = manifest.read_manifest(self._conn, row["job_id"])
        admitted, completed = receipt["admission"], receipt["completion"]
        pin = admitted["capability"]
        if (admitted["run_id"] != self._run or f"{pin['id']}@{pin['version']}" != capability
                or not completed or completed["status"] != "SUCCEEDED"):
            raise SchemaInvalid(f"Expected a successful {capability} Job in the current run")
        if not any(item["artifact_ref"] == ref and item["output_name"] == output_name
                   and item["sha256"] == row["sha256"] and item.get("runner_sha256", item["sha256"]) == row["sha256"]
                   for item in completed["outputs"]):
            raise SchemaInvalid("Artifact does not match the immutable Job outcome")
        frozen = {item["role"]: item for item in admitted["inputs"]}
        self._refs.update(item["artifact_ref"] for item in frozen.values())
        self._verified_outputs.add((ref, capability))
        return row, frozen

    def publish_json(self, folder, kind, value):
        if not self._writable:
            raise ScopeDenied("This method is validating inputs", details={"reason": "binding_needs_materialization"})
        ref = science._publish(self._conn, self._run, folder, kind, value)
        self._refs.add(ref)
        return ref

    def install_analysis_template(self, *, build_ref, capability, scripts, documents, expected_revision):
        """Use only a successful builder Job in this Run and the builder's pinned Python templates."""
        from ..plugins import development
        if not self._writable or (build_ref, self._tool_capability) not in self._verified_outputs:
            raise ScopeDenied("A granted method author tool is required")
        row = next(row for row in goal.catalog(self._conn, self._run)
                   if row["capability_version"] == self._tool_capability)
        return development.install(self._conn, self._run, goal._provider(row), build_ref=build_ref,
                                   capability=capability, scripts=scripts, documents=documents,
                                   expected_revision=expected_revision, authority=self._authority)

    def evaluation(self, value, source_refs, *, folder="evaluations", kind="goal_evaluation"):
        return Evaluation(value, tuple(source_refs), folder, kind)


def invoke(callback, *args, **kwargs):
    try:
        return callback(*args, **kwargs)
    except (ValueError, TypeError, OverflowError) as exc:
        raise SchemaInvalid(str(exc)) from exc


def dependencies(conn, run, capability, executable):
    method = goal.method(conn, run, capability)
    if not method or not method.dependencies:
        return {}

    def probe(interpreter, requirements, *, source_digest_key=None):
        result = environments.probe({"env_id": "goal-method", "kind": "SYSTEM_BINARY",
                                     "python_executable": interpreter, "requirements": requirements})
        if result["status"] != "HEALTHY":
            raise EnvNotReady("The Job interpreter does not meet the method's release requirements",
                              details={"python_executable": interpreter, "reasons": result["reasons"]})
        values = {key: value["installed"] for key, value in result["requirements"].items()}
        if source_digest_key:
            source = registry.candidate(conn, capability)["manifest"]["source"]
            actual = ids.sha256_of_file(capabilities.resolve_path(source["path"]))
            if actual != source["sha256"]:
                raise SchemaInvalid("Installed method source differs from its registered manifest")
            values[source_digest_key] = actual
        return values

    return invoke(method.dependencies, capability, executable, probe=probe)


def call_tool(conn, name, arguments, *, run_id, task_id, lease_id, context_ref, thread_id, turn_id, call_id):
    tool = goal.tools(conn, run_id).get(name)
    if not tool:
        raise ScopeDenied(f"Goal has no granted provider for {name}")
    identity = dict(run_id=run_id, task_id=task_id, lease_id=lease_id, context_ref=context_ref,
                    thread_id=thread_id, turn_id=turn_id, call_id=call_id)
    key = "v6-goal-tool:" + ids.sha256_of_obj([thread_id, call_id])
    request = {**identity, "tool": name, "arguments": arguments}
    replay = v6_claims.lookup_replay(conn, key, request)
    if replay is not None:
        return replay

    def authority():
        manifest.check_capability(conn, run_id, tool.capability)
        task = v6_claims.active_context(conn, run_id, task_id, lease_id, context_ref, thread_id, turn_id)
        return set(json.loads(task["visible_refs_json"]))

    visible = authority()
    problems = schema.validate(arguments, tool.as_spec()["inputSchema"])
    if problems:
        raise SchemaInvalid(f"Invalid arguments to {name}: " + ", ".join(problems))
    ctx = MethodContext(conn, run_id, visible, writable=True, tool_capability=tool.capability, authority=authority)
    result = invoke(tool.handler, ctx, arguments)
    if isinstance(result, Evaluation):
        if not result.sources or not set(result.sources) <= visible:
            raise ScopeDenied("A retrospective evaluation requires evidence visible in the frozen decision")
        ref = ctx.publish_json(result.folder, result.kind, result.value)
        answer = {"evaluation_ref": ref, "evaluation": result.value}
    else:
        answer = result
    with store.transaction(conn, transaction_owner="GoalTools", operation="record_result", run_id=run_id):
        idem = store.begin_idempotent(conn, key, "v6.goal_tool", request)
        if idem.replay:
            return idem.response
        authority()
        if isinstance(result, Evaluation):
            store.append_event(conn, run_id=run_id, kind="v6.evaluation.recorded", actor=thread_id,
                               subject_kind="task", subject_id=task_id, evidence_level="EXECUTION_OBSERVED",
                               payload={**identity, "tool": name, "evaluation_ref": ref,
                                        "source_refs": list(result.sources)})
        store.complete_idempotent(conn, key, answer)
    return answer
