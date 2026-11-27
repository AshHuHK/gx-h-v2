"""Open an explicitly requested follow-up from the immutable conclusion of a Goal."""
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import tempfile

from ..artifacts import ArtifactService
from ..artifacts.service import sync_directory_chain
from ..domain import ids
from ..errors import IdempotencyConflict, SchemaInvalid
from ..native_release import native_state, ReleaseStateError
from ..persistence import store
from . import coordinator, research_report, v621_assignment, workspace


def _freeze(directory, name, digest, write):
    """Freeze exactly the bytes later read by normal Goal admission, even across a crash."""
    if not name or name != Path(name).name or name in (".", ".."):
        raise SchemaInvalid("A successor material needs a file name")
    target = directory / digest / name
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as stream:
        tmp = Path(stream.name)
        try:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
            try:
                os.link(tmp, target)
            except FileExistsError:
                if ids.sha256_of_file(target) != digest:
                    raise SchemaInvalid("The frozen successor input bytes have changed")
        finally:
            tmp.unlink(missing_ok=True)
    sync_directory_chain(target.parent, directory.parent)
    return {"path": str(target), "sha256": digest}


def _pin(directory, name, raw):
    return _freeze(directory, name, hashlib.sha256(raw).hexdigest(), lambda stream: stream.write(raw))


def freeze_files(directory, options):
    """Pin local operator inputs outside the inbox write lock, before acknowledging delivery."""
    local = options.get("input_files", [])
    if not isinstance(local, list):
        raise SchemaInvalid("Successor input_files must be a list")
    if not local:
        return options
    stage = directory / "successor-inputs"
    prepared = []
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
            entry["acquisition"] = _pin(stage, "acquisition.json",
                v621_assignment._bytes(item["acquisition"]))["path"]
        role = item.get("role", "material")
        if not isinstance(role, str) or not role.strip():
            raise SchemaInvalid("The selected material role must be text")
        prepared.append({**entry, "role": role})
    return {**options, "input_files": prepared}


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


def _output_materials(conn, rid, close, selections):
    """Select exact scientific outputs that were public to the closing Lead."""
    from . import v62_outputs
    if not isinstance(selections, list):
        raise SchemaInvalid("parent_outputs must be a list of named output bindings")
    if not selections:
        return []
    row = conn.execute("""SELECT t.visible_refs_json FROM v6_decision_tasks t JOIN v6_decisions d USING(run_id,task_id)
        WHERE d.run_id=? AND d.decision_ref=? AND t.scope='run'""", (rid, close["decision_ref"])).fetchone()
    if row is None:
        raise SchemaInvalid("The closing Lead has no saved visibility snapshot for this Run")
    visible = set(json.loads(row[0]))
    inventory, selected, seen = {}, [], set()
    for pin in selections:
        keys = {"job_id", "output_name", "artifact_ref", "sha256", "role"}
        if not isinstance(pin, dict) or set(pin) != keys or not all(
                isinstance(value, str) and value.strip() for value in pin.values()):
            raise SchemaInvalid("A parent output requires job_id, output_name, artifact_ref, sha256 and target role")
        ref, job = pin["artifact_ref"], pin["job_id"]
        if ref not in visible or (ref, pin["role"]) in seen:
            raise SchemaInvalid("Choose a published output visible to the closing Lead once per target role")
        seen.add((ref, pin["role"]))
        if job not in inventory:
            if not conn.execute("SELECT 1 FROM jobs WHERE run_id=? AND job_id=? AND status='SUCCEEDED'",
                                (rid, job)).fetchone():
                raise SchemaInvalid("The selected Job did not succeed on the sealed parent")
            inventory[job] = {out["output_name"]:out for out in v62_outputs.published_outputs(conn, rid, job)}
        output = inventory[job].get(pin["output_name"])
        if not output or output["artifact_ref"] != ref or output["sha256"] != pin["sha256"]:
            raise SchemaInvalid("The selected parent Job, named output, artifact and digest must agree")
        fact = v62_outputs.classify(conn, rid, ref, through_event=close["closed_event"])
        origin = {"run_id": rid, "job_id": job, "output_name": pin["output_name"], "artifact_ref": ref,
                  "sha256": output["sha256"], "nature": fact["nature"], "record_kind": fact["record_kind"]}
        selected.append({"name": Path(pin["output_name"]).name, "sha256": pin["sha256"], "role": pin["role"],
                         "artifact_ref": ref, "source_output": origin})
    return selected


def transferred_output(conn, rid, ref, cursor):
    row = conn.execute("""SELECT json_extract(pin.value,'$.source_output') FROM events e,
        json_each(e.payload_json,'$.material_transfers') pin
        WHERE e.run_id=? AND e.kind='v6.research.successor_opened' AND e.event_id<=?
          AND json_extract(pin.value,'$.artifact_ref')=? AND json_type(pin.value,'$.source_output')='object'
        LIMIT 1""", (rid, cursor, ref)).fetchone()
    if not row:
        return None
    # Earlier versions stored using only (digest, basename). Two selected slots could alias
    # one local ref, including a PI attachment with no transferred-output provenance.
    different = conn.execute("""SELECT 1 FROM runs r, json_each(r.inputs_json,'$.files') f
        WHERE r.run_id=? AND json_extract(f.value,'$.artifact_ref')=? AND
        json_extract(r.inputs_json,'$.inputs.p0_spec.predecessor.selected_materials['||f.key||'].source_output') IS NOT ?
        LIMIT 1""", (rid, ref, row[0])).fetchone()
    if different:
        return {"nature": "unclassified", "record_kind": "ambiguous_transfer"}
    return json.loads(row[0])


def _spec(conn, directory, parent, close, mid, text, options):
    rid = parent["run_id"]
    if not isinstance(options, dict) or set(options) - {
        "parent_material_refs", "input_files", "research_effort", "include_message_attachments", "parent_method_options", "parent_outputs"
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
        "project", "workspace", "budget_profile", "turn_token_budget",
        "research_effort", "research_modules", "module_policy", "branch_delegation", "initial_job_limit",
        "capabilities", "plugins", "max_parallel_roots"
    }}
    # New studies inherit the live workspace budget; the parent bootstrap may predate a funded extension.
    from . import capability_views
    if context["inputs"].get("discovery_policy") == capability_views.DISCOVERY_POLICY:
        spec["discovery_policy"] = capability_views.DISCOVERY_POLICY
        spec["capability_options"] = list(capability_views.goal_options(context["inputs"]).values())
    if "parent_method_options" in options:
        from ..plugins import goal
        choices = options["parent_method_options"]
        if (not isinstance(choices, list) or not all(isinstance(name, str) for name in choices)
                or len(set(choices)) != len(choices)):
            raise SchemaInvalid("Select distinct, exact parent method references")
        active = {row["capability_version"]: row for row in goal.catalog(conn, rid)}
        if set(choices) - active.keys():
            raise SchemaInvalid("A successor method option must be active in the sealed parent")
        options_for_lead = []
        for name in choices:
            row = active[name]
            option = {"method": name, "digest": row["portable_sha256"]}
            if owner := row.get("plugin"):
                option["provider"] = owner["id"] + "@" + owner["version"]
                if "discovery_policy" in spec:
                    option["provider_digest"] = owner["sha256"]
            options_for_lead.append(option)
        spec["capability_options"] = options_for_lead
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
    outputs = _output_materials(conn, rid, close, options.get("parent_outputs", []))
    for item in [public[ref] for ref in prior] + uploaded + outputs:
        ref = item["artifact_ref"]
        entry = _freeze(stage, item["name"], item["sha256"],
                        lambda stream: assets.copy_verified(ref, stream, expected_sha256=item["sha256"]))
        origin = item.get("source_output") or transferred_output(conn, rid, ref, close["closed_event"])
        descriptor = None if origin else _prior_descriptor(conn, rid, ref)
        if descriptor:
            acquisitions[str(len(files))] = _pin(stage, "acquisition.json", ids.canonical_json(descriptor).encode())["path"]
        files.append({**entry, "role": item.get("role") or "material"})
        sources.append({"parent_artifact_ref": ref, "sha256": entry["sha256"], "name": item["name"],
                        "role": files[-1]["role"], **({"source_output": origin} if origin else {})})
    # Early conversation journals stored the original local options. An already queued
    # start keeps its frozen specification; an inbox-only request pins its inputs once here.
    # Its request_digest below still binds the unchanged, originally recorded envelope.
    if local and not all(isinstance(item, dict) and {"path","role","sha256"} <= item.keys()
                         and Path(item["path"]).parent == stage / item["sha256"] for item in local):
        local = freeze_files(directory, options)["input_files"]
    for item in local:
        material = {key: item[key] for key in ("path","role","sha256")}
        if "acquisition" in item:
            acquisitions[str(len(files))] = item["acquisition"]
        files.append(material)
        sources.append({"name": Path(material["path"]).name, "role": material["role"], "sha256": material["sha256"]})
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
    transfers = []
    for material, source in zip(materials, predecessor["selected_materials"], strict=True):
        if source["sha256"] != material["sha256"]:
            raise SchemaInvalid("Successor material changed during admission")
        if source.get("parent_artifact_ref"):
            transfer = {"parent_artifact_ref": source["parent_artifact_ref"], "artifact_ref": material["artifact_ref"],
                        "sha256": source["sha256"]}
            if origin := source.get("source_output"):
                transfer["source_output"] = origin
            transfers.append(transfer)
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
