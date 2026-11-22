"""Official task-root proposals enter the existing Outbox, Job, Artifact and wait owners."""
import fcntl
import json
import os
import re
from contextlib import nullcontext

from ..artifacts import ArtifactService
from ..capabilities import registry, shipped, storage
from ..capabilities.tools import capability_inspect
from ..context import Ctx, Principal
from ..domain import clock, ids
from ..errors import CapabilityNotApproved, GxError, IdempotencyConflict, SchemaInvalid, ScopeDenied
from ..jobs import leases, tools as job_tools
from ..persistence import store

IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,30}\Z")
CAP = re.compile(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+\Z")
TEXT = {"type": "string", "minLength": 1}
OBJ = {"type": "object"}
SUBMIT, CANCEL = "p0.compute.submit", "p0.compute.cancel"


def _tool(name, summary, fields, required=()):
    return {"type": "function", "name": name, "description": summary,
            "inputSchema": {"type": "object", "properties": fields, "required": list(required),
                            "additionalProperties": False}}


TOOLS = (
    _tool("gx_capability_inspect", "Inspect registered scientific capabilities and release methods.",
          {"capability_id": TEXT, "filters": OBJ, "include": {"type": "array", "items": TEXT},
           "limit": {"type": "integer", "minimum": 1, "maximum": 100}}),
    _tool("gx_job_submit", "Propose a versioned capability on named, cited artifact refs. For artifact-analysis@1 "
          "give one Python object expression using data/params, count/sum/mean/median/stdev/min/max/abs/sqrt/column/sample, "
          "an exact object output_schema and optional seed. Use gx_wait_job with the returned Job ID and end the turn.",
          {"capability_version": TEXT, "inputs": {"type": "object", "additionalProperties": TEXT},
           "parameters": OBJ, "script": TEXT, "output_schema": OBJ, "seed": {"type": "integer"},
           "tighten": OBJ, "idempotency_key": TEXT}, ("capability_version", "inputs", "idempotency_key")),
    _tool("gx_job_status", "Read this attempt's last durable Job observation, outputs or admission refusal.",
          {"job_id": TEXT}, ("job_id",)),
    _tool("gx_job_cancel", "Cancel this attempt's proposed or admitted Job. The runner confirms cancellation.",
          {"job_id": TEXT, "reason": TEXT, "idempotency_key": TEXT}, ("job_id", "reason", "idempotency_key")),
)
TOOL_MAP = {tool["name"]: tool for tool in TOOLS}


def _ctx(conn, row, tool):
    return Ctx(conn=conn, tool=tool, transport="native_app_server",
               principal=Principal(row["thread_id"], "worker", source="official_root_binding"))


def is_analysis_data(conn, artifact_ref):
    row = conn.execute("SELECT a.kind,j.capability_id FROM artifacts a LEFT JOIN jobs j USING(job_id) WHERE a.artifact_ref=?",
                       (artifact_ref,)).fetchone()
    return row is not None and (row["kind"] == "analysis_program" or row["capability_id"] == "artifact-analysis")


def _scope(conn, row, refs, *, internal):
    for ref in refs:
        if not isinstance(ref, str) or ids.kind_of(ref) != "artifact":
            raise SchemaInvalid("inputs must be Artifact refs; paths and URLs are unavailable")
        if internal:
            ok = conn.execute("SELECT 1 FROM artifacts WHERE artifact_ref=? AND (run_id=? OR run_id IS NULL)",
                              (ref, row["run_id"])).fetchone()
        else:
            ok = conn.execute("SELECT 1 FROM p0_root_artifact_reads r JOIN artifacts a USING(artifact_ref)"
                              " WHERE r.root_key=? AND a.artifact_ref=? AND (a.run_id=? OR a.run_id IS NULL)",
                              (row["root_key"], ref, row["run_id"])).fetchone()
        if not ok:
            raise ScopeDenied("the current task has no access to the specified artifact")


def _registered(conn, run_id, cap):
    if cap in shipped.MANIFESTS:
        return  # Built-ins may be initialized when the proposal is drained.
    if not shipped.participates(conn, run_id, cap):
        raise ScopeDenied(f"{cap} is outside the Run composition")
    try:
        registry.resolve(conn, cap)
    except CapabilityNotApproved as exc:
        raise ScopeDenied("this capability version is outside the registered catalog") from exc


def queue_job(conn, row, *, capability_version, inputs, parameters, idempotency_key, tighten=None,
              binding=None, internal=False, analysis=None, join_callback=False):
    from .roots import require_binding
    owner = require_binding(conn, row["run_id"], row["thread_id"])
    if row["root_key"] != owner["root_key"]:
        raise ScopeDenied("superseded task attempt")
    if (not isinstance(capability_version, str) or not CAP.fullmatch(capability_version) or
            not isinstance(idempotency_key, str) or not 0 < len(idempotency_key) <= 200 or
            not isinstance(inputs, dict) or not 1 <= len(inputs) <= 32 or
            any(not isinstance(key,str) or not IDENT.fullmatch(key) for key in inputs) or not isinstance(parameters, dict) or
            not isinstance(tighten or {}, dict) or set(binding or {}) - {"evaluator"}):
        raise SchemaInvalid("invalid version, named input refs, operation key or job parameters")
    if analysis is not None:
        from adapters.local.artifact_analysis.worker import validate_spec
        if capability_version != shipped.ARTIFACT_ANALYSIS or len(inputs) > 8:
            raise SchemaInvalid("analysis programs take one to eight cited JSON artifacts")
        try:
            validate_spec(analysis)
        except (ValueError, TypeError, SyntaxError, RecursionError) as exc:
            raise SchemaInvalid("analysis program refused: " + str(exc)[:200]) from exc
    request = {"capability_version": capability_version, "inputs": inputs, "parameters": parameters,
               "tighten": tighten or {}, "binding": binding or {}, "analysis": analysis, "internal": internal}
    request_text = ids.canonical_json(request)
    if len(request_text.encode()) > 24576 or analysis and len(ids.canonical_json(analysis).encode()) > 16384:
        raise SchemaInvalid("job proposal exceeds its declared size limit")
    if join_callback and not conn.in_transaction:
        raise RuntimeError("an atomic callback proposal requires its active writer")
    with (nullcontext(conn) if join_callback else store.transaction(conn)):
        old = conn.execute("SELECT p.* FROM p0_job_proposals p JOIN p0_roots r USING(root_key)"
                           " WHERE p.run_id=? AND r.task_id=? AND p.operation_key=? ORDER BY p.rowid LIMIT 1",
                           (row["run_id"],row["task_id"],idempotency_key)).fetchone()
        if old:
            if old["request_json"] != request_text:
                raise IdempotencyConflict("this operation already binds different inputs or parameters")
            if old["root_key"] != row["root_key"]:
                _scope(conn,row,inputs.values(),internal=internal)
                job = conn.execute("SELECT * FROM jobs WHERE job_id=?", (old["job_id"],)).fetchone()
                if job:
                    leases.adopt(conn,job,principal=row["thread_id"],actor="backend",
                                 via="gx_job_submit",include_in_transaction=True)
                else:
                    conn.execute("UPDATE p0_attempt_waits SET ended_at=? WHERE root_key=? AND reason='job'"
                                 " AND wait_key=? AND ended_at IS NULL",(clock.now_iso(),old["root_key"],old["job_id"]))
                    store.append_event(conn,kind="job.proposal_transferred",actor="backend",run_id=row["run_id"],
                        subject_kind="task",subject_id=row["task_id"],evidence_level="EXECUTION_OBSERVED",
                        payload={"job_id":old["job_id"],"from_root":old["root_key"],"to_root":row["root_key"]})
                    conn.execute("UPDATE p0_job_proposals SET root_key=?,wait_started=NULL,wait_turn=NULL,wait_ended=NULL"
                                 " WHERE job_id=?", (row["root_key"],old["job_id"]))
            return {"job_id": old["job_id"], "status": job_status(conn, row, old["job_id"])["status"]}
        _registered(conn, row["run_id"], capability_version)
        _scope(conn, row, inputs.values(), internal=internal)
        job_id = ids.new_id("job")
        conn.execute("INSERT INTO p0_job_proposals(job_id,run_id,root_key,operation_key,request_json,state)"
                     " VALUES(?,?,?,?,?,'PROPOSED')", (job_id,row["run_id"],row["root_key"],idempotency_key,request_text))
        store.enqueue_outbox(conn, topic=SUBMIT, payload={"run_id": row["run_id"], "job_id": job_id},
                             idempotency_key=SUBMIT+":"+job_id)
    return {"job_id": job_id, "status": "PROPOSED"}


def validate_proposal(conn, job_id, request):
    if not isinstance(job_id,str) or not re.fullmatch(r"gxj_[0-9a-z]{18}", job_id):
        raise SchemaInvalid("invalid reserved Job ID")
    row = conn.execute("SELECT p.state,r.task_id FROM p0_job_proposals p JOIN p0_roots r USING(root_key)"
                       " WHERE p.job_id=? AND p.run_id=? AND r.root_key=?", (job_id,request["run_id"],
                       (request.get("binding") or {}).get("attempt_id"))).fetchone()
    if not row or row["state"] != "PROPOSED" or row["task_id"] != request.get("task_id"):
        raise ScopeDenied("the original attempt has no open proposal for this Job ID")


def owned_job(conn, row, job_id):
    job = conn.execute("SELECT * FROM jobs WHERE job_id=? AND run_id=? AND task_id=?",
                       (job_id,row["run_id"],row["task_id"])).fetchone()
    if job and leases.job_lease(conn,job)["lease_ref"] != row["lease_id"]:
        raise ScopeDenied("this attempt does not currently own this Job")
    return job


def job_status(conn, row, job_id):
    job = owned_job(conn,row,job_id)
    if job:
        return job_tools.job_status(_ctx(conn, row, "gx_job_status"), {"job_id": job_id}, observed_row=job).data
    req = conn.execute("SELECT state,error_json FROM p0_job_proposals WHERE job_id=? AND root_key=?",
                       (job_id, row["root_key"])).fetchone()
    if not req:
        raise ScopeDenied("this Job belongs to another task or attempt")
    return {"job_id": job_id, "status": req["state"], "terminal": req["state"] in ("REJECTED", "CANCELLED"),
            "outputs": [], "failure": store.loads(req, "error_json")}


def _notice(conn, req):
    if req["wait_turn"] and not req["wait_ended"]:
        store.enqueue_outbox(conn, topic="p0.compute.notice", payload={"run_id": req["run_id"], "job_id": req["job_id"]},
                             idempotency_key="p0.compute.notice:" + req["job_id"])


def deliver_notice(native, payload):
    conn, job_id = native.conn, payload["job_id"]
    row = conn.execute("SELECT p.state,p.error_json,p.wait_ended,r.* FROM p0_job_proposals p JOIN p0_roots r"
                       " USING(root_key) WHERE p.job_id=? AND p.run_id=?", (job_id,payload["run_id"])).fetchone()
    if not row or row["wait_ended"]:
        return
    current = conn.execute("SELECT 1 FROM tasks JOIN runs USING(run_id) WHERE task_id=? AND lease_id=?"
                           " AND tasks.status IN ('CLAIMED','RUNNING') AND runs.status NOT IN ('CANCELLED','FAILED','COMPLETED')",
                           (row["task_id"],row["lease_id"])).fetchone()
    if not current:
        conn.execute("UPDATE p0_job_proposals SET wait_ended=? WHERE job_id=?", (clock.now_iso(),job_id))
        return
    from . import waits
    with store.transaction(conn):
        waits.proposal_wait(conn,row["root_key"],job_id,stage="notice_ready")
    event = "proposal:"+row["lease_id"]+":"+job_id
    native.deliver(row["thread_id"], "GX admission result: " + ids.canonical_json(job_status(conn,row,job_id)), event_id=event)
    with store.transaction(conn):
        waits.proposal_wait(conn,row["root_key"],job_id,stage="notice_delivered")
        conn.execute("UPDATE p0_job_proposals SET wait_ended=? WHERE job_id=?", (clock.now_iso(), job_id))
        conn.execute("UPDATE p0_roots SET goal_desired=NULL WHERE root_key=? AND goal_desired='paused' AND wait_job_id IS NULL",
                     (row["root_key"],))


def park_proposal(conn, row, job_id, turn):
    req = conn.execute("SELECT * FROM p0_job_proposals WHERE job_id=? AND root_key=?", (job_id,row["root_key"])).fetchone()
    if not req:
        raise ScopeDenied("a root may only wait for a Job proposed under its task lease")
    view = job_status(conn,row,job_id)
    if view["terminal"]:
        return {**view, "waiting": False}
    other = pending_wait(conn, row["root_key"])
    if row["wait_job_id"] or other and other["job_id"] != job_id:
        raise ScopeDenied("this attempt is already waiting for a different Job")
    conn.execute("UPDATE p0_job_proposals SET wait_turn=COALESCE(wait_turn,?),wait_started=COALESCE(wait_started,?)"
                 " WHERE job_id=?", (turn,clock.now_iso(),job_id))
    from . import waits
    waits.proposal_wait(conn,row["root_key"],job_id,stage="park",turn_id=turn)
    conn.execute("UPDATE p0_roots SET goal_desired='paused' WHERE root_key=?", (row["root_key"],))
    return {"job_id": job_id, "status": view["status"], "waiting": True}


def pending_wait(conn, root_key, turn=None):
    sql = "SELECT * FROM p0_job_proposals WHERE root_key=? AND wait_turn IS NOT NULL AND wait_ended IS NULL"
    return conn.execute(sql+(" AND wait_turn=?" if turn else ""), (root_key,turn) if turn else (root_key,)).fetchone()


def call(conn, row, name, args, *, join_callback=False):
    spec = TOOL_MAP[name]["inputSchema"]
    if set(args) - set(spec["properties"]) or set(spec["required"]) - set(args):
        raise SchemaInvalid("invalid arguments to " + name)
    for key, value in args.items():
        field = spec["properties"][key]
        expected = {"string":str,"integer":int,"object":dict,"array":list}[field["type"]]
        if type(value) is not expected or (expected is str and not value):
            raise SchemaInvalid(f"{name}.{key} must be a {field['type']}")
        if expected is int and (value < field.get("minimum",value) or value > field.get("maximum",value)):
            raise SchemaInvalid(f"{name}.{key} is outside the allowed range")
        if expected is list and not all(isinstance(k,str) for k in value):
            raise SchemaInvalid(f"{name}.{key} requires string names")
    if name == "gx_capability_inspect":
        from .. import config
        from ..capabilities import manifest as capability_manifest
        requested = args.get("capability_id")
        key, sep, version = (requested or "").partition("@")
        ctx = _ctx(conn, row, name)
        ctx.authz["run_id"] = row["run_id"]
        result = capability_inspect(ctx, {**args, "capability_id": key or None}).data
        if sep:
            result["capabilities"] = [item for item in result["capabilities"] if item["version"] == version]
        # Release methods are discoverable before their first execution. Their local governance state is explicit.
        methods = []
        for cap, relative in sorted(shipped.MANIFESTS.items()):
            if requested not in (None,cap,cap.rsplit("@",1)[0]):
                continue
            method = json.loads((config.HARNESS_ROOT / relative).read_text())["manifest"]
            asset = conn.execute("SELECT status FROM assets WHERE kind='TOOL_ADAPTER' AND asset_id=? AND version=?",
                                 cap.split("@",1)).fetchone()
            info = {"capability_version":cap,"sha256":shipped.pin(cap),"status":asset["status"] if asset else "RELEASE_PINNED",
                    "method_card":method["method_card"], "resources":method["resources"], "limitations":method["limitations"],
                    "env_id":method["env_id"],"kind":method["kind"],"outputs":method["outputs"],
                    "input_roles":capability_manifest.input_roles(method),"parameters":capability_manifest.parameter_names(method)}
            if cap == shipped.ARTIFACT_ANALYSIS:
                info.update(input_roles="one to eight named JSON artifacts",parameters="JSON object exposed to the script as params",
                            program_fields=["script","output_schema","seed (optional)"])
            fields = {**info,"family":method["method_card"]["family"]}
            if all(fields.get(k) == v for k,v in (args.get("filters") or {}).items()):
                methods.append(info)
        result["release_methods"] = methods[:result["limit"]]
        return result
    if name == "gx_job_submit":
        args = dict(args)
        params = args.pop("parameters", {})
        special = {k: args.pop(k) for k in ("script","output_schema","seed") if k in args}
        if args.get("capability_version") == shipped.ARTIFACT_ANALYSIS:
            if not special:
                raise SchemaInvalid("artifact-analysis@1 requires script and output_schema")
            special["parameters"] = params
            return queue_job(conn, row, **args, parameters={}, analysis=special, join_callback=join_callback)
        if special:
            raise SchemaInvalid("analysis script fields apply only to artifact-analysis@1")
        return queue_job(conn,row,**args,parameters=params,join_callback=join_callback)
    status = job_status(conn,row,args["job_id"])
    if name == "gx_job_status" or status["terminal"]:
        return status
    reason, key, job_id = args["reason"],args["idempotency_key"],args["job_id"]
    if not isinstance(reason,str) or not 0 < len(reason) <= 500 or not isinstance(key,str) or not 0 < len(key) <= 200:
        raise SchemaInvalid("a cancellation requires a bounded reason and operation key")
    if status["status"] == "PROPOSED":
        conn.execute("UPDATE p0_job_proposals SET state='CANCELLED' WHERE job_id=? AND state='PROPOSED'", (job_id,))
        _notice(conn, conn.execute("SELECT * FROM p0_job_proposals WHERE job_id=?", (job_id,)).fetchone())
        return {"job_id":job_id, "status":"CANCELLED"}
    data = {"run_id":row["run_id"], "root_key":row["root_key"], "job_id":job_id, "reason":reason}
    oid = store.enqueue_outbox(conn, topic=CANCEL,payload=data,idempotency_key=f"{CANCEL}:{row['root_key']}:{key}")
    if store.loads(conn.execute("SELECT payload_json FROM outbox WHERE outbox_id=?", (oid,)).fetchone(),"payload_json") != data:
        raise IdempotencyConflict("that cancellation key names a different request")
    return {"job_id":job_id, "status":"CANCEL_REQUESTED"}


def _submit(conn, row, proposal):
    from .roots import require_binding
    from . import waits
    args = store.loads(proposal,"request_json")
    job_id = proposal["job_id"]
    job = conn.execute("SELECT job_id FROM jobs WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        from .team_admission import team_funding
        team_funding(conn,row["run_id"],row["task_id"],None,admit=True)
        with store.transaction(conn):
            waits.proposal_wait(conn,row["root_key"],job_id,stage="admission")
        require_binding(conn, row["run_id"], row["thread_id"])
        _scope(conn,row,args["inputs"].values(), internal=args["internal"])
        ctx = _ctx(conn,row,"gx_job_submit")
        cap = args["capability_version"]
        if cap in shipped.MANIFESTS:
            shipped.ensure(ctx,cap)
        artifacts = ArtifactService(conn)
        inputs = args["inputs"]
        if args["analysis"] is not None:
            total = sum(artifacts.describe(ref)["bytes"] for ref in inputs.values())
            if total > 1048576:
                raise SchemaInvalid("selected JSON artifacts exceed the 1 MiB combined limit")
            ref = artifacts.publish_bytes(namespace=row["run_id"]+"/analysis",rel_path=job_id+"/program.json",
                    kind="analysis_program",media_type="application/json",data=ids.canonical_json(args["analysis"]),
                    run_id=row["run_id"],task_id=row["task_id"],actor=row["thread_id"]).artifact_ref
            inputs = {"spec":ref, **{"input_"+k:v for k,v in inputs.items()}}
        manifest = artifacts.make_input_manifest(namespace=row["run_id"]+"/compute/inputs",run_id=row["run_id"],
            task_id=row["task_id"],entries=[{"role":k,"artifact_ref":v} for k,v in sorted(inputs.items())])
        binding = {**args["binding"],"task_id":row["task_id"],"attempt_id":row["root_key"]}
        job_tools.job_submit(ctx,{"run_id":row["run_id"],"task_id":row["task_id"],"lease_ref":row["lease_id"],
             "capability_version":cap,"input_manifest_ref":manifest.artifact_ref,"parameters":args["parameters"],
             "tighten":args["tighten"],"idempotency_key":"p0-proposal:"+job_id}, binding=binding,reserved_job_id=job_id)
    with store.transaction(conn):
        req = conn.execute("SELECT * FROM p0_job_proposals WHERE job_id=?", (job_id,)).fetchone()
        conn.execute("UPDATE p0_job_proposals SET state='ADMITTED',error_json=NULL WHERE job_id=?", (job_id,))
        if req["wait_turn"] and not req["wait_ended"]:
            conn.execute("UPDATE p0_roots SET wait_job_id=?,goal_desired='paused' WHERE root_key=?", (job_id,row["root_key"]))
            waits.begin(conn,row["root_key"],"job",job_id,started_at=req["wait_started"],turn_id=req["wait_turn"])
            conn.execute("UPDATE p0_job_proposals SET wait_ended=? WHERE job_id=?", (clock.now_iso(),job_id))


def drain(conn, run_id):
    """One coordinator pass for outstanding requests. Scientific work stays in detached Jobs."""
    rows = conn.execute("SELECT * FROM outbox WHERE run_id=? AND consumed_at IS NULL AND topic IN (?,?)"
                        " ORDER BY outbox_id LIMIT 32", (run_id,SUBMIT,CANCEL)).fetchall()
    if not rows:
        return
    # An overlapping cold coordinator must not stage the same preallocated ID twice.
    fd = os.open(storage.store_dir(conn),os.O_RDONLY | os.O_DIRECTORY)
    try:
        try:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            return
        for outbox in rows:
            p = store.loads(outbox,"payload_json")
            req = conn.execute("SELECT * FROM p0_job_proposals WHERE job_id=?", (p["job_id"],)).fetchone()
            root_key = p.get("root_key") or req["root_key"]
            root = conn.execute("SELECT * FROM p0_roots WHERE root_key=?", (root_key,)).fetchone()
            state = conn.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()[0]
            if outbox["topic"] == SUBMIT and req["state"] == "PROPOSED" and state == "PAUSED":
                continue
            try:
                if outbox["topic"] == SUBMIT and req["state"] == "PROPOSED":
                    _submit(conn,root,req)
                elif outbox["topic"] == CANCEL:
                    from .roots import require_binding
                    require_binding(conn,run_id,root["thread_id"])
                    job_status(conn,root,p["job_id"])
                    job_tools.job_cancel(_ctx(conn,root,"gx_job_cancel"), {"job_id":p["job_id"], "reason":p["reason"],
                                         "idempotency_key":outbox["idempotency_key"]})
            except GxError as exc:
                if outbox["topic"] == SUBMIT:
                    current = conn.execute("SELECT root_key FROM p0_job_proposals WHERE job_id=?", (p["job_id"],)).fetchone()
                    if current["root_key"] != root_key:
                        continue
                    if exc.details.get("reason") in ("team_paused", "soft_budget"):
                        detail = {"code":exc.code, "reason":exc.details["reason"], "message":exc.message}
                        conn.execute("UPDATE p0_job_proposals SET error_json=? WHERE job_id=? AND state='PROPOSED'",
                                     (ids.canonical_json(detail),p["job_id"]))
                        continue
                # If admission committed before transport/process failure the Job remains authoritative.
                if outbox["topic"] == SUBMIT and conn.execute("SELECT 1 FROM jobs WHERE job_id=?", (p["job_id"],)).fetchone():
                    _submit(conn,root,req)
                elif outbox["topic"] == SUBMIT:
                    with store.transaction(conn):
                        conn.execute("UPDATE p0_job_proposals SET state='REJECTED',error_json=? WHERE job_id=? AND state='PROPOSED'",
                                     (ids.canonical_json({"code":exc.code,"message":exc.message}),p["job_id"]))
                        _notice(conn,req)
                else:
                    with store.transaction(conn):
                        store.fail_outbox(conn,outbox["outbox_id"],str(exc))
            with store.transaction(conn):
                store.complete_outbox(conn,outbox["outbox_id"],"p0.compute")
    finally:
        os.close(fd)
