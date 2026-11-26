"""Register a Job's verified archive bytes as original material on the scientific graph."""
import json

from ..artifacts import ArtifactService
from ..errors import SchemaInvalid
from ..persistence import store
from . import manifest, v621_assignment, v622_sources, v62_catalog, v62_outputs


def declared(record):
    return next((d for d in record["admission"].get("output_declarations", ())
                 if d.get("acquired_material")), None)


def prepare(conn, node, job_id):
    # Ordinary measurement Jobs do not require a second manifest or any file reads.
    exists = conn.execute("""SELECT 1 FROM p0_execution_manifests m,json_each(m.admission_json,'$.output_declarations') d
        WHERE m.job_id=? AND json_type(d.value,'$.acquired_material')='object' LIMIT 1""", (job_id,)).fetchone()
    if not exists:
        return None
    record = manifest.read_manifest(conn, job_id)
    decl = declared(record)
    rules = decl["acquired_material"]
    rid = node["run_id"]
    outputs, _ = v62_outputs.recorded_outputs(conn, rid, job_id, record=record, check_publications=True)
    by_name = {o["output_name"]: o for o in outputs}
    raw, descriptor = by_name[decl["name"]], by_name[rules["metadata_output"]]
    if raw["nature"] != "observed" or record["admission"]["reproducibility"]["level"] == "deterministic_synthetic_cpu":
        raise SchemaInvalid("An acquired original requires an observed archive transfer")
    assets = ArtifactService(conn)
    meta = json.loads(assets.read_bytes(descriptor["artifact_ref"], expected_sha256=descriptor["sha256"]))
    if not isinstance(meta, dict) or meta.get("sha256") != raw["sha256"] or not isinstance(meta.get("source"), str):
        raise SchemaInvalid("The acquired original must match the named source descriptor and SHA-256")
    source = meta["source"]
    pub = assets.describe(raw["artifact_ref"])
    identity = v622_sources.prepare(conn, rid, source, meta, raw["sha256"], pub["media_type"], ref=raw["artifact_ref"])
    if (not identity or identity.get("status") != "matched_accession" or
            (identity.get("revision") or {}).get("status") != "verified"):
        raise SchemaInvalid("An acquired coordinate source must match its archive accession and revision")
    material = dict(artifact_ref=raw["artifact_ref"], sha256=raw["sha256"], name=identity["entry_id"] + ".cif",
                    role=rules["role"], source=source, job_id=job_id,
                    descriptor_ref=descriptor["artifact_ref"], completion_sha256=record["manifest_hash"])
    material["receipt"] = v62_outputs.acquisition_receipt(conn, rid, raw["artifact_ref"], meta,
        source_id=source, prepared_identity=identity, acquired_job=material)
    return material


def attach(conn, node, material):
    """In the node settlement writer, reuse the same published artifact in every subsequent grant."""
    from . import v6_goal
    rid, ref = node["run_id"], material["artifact_ref"]
    version = conn.execute("SELECT goal_version FROM v6_goals WHERE run_id=?", (rid,)).fetchone()[0]
    detail = {key: material[key] for key in ("artifact_ref", "sha256", "name", "role", "source", "job_id",
                                             "descriptor_ref", "completion_sha256")}
    detail.update(goal_version=version, node_id=node["node_id"], decision_ref=node["decision_ref"])
    assigned = conn.execute("""SELECT event_id FROM events WHERE run_id=? AND kind=? AND subject_id=?
        AND json_extract(payload_json,'$.goal_version')=? ORDER BY event_id LIMIT 1""",
        (rid, v621_assignment.ASSIGNED, ref, version)).fetchone()
    if assigned:
        event = assigned[0]
    else:
        observation = v6_goal.record_observation(conn, rid, ref, source=material["source"] + "#" + ref,
                                                  acquired_receipt=material["receipt"])
        detail["acquisition_event_id"] = observation["event_id"]
        event = store.append_event(conn, run_id=rid, kind=v621_assignment.ASSIGNED, actor="gx:job",
                                   subject_kind="artifact", subject_id=ref, payload=detail)
    module = conn.execute("SELECT module_instance_id,scope,module_id,module_version FROM v62_modules "
                          "WHERE run_id=? AND branch_id=? AND status='OPEN'",
                          (rid, node["branch_id"])).fetchone()
    if not module:
        return
    contract = v62_catalog.resolve(conn, rid, module["module_id"], module["module_version"])
    if contract.get("published_inputs_only") or not {"*", material["role"]}.intersection(contract["input_roles"]):
        return
    if not conn.execute("""SELECT 1 FROM events WHERE run_id=? AND kind=? AND subject_id=?
            AND json_extract(payload_json,'$.artifact_ref')=?""", (rid, v621_assignment.GRANTED, module[0], ref)).fetchone():
        store.append_event(conn, run_id=rid, kind=v621_assignment.GRANTED, actor="gx:job",
            subject_kind="module", subject_id=module[0], payload=dict(detail, module_instance_id=module[0],
                scope=module[1], assignment_event_id=event))
