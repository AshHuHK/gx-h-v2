"""Open an explicitly requested follow-up from the immutable conclusion of a Goal."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import IdempotencyConflict, SchemaInvalid
from ..native_release import native_state, ReleaseStateError
from ..persistence import store
from . import coordinator, research_report, v621_assignment, workspace


def _pin(directory, name, raw):
    """Freeze exactly the bytes later read by normal Goal admission, even across a crash."""
    if not name or name != Path(name).name or name in (".", ".."):
        raise SchemaInvalid("A successor material needs a file name")
    digest = hashlib.sha256(raw).hexdigest()
    target = directory / digest / name
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(tmp, target)
            except FileExistsError:
                if ids.sha256_of_file(target) != digest:
                    raise SchemaInvalid("The frozen successor input bytes have changed")
        finally:
            tmp.unlink(missing_ok=True)
    return {"path": str(target), "sha256": digest}


def _attachments(conn, rid, message_id):
    from . import pi_materials
    return [item for item in pi_materials.for_message(conn, rid, message_id) if not item.get("branch_id")]


def _prior_descriptor(conn, rid, ref):
    row = conn.execute("""SELECT json_extract(payload_json,'$.acquisition') FROM events
        WHERE run_id=? AND kind='v6.observation.added' AND json_extract(payload_json,'$.scope')='run'
        AND json_extract(payload_json,'$.artifact_ref')=? AND json_type(payload_json,'$.acquisition')='object'
        ORDER BY event_id LIMIT 1""", (rid, ref)).fetchone()
    if not row:
        return None
    receipt = json.loads(row[0])
    if receipt.get("data_refs") or receipt.get("derived_from"):
        # Those edges cite parent-namespace artifacts. Never reinterpret as an independent child sample.
        return None
    deps = {k: (None if k in receipt["unknown_dependencies"] else v)
            for k, v in receipt["dependencies"].items()}
    if not isinstance(deps.get("source"), list) or len(deps["source"]) != 1:
        return None
    deps["source"] = deps["source"][0]
    return {**deps, "sha256": receipt["sha256"], "applicability": receipt.get("applicability")}


def _spec(conn, directory, parent, close, mid, text, options):
    rid = parent["run_id"]
    if not isinstance(options, dict) or set(options) - {
        "parent_material_refs", "input_files", "research_effort", "include_message_attachments"
    }:
        raise SchemaInvalid("Select prior material references, new input files or message attachments for the successor")
    prior = options.get("parent_material_refs", [])
    local = options.get("input_files", [])
    include = options.get("include_message_attachments", False)
    if (not isinstance(prior, list) or not all(isinstance(ref, str) and ref for ref in prior)
            or len(prior) != len(set(prior)) or not isinstance(local, list) or type(include) is not bool):
        raise SchemaInvalid("Select each prior material once and supply input_files as a list")
    context = store.loads(parent, "inputs_json")
    grant = context["inputs"]["p0_spec"]
    spec = {k: v for k, v in grant.items() if k in {
        "project", "workspace", "budget_profile", "turn_token_budget", "workspace_budget",
        "research_effort", "research_modules", "module_policy", "branch_delegation", "initial_job_limit",
        "capabilities", "plugins", "max_parallel_roots"
    }}
    if "research_effort" in options:
        if options["research_effort"] not in ("quick", "medium", "deep"):
            raise SchemaInvalid("Choose quick, medium or deep research effort")
        spec["research_effort"] = options["research_effort"]
    public = {item["artifact_ref"]: {**item, "name": Path(item["path"]).name}
              for item in context["files"]}
    public.update({item["artifact_ref"]: item for item in v621_assignment.for_lead(
        conn, rid, close["goal_version"], close["closed_event"])})
    uploaded = _attachments(conn, rid, mid) if include else []
    files, sources, acquisitions = [], [], {}
    stage = directory / "successor-inputs"
    assets = ArtifactService(conn)
    for ref in prior:
        if ref not in public:
            raise SchemaInvalid("The selected material is outside the parent Goal's public originals")
    for item in [public[ref] for ref in prior] + uploaded:
        ref = item["artifact_ref"]
        entry = _pin(stage, item["name"], assets.read_bytes(ref, expected_sha256=item["sha256"]))
        descriptor = _prior_descriptor(conn, rid, ref)
        if descriptor:
            acquisitions[str(len(files))] = _pin(stage, "acquisition.json", ids.canonical_json(descriptor).encode())["path"]
        files.append({**entry, "role": item.get("role") or "material"})
        sources.append({"parent_artifact_ref": ref, "sha256": entry["sha256"], "name": item["name"],
                        "role": files[-1]["role"]})
    for item in local:
        if (not isinstance(item, dict) or set(item) - {"path", "role", "sha256", "acquisition"}
                or not isinstance(item.get("path"), str) or not Path(item["path"]).is_absolute()):
            raise SchemaInvalid("A new material requires an absolute local path and optional role, sha256, acquisition")
        source = Path(item["path"])
        entry = _pin(stage, source.name, v621_assignment._bytes(source))
        if "sha256" in item and item["sha256"] != entry["sha256"]:
            raise SchemaInvalid("New successor material differs from the selected SHA-256")
        if "acquisition" in item:
            if not isinstance(item["acquisition"], str) or not Path(item["acquisition"]).is_absolute():
                raise SchemaInvalid("A successor acquisition requires a local descriptor path")
            acquisitions[str(len(files))] = _pin(stage, "acquisition.json",
                v621_assignment._bytes(item["acquisition"]))["path"]
        role = item.get("role", "material")
        if not isinstance(role, str) or not role.strip():
            raise SchemaInvalid("The selected material role must be text")
        files.append({**entry, "role": role})
        sources.append({"name": source.name, "role": role, "sha256": entry["sha256"]})
    decision = research_report.closing_decision(conn, rid, close)
    predecessor = {"parent_run_id": rid, "parent_decision_ref": close["decision_ref"],
                   "parent_goal_version": close["goal_version"], "closed_event": close["closed_event"],
                   "request_message_id": mid, "request_digest": ids.sha256_of_obj([text, options]),
                   "selected_materials": sources, "prior_conclusion": decision}
    return {**spec, "project": parent["project_ref"], "mode": "goal", "objective": text, "input_files": files,
            "predecessor": predecessor, **({"material_acquisitions": acquisitions} if acquisitions else {})}


def verify_admission(conn, run_key, spec):
    """Only the saved, explicitly requested workspace start can establish successor lineage."""
    prior = spec["predecessor"]
    parent, mid = prior["parent_run_id"], prior["request_message_id"]
    expected = "successor-" + ids.sha256_of_obj([parent, mid])
    row = conn.execute("SELECT payload_json FROM outbox WHERE idempotency_key=?",
                       (coordinator._start_key(expected),)).fetchone()
    if (expected != run_key or row is None or store.loads(row, "payload_json").get("spec") != spec
            or research_report.completed_close(conn, parent)["decision_ref"] != prior["parent_decision_ref"]):
        raise SchemaInvalid("Open successor research from its explicitly requested, sealed parent study")
    message = conn.execute("""SELECT m.message_text,c.successor_json FROM p0_operator_messages m
        JOIN p0_conversation_messages c USING(message_id) WHERE run_id=? AND message_id=?""", (parent, mid)).fetchone()
    if (not message or not message["successor_json"] or message["message_text"] != spec["objective"]
            or ids.sha256_of_obj([message["message_text"], json.loads(message["successor_json"])]) != prior["request_digest"]):
        raise SchemaInvalid("The successor admission differs from the recorded PI request")


def record_transfer(conn, rid, predecessor, materials):
    """Pin the identity of publicly selected prior originals at the child's admission cut."""
    transfers = [{"parent_artifact_ref": source["parent_artifact_ref"], "artifact_ref": material["artifact_ref"],
                  "sha256": source["sha256"]}
                 for material, source in zip(materials, predecessor["selected_materials"], strict=True)
                 if source.get("parent_artifact_ref") and source["sha256"] == material["sha256"]]
    store.append_event(conn, run_id=rid, kind="v6.research.successor_opened", actor="gx-v6",
                       subject_kind="message", subject_id=predecessor["request_message_id"],
                       payload={"parent_run_id":predecessor["parent_run_id"],
                                "parent_decision_ref":predecessor["parent_decision_ref"],
                                "material_transfers":transfers})


def request(conn, directory, parent_run_id, message_id, text, options, *, native_owner=False):
    """Resume a durable PI message after ingress, including after creation but before reply."""
    message = conn.execute("SELECT m.message_text, c.successor_json FROM p0_operator_messages m "
                           "JOIN p0_conversation_messages c USING(message_id) WHERE m.run_id=? AND m.message_id=?",
                           (parent_run_id, message_id)).fetchone()
    if (not message or message[0] != text or not text.strip() or message["successor_json"] is None
            or json.loads(message["successor_json"]) != options):
        raise SchemaInvalid("A successor requires the original recorded PI message on its parent Run")
    parent = workspace.find_run(conn, run_id=parent_run_id)
    close = research_report.completed_close(conn, parent_run_id)
    key = "successor-" + ids.sha256_of_obj([parent_run_id, message_id])
    anchor = {"run_key": key, "parent_run_id": parent_run_id, "parent_decision_ref": close["decision_ref"],
              "request_message_id": message_id}
    queued = conn.execute("SELECT payload_json FROM outbox WHERE idempotency_key=?",
                          (coordinator._start_key(key),)).fetchone()
    directory = Path(directory).resolve()
    if queued:
        spec = store.loads(queued, "payload_json")["spec"]
        if spec["predecessor"]["request_digest"] != ids.sha256_of_obj([text, options]):
            raise IdempotencyConflict("This successor request is bound to different materials or options")
    else:
        spec = _spec(conn, directory, parent, close, message_id, text, options)
    coordinator.queue_start(conn, key, spec)
    child = workspace.find_run(conn, run_key=key)
    if not child:
        try:
            with (nullcontext() if native_owner else native_state(directory)):
                coordinator.drain_starts(conn, directory)
        except ReleaseStateError as exc:
            if "another native client" not in str(exc):
                raise
        child = workspace.find_run(conn, run_key=key)
    if not child:
        pending = conn.execute("SELECT consumed_at,last_error,outbox_id FROM outbox WHERE idempotency_key=?",
                               (coordinator._start_key(key),)).fetchone()
        if pending["consumed_at"] and pending["last_error"]:
            raise SchemaInvalid(pending["last_error"])
        return {"status":"queued_for_active_driver", "outbox_id":pending["outbox_id"], **anchor}
    return {"status": "successor_started", **anchor, "run_id": child["run_id"], "goal_version": 1}
