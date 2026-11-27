"""Bind author operations to a single GX hook or fenced worker call."""
from dataclasses import dataclass

from ..artifacts.service import ArtifactService
from ..capabilities import registry
from ..context import Ctx, Principal
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied
from ..orchestration import expansion
from ..p0 import compute, intent
from ..persistence import store
from ..research import records
from .author_api import (ArtifactOperations, EvaluationOperations, IntentOperations,
                         JobOperations, RunContext, TaskOperations)


class TaskContext(dict):
    """The plugin's rendered brief and independently observed task citations."""
    def __init__(self, document, citations):
        super().__init__(document)
        self.citations = tuple(citations)


@dataclass(frozen=True)
class _Product:
    document: dict


class CallScope:
    def __init__(self, conn, run_id, *, task_id=None, attempt=None, root=None, binding=None, envelope=None):
        self._connection = conn
        self.run_id, self.task_id, self.attempt = run_id, task_id, attempt
        self.root, self.binding, self.envelope = root, binding, envelope
        self.citations = set()
        self._failed_command = None
        self.context = RunContext(run_id, task_id, attempt,
            TaskOperations(self.read_context, self.inputs, self.propose_task),
            IntentOperations(self.items), ArtifactOperations(self.read_artifact, self.publish_artifact),
            JobOperations(self.inspect_capability, self.propose_job, self.job_status, self.wait_job, self.cancel_job),
            EvaluationOperations(self.propose_evaluation))

    def close(self):
        self._connection = self.root = self.binding = self.envelope = None

    @property
    def conn(self):
        if self._connection is None:
            raise ScopeDenied("the plugin callback has ended")
        return self._connection

    def _task(self, task_id):
        task_id = task_id or self.task_id
        if not task_id or (self.task_id is not None and task_id != self.task_id):
            raise ScopeDenied("this operation requires the assigned task")
        task = self.conn.execute("SELECT task_id,input_json FROM tasks WHERE run_id=? AND task_id=?",
                                 (self.run_id, task_id)).fetchone()
        if task is None:
            raise ScopeDenied("the task belongs to another Run or does not exist")
        return task

    def _cited(self, document):
        from ..p0.roots import _artifact_refs
        refs = set(_artifact_refs(document))
        self.citations.update(refs)
        if self.root is not None:
            self.root._allow_refs(self.binding["root_key"], list(refs))
        return document

    def inputs(self, task_id=None):
        return self._cited(store.loads(self._task(task_id), "input_json", {}))

    def read_context(self, task_id=None):
        task = self._task(task_id)
        return self._cited(records.task_context(self.conn, self.run_id, task["task_id"]))

    def items(self):
        if self.task_id is not None:
            return self._cited(intent.snapshot(self.conn, self.run_id, "task", self.task_id))
        return intent.active_items(self.conn, self.run_id)

    def read_artifact(self, artifact_ref):
        conn = self.conn
        if self.root is not None:
            return self.root._read_task_artifact(self.binding, {"ref": artifact_ref})
        if self.task_id is not None and artifact_ref not in self.citations:
            raise ScopeDenied("the artifact has not been cited by this task")
        artifacts = ArtifactService(conn)
        owner = artifacts.describe(artifact_ref)["run_id"]
        if owner != self.run_id and not (owner is None and artifact_ref in self.citations):
            raise ScopeDenied("the artifact is outside the bound Run")
        document = artifacts.read_json(artifact_ref)
        if self.task_id is not None and not compute.is_analysis_data(conn, artifact_ref):
            self._cited(document)
        return document

    def _outside_writer(self):
        if self.root is not None or self.conn.in_transaction:
            raise ScopeDenied("this operation requires a workflow service callback outside the writer")

    def propose_task(self, spec, *, idempotency_key):
        self._outside_writer()
        allowed = {"kind", "role", "bundle_kind", "acceptance", "input", "depends_on", "stage",
                   "team_id", "independent_of_task_id", "max_attempts", "parallel_group", "required_artifacts"}
        if not isinstance(spec, dict) or spec.keys() - allowed or not isinstance(idempotency_key, str) or not idempotency_key:
            raise SchemaInvalid("a task proposal requires named task fields and an operation key")
        key = expansion.fact_key(self.run_id, "plugin", idempotency_key)
        actor = Ctx(self.conn, Principal("backend", "operator", source="backend"))
        return expansion.expand(actor, self.run_id, rule="plugin", key=key, trigger={"key": idempotency_key},
                                tasks=[spec], stage=spec.get("stage"))

    def publish_artifact(self, spec, *, idempotency_key):
        self._outside_writer()
        if (not isinstance(spec, dict) or set(spec) != {"kind", "document"} or not isinstance(spec["kind"], str)
                or not spec["kind"]
                or not isinstance(idempotency_key, str) or not idempotency_key):
            raise SchemaInvalid("artifact publication requires a kind, JSON document and operation key")
        receipt = ArtifactService(self.conn).publish_bytes(
            namespace=f"{self.run_id}/plugin-products", rel_path=ids.sha256_of_obj([self.task_id, idempotency_key])+".json",
            kind=spec["kind"], data=ids.canonical_json(spec["document"]), media_type="application/json",
            run_id=self.run_id, task_id=self.task_id)
        if receipt.kind != spec["kind"]:
            raise SchemaInvalid("the publication key already belongs to an artifact of another kind")
        return {"artifact_ref": receipt.artifact_ref, "sha256": receipt.sha256}

    def _worker(self, tool):
        conn = self.conn
        if self.root is None or tool not in {spec["name"] for spec in self.root._tools_for(self.binding)}:
            raise ScopeDenied(f"{tool} requires an eligible assigned worker callback")
        return conn

    def _command(self, owner, *args, **kwargs):
        # A callback cannot catch a partial owner write and commit its prefix.
        try:
            return owner(*args, **kwargs)
        except Exception as exc:
            self._failed_command = str(exc)
            raise

    def inspect_capability(self, capability_id, version):
        if self.root is not None:
            return compute.call(self._worker("gx_capability_inspect"), self.binding, "gx_capability_inspect",
                                {"capability_id": f"{capability_id}@{version}"})
        return registry.candidate(self.conn, f"{capability_id}@{version}")

    def propose_job(self, proposal, *, idempotency_key):
        if not isinstance(proposal, dict) or "idempotency_key" in proposal:
            raise SchemaInvalid("supply Job fields and a separate operation key")
        return self._command(compute.call, self._worker("gx_job_submit"), self.binding, "gx_job_submit",
                             {**proposal, "idempotency_key": idempotency_key}, join_callback=True)

    def job_status(self, job_id):
        conn = self._worker("gx_job_status")
        result = compute.call(conn, self.binding, "gx_job_status", {"job_id": job_id})
        self._cited(result.get("outputs", []))
        return result

    def wait_job(self, job_id):
        self._worker("gx_wait_job")
        return self._command(self.root._park_job, self.binding, job_id, turn_id=self.envelope["turn_id"])

    def cancel_job(self, job_id, *, reason, idempotency_key):
        return self._command(compute.call, self._worker("gx_job_cancel"), self.binding, "gx_job_cancel",
                             {"job_id": job_id, "reason": reason, "idempotency_key": idempotency_key})

    def propose_evaluation(self, product):
        """Return a workflow product for the existing Root submission and scientific validator."""
        self._worker("gx_submit_product")
        if not isinstance(product, dict):
            raise SchemaInvalid("an evaluation product must be a JSON object")
        return _Product(product)

    def finish_tool(self, answer):
        if self._failed_command is not None:
            raise SchemaInvalid("the plugin callback's Job command failed: " + self._failed_command)
        if isinstance(answer, _Product):
            return self.root._submit(self.binding, answer.document, self.envelope)
        return answer
