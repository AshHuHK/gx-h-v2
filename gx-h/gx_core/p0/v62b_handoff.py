"""Select published scientific inputs from the delegating Lead's frozen evidence."""
import json

from ..artifacts import ArtifactService
from ..domain import ids
from ..errors import ScopeDenied, StaleRevision


_PUBLISHED_ROLES = {
    "module_report_bundle": "module_report",
    "v62_source_extraction": "source_evidence",
    "v62_evidence_review": "source_review",
    "v62_public_method_spec": "method_spec",
}
_GX_RESULT_ROLES = {"module_report", "source_evidence", "source_review", "job_output", "analysis", "model_prediction", "observation"}


def select_claims(conn, task, frozen, requested):
    """Assign exact adopted claim versions as questions, without granting their underlying artifact trees."""
    if not requested:
        return []
    from . import v62_lead
    private = v62_lead.branch_ids(conn, task["run_id"])
    visible = set(json.loads(task["visible_refs_json"]))
    candidates = {key: claim for claim in frozen["claims"]
                  if not claim["decision_scope"].startswith("module:")
                  and claim["decision_scope"].removeprefix("branch:") not in private
                  for key in (claim["claim_revision_id"], claim["claim_ref"]) if key in visible}
    targets = {}
    for ref in requested:
        claim = candidates.get(ref)
        if claim is None:
            raise ScopeDenied("The target claim is outside the delegating Lead's frozen, adopted claims")
        version = claim["claim_revision_id"]
        pin = conn.execute("""
            SELECT r.sha256 FROM hypothesis_revisions r JOIN hypotheses h USING(hypothesis_id)
            JOIN events e ON e.run_id=h.run_id AND e.subject_id=h.hypothesis_id AND e.new_revision=r.revision
            WHERE h.run_id=? AND h.hypothesis_id=? AND r.revision=? AND e.event_id<=?
              AND e.kind IN ('v6.claim.adopted','v62.claim.reassessed')
              AND json_extract(e.payload_json,'$.claim_revision_id')=?
        """, (task["run_id"], claim["hypothesis_id"], claim["revision"], task["event_cursor"], version)).fetchone()
        if not pin or pin[0] != ids.sha256_of_obj(claim):
            raise StaleRevision("The target claim differs from its immutable adoption or reappraisal")
        targets[version] = {"claim_revision_id": version, "statement": claim["statement"], "scope": claim["scope"],
                            "hypothesis_id": claim["hypothesis_id"], "revision": claim["revision"], "sha256": pin[0]}
    return list(targets.values())


def require_claims(conn, run_id, targets):
    for target in targets:
        pin = conn.execute("SELECT r.sha256 FROM hypothesis_revisions r JOIN hypotheses h USING(hypothesis_id) "
                           "WHERE h.run_id=? AND h.hypothesis_id=? AND r.revision=?",
                           (run_id, target["hypothesis_id"], target["revision"])).fetchone()
        if not pin or pin[0] != target["sha256"]:
            raise StaleRevision("The delegated target claim version changed")


def _publications(conn, run_id, cursor, visible):
    """A's immutable receipts enumerate the selected originals, including contrary findings."""
    from . import v62_reports

    anchors = {}
    reports = {}
    for row in v62_reports._released_rows(conn, run_id, cursor):
        report_ref = row["report_ref"]
        if report_ref not in visible:
            continue
        receipt = json.loads(row["receipt_json"])
        reports[report_ref] = receipt
        for ref in receipt["source_refs"]:
            if ref in visible:
                anchors.setdefault(ref, report_ref)
        anchors[report_ref] = report_ref
    return reports, anchors


def _other_publication(conn, task, frozen, ref):
    """A frozen artifact can also be a Run/branch observation or a completed public Job."""
    from . import manifest

    for node in frozen["nodes"]:
        if node["status"] not in ("SUCCEEDED", "REUSED"):
            continue
        if node.get("result_ref") != ref and not any(r["artifact_ref"] == ref for r in node.get("outputs", ())):
            continue
        record = manifest.read_manifest(conn, node["job_id"])
        end = record["completion"]
        if (record["admission"]["run_id"] != task["run_id"] or not end or end["status"] != "SUCCEEDED"
                or not any(o["artifact_ref"] == ref for o in end["outputs"])):
            raise StaleRevision("The frozen Job result differs from its completion receipt")
        return {"node_id": node["node_id"], "job_id": node["job_id"], "completion_sha256": record["manifest_hash"]}
    observation = conn.execute(
        """SELECT event_id FROM events e WHERE e.run_id=? AND e.kind='v6.observation.added'
           AND e.event_id<=? AND json_extract(e.payload_json,'$.artifact_ref')=?
           AND NOT EXISTS (SELECT 1 FROM v62_modules m WHERE m.run_id=e.run_id
               AND json_extract(e.payload_json,'$.scope') IN (m.scope,m.branch_id))
           ORDER BY e.event_id LIMIT 1""", (task["run_id"], task["event_cursor"], ref)).fetchone()
    if observation:
        return {"observation_event": observation[0]}
    from . import v62_sources
    owned = v62_sources._owned(conn, task["run_id"], ref, cursor=task["event_cursor"])
    if owned and json.loads(owned["payload_json"])["scope"] == "run":
        return {"source_event": owned["event_id"]}
    raise ScopeDenied("The selected input is outside the Lead's published scientific results")


def _description(conn, run_id, ref, meta, pin, cursor):
    if meta["origin"] == "JOB_OUTPUT" or "observation_event" in pin:
        from .v62_outputs import classify
        classification = classify(conn, run_id, ref, through_event=cursor)
        kind, nature = classification["record_kind"], classification["nature"]
        role = "supplied_information" if "observation_event" in pin else "job_output"
        if kind in ("test_fixture", "execution_failure"):
            role = kind
        elif nature != "unclassified":
            role = "observation" if nature == "observed" else nature
        return {"role": role, "record_kind": kind, "nature": nature}
    return {"role": _PUBLISHED_ROLES.get(meta["kind"], meta["kind"])}


def select_materials(conn, task, frozen, requested, roles, *, published_only=False):
    """Expand a selected report only to its released sources allowed by the recipient's role grant."""
    originals = {m["artifact_ref"]: m for m in frozen["materials"]}
    visible = set(json.loads(task["visible_refs_json"]))
    explicit = set(requested)
    if not explicit <= visible:
        raise ScopeDenied("The selected input is outside the delegating Lead's frozen evidence")
    reports, anchors = ({}, {})
    if published_only or explicit - originals.keys() or any(originals.get(ref, {}).get("role") in _GX_RESULT_ROLES for ref in explicit):
        reports, anchors = _publications(conn, task["run_id"], task["event_cursor"], visible)
    selected = dict.fromkeys(requested)
    for ref in requested:
        if ref in reports:
            for source in reports[ref]["source_refs"]:
                if source in visible:
                    selected.setdefault(source, None)
                    if source not in reports:
                        anchors[source] = ref

    service = ArtifactService(conn)
    proofs = {}
    for report_ref in {anchors[ref] for ref in selected if ref in anchors}:
        receipt = reports[report_ref]
        original = service.read_json(report_ref)
        sources = {source["artifact_ref"]: source["sha256"] for source in original["sources"]}
        if (original["run_id"] != task["run_id"] or original["module_instance_id"] != receipt["module_instance_id"]
                or set(sources) != set(receipt["source_refs"])
                or service.describe(report_ref)["sha256"] != receipt["report_sha256"]):
            raise StaleRevision("The released report differs from its publication receipt")
        proofs[report_ref] = {**sources, report_ref: receipt["report_sha256"]}

    materials = []
    for ref in selected:
        supplied = originals.get(ref)
        if supplied and not published_only and supplied["role"] not in _GX_RESULT_ROLES:
            material = supplied
        elif supplied and supplied["role"] in _GX_RESULT_ROLES and not published_only and "*" in roles:
            material = {**supplied, "role": "material", "record_kind": "operator_input"}
        else:
            pin = ({"report_ref": anchors[ref]} if ref in anchors else _other_publication(conn, task, frozen, ref))
            meta = service.describe(ref)
            if meta["run_id"] != task["run_id"]:
                raise ScopeDenied("The selected publication belongs to another Run")
            description = _description(conn, task["run_id"], ref, meta, pin, task["event_cursor"])
            if supplied and supplied["role"] not in _GX_RESULT_ROLES and "report_ref" in pin:
                description["role"] = supplied["role"]
            material = dict(artifact_ref=ref, sha256=meta["sha256"], name=meta["kind"], publication=pin, **description)
            if supplied:
                material.update(name=supplied["name"], original_input=True)
        if "*" not in roles and material["role"] not in roles:
            if ref in explicit:
                raise ScopeDenied("This module is not authorized for a delegated input role")
            continue
        verified = service.verify(ref)
        if (not verified["ok"] or verified["observed_sha256"] != material["sha256"]
                or ref in anchors and proofs[anchors[ref]][ref] != material["sha256"]):
            raise StaleRevision("The bytes behind a delegated material changed")
        materials.append(material)
    return materials


def require_materials(conn, run_id, original_refs, materials):
    """Fence chosen publications and original inputs under the same writer as the module grant."""
    for material in materials:
        ref = material["artifact_ref"]
        meta = conn.execute("SELECT sha256 FROM artifacts WHERE run_id=? AND artifact_ref=?", (run_id, ref)).fetchone()
        if not meta or meta[0] != material["sha256"]:
            raise StaleRevision("A delegated material or its content pin changed")
        pin = material.get("publication")
        if material.get("original_input") and ref not in original_refs:
            raise StaleRevision("A published original is no longer an input of this Goal")
        if pin is None:
            current = ref in original_refs
        elif "report_ref" in pin:
            receipt = conn.execute("SELECT receipt_json FROM v62_module_reports WHERE run_id=? AND report_ref=?",
                                   (run_id, pin["report_ref"])).fetchone()
            current = receipt and (ref == pin["report_ref"] or ref in json.loads(receipt[0])["source_refs"])
        elif "node_id" in pin:
            current = conn.execute("SELECT 1 FROM v6_nodes n JOIN p0_execution_manifests m USING(job_id) "
                                   "WHERE n.run_id=? AND n.node_id=? AND n.job_id=? AND m.completion_sha256=? "
                                   "AND n.status IN ('SUCCEEDED','REUSED')",
                                   (run_id, pin["node_id"], pin["job_id"], pin["completion_sha256"])).fetchone()
        elif "source_event" in pin:
            current = conn.execute("SELECT 1 FROM events WHERE run_id=? AND event_id=? AND subject_id=? "
                                   "AND kind IN ('v62.source.extracted','v62.source.reviewed') "
                                   "AND json_extract(payload_json,'$.scope')='run'", (run_id, pin["source_event"], ref)).fetchone()
        else:
            current = conn.execute("SELECT 1 FROM events WHERE run_id=? AND event_id=? AND kind='v6.observation.added' "
                                   "AND json_extract(payload_json,'$.artifact_ref')=?", (run_id, pin["observation_event"], ref)).fetchone()
        if not current:
            raise StaleRevision("The publication or original input behind a delegated material changed")
