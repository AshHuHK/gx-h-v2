"""Rebuild typed endpoint availability from store-local immutable acquisition and Job receipts.

Chronology can consider facts outside the recipient's scope. Its public projection consists of
the registered material and ordered events, never another Run's artifact, value or source.
"""
from __future__ import annotations

import json
from collections import defaultdict

from ..artifacts import ArtifactService
from ..errors import SchemaInvalid
from ..domain import ids
from . import v62_maturity, v622_measurement


RULE = "gx.claim-proposition/3"


def endpoint(prediction, method_pin):
    if method_pin:
        return {k: value for k, value in method_pin["measurement_spec"].items() if k != "applicability"}
    return {k: prediction[k] for k in ("measurement_schema", "observable", "target", "external_endpoint") if k in prediction}


def source_identity(acquisition):
    deps = acquisition.get("dependencies", {})
    axes = ("acquisition", "source", "sample")
    if all(deps.get(k) for k in axes):
        return ids.sha256_of_obj({k: sorted(deps[k]) for k in axes})
    return None


def distinguish_samples(conn, run, pins):
    """Duplicate digests can identify distinct samples only with separate acquired versions."""
    groups = defaultdict(list)
    for pin in pins:
        groups[pin["sha256"]].append(pin)
    for group in groups.values():
        if len(group) < 2:
            continue
        identities = set()
        for pin in group:
            rows = conn.execute("""SELECT json_extract(payload_json,'$.acquisition') FROM events
                WHERE run_id=? AND kind='v6.observation.added'
                AND json_extract(payload_json,'$.artifact_ref')=? AND json_type(payload_json,'$.acquisition')='object'""",
                (run, pin["artifact_ref"]))
            sources = {source_identity(json.loads(row[0])) for row in rows}
            if len(sources) != 1 or None in sources or sources & identities:
                raise SchemaInvalid("Equal material bytes require separately identified original acquisitions")
            source, = sources
            identities.add(source)
            pin["material_key"] = ids.sha256_of_obj([pin["sha256"], source])


def external_measurement(conn, ref, pred):
    try:
        document = ArtifactService(conn).read_json(ref)
    except SchemaInvalid:
        return None, None
    if not isinstance(document, dict) or document.get("schema") != pred["measurement_schema"]:
        return None, None
    expected = pred.get("external_endpoint", {})
    declared = document.get("endpoint", document)
    if not isinstance(declared, dict):
        return None, None
    dimensions = {k: declared[k] for k in ("unit", "instrument", "selection") if k in declared}
    if expected != dimensions:
        return None, None
    return v62_maturity._scalar_at(document, pred["observable"])


class _Materials:
    """Keep custody identities separate; joins require actual parentage or matching acquisition."""
    def __init__(self):
        self.parent = {}

    def key(self, ref):
        parent = self.parent
        while ref in parent:
            next_ref = parent[ref]
            if next_ref in parent:
                parent[ref] = parent[next_ref]
            ref = next_ref
        return ref

    def join(self, left, right):
        a, b = self.key(left), self.key(right)
        if a != b:
            self.parent[a] = b


def availability(conn, reg, through, *, bindings=None):
    """Earliest provable availability of this typed endpoint, for each requested material."""
    pred = reg["prediction"]
    mats = _Materials()
    pubs = {r["subject_id"]: dict(r) for r in conn.execute("""
        SELECT e.event_id,e.subject_id,a.sha256,a.media_type,a.rel_path,a.kind,
               json_extract(e.payload_json,'$.source_role') source_role FROM events e
        JOIN artifacts a ON a.artifact_ref=e.subject_id WHERE e.kind='artifact.published'
        AND e.event_id<=? AND a.published=1 AND a.origin='INGEST' ORDER BY e.event_id""", (through,))}
    individual_observations = set()
    by_ref = defaultdict(set)
    first_by_source = {}
    # Use only immutable custody edges and scientific identity fields here. A different
    # project's artifact content, literature and transformation bodies are not read.
    for row in conn.execute("""SELECT json_extract(payload_json,'$.artifact_ref') ref,
            json_extract(payload_json,'$.acquisition.dependencies') dependencies,
            json_extract(payload_json,'$.acquisition.derived_from') parents,
            json_extract(payload_json,'$.acquisition.data_refs') inputs
            FROM events WHERE kind='v6.observation.added' AND event_id<=?
            AND json_type(payload_json,'$.acquisition')='object' ORDER BY event_id""", (through,)):
        ref = row["ref"]
        if ref not in pubs:
            continue
        origin = source_identity({"dependencies": json.loads(row["dependencies"] or "{}")})
        by_ref[ref].add(origin)
        if origin:
            mats.join(ref, first_by_source.setdefault(origin, ref))
        parents = json.loads(row["parents"] or "[]")
        if len(parents) == 1:
            mats.join(ref, parents[0]["artifact_ref"])
        direct = json.loads(row["inputs"] or "[]")
        if len(direct) == 1:
            mats.join(ref, direct[0]["artifact_ref"])
        if len(direct) <= 1:
            individual_observations.add(ref)
    # Equal raw bytes alone cannot merge separately identified physical samples.
    representations = {}
    for ref, pub in pubs.items():
        keys = by_ref.get(ref, set())
        if len(keys) > 1:
            continue
        identity = (pub["sha256"], next(iter(keys), None))
        mats.join(ref, representations.setdefault(identity, ref))
    selected = defaultdict(list)
    records = {}
    for pin in reg["data"]:
        ref = pin["artifact_ref"]
        key = pin.get("material_key", pin["sha256"])
        selected[mats.key(ref)].append(key)
        pub = pubs.get(ref)
        records[key] = {"raw_material_received_event_id": pub["event_id"] if pub else None,
                                  "endpoint_first_available_event_id": None}
    if bindings is not None:
        for ref in pubs:
            if keys := selected.get(mats.key(ref)):
                bindings[ref] = keys
    def found(ref, event_id):
        for digest in selected.get(mats.key(ref), ()):
            record = records[digest]
            prior = record["endpoint_first_available_event_id"]
            if prior is None or event_id < prior:
                record["endpoint_first_available_event_id"] = event_id

    if reg.get("method_pin"):
        sql = """SELECT m.job_id,json_extract(m.admission_json,'$.run_id') run_id,
            e.subject_id ref,max(e.event_id,j.event_id) available,json_extract(o.value,'$.source_inputs') source_inputs
            FROM p0_execution_manifests m, json_each(m.completion_json,'$.outputs') o
            JOIN events e ON e.subject_id=json_extract(o.value,'$.artifact_ref') AND e.kind='artifact.published'
            JOIN events j ON j.subject_id=m.job_id AND j.kind='job.transition' AND j.new_state='SUCCEEDED'
            WHERE json_extract(o.value,'$.output_name')=? AND e.event_id<=? AND j.event_id<=?
            AND json_extract(m.admission_json,'$.capability.sha256')=? ORDER BY available"""
        for row in conn.execute(sql, (reg["method_pin"]["output"], through, through, reg["method_pin"]["manifest_sha256"])):
            inputs = json.loads(row["source_inputs"] or "[]")
            if not any(mats.key(pin["artifact_ref"]) in selected for pin in inputs):
                continue
            for fact in v62_maturity._historical_facts(conn, row["run_id"], row["ref"], through):
                if fact["nature"] != v62_maturity.NATURE[pred["target"]] or fact["record_kind"] == "test_fixture":
                    continue
                reason, reading = v622_measurement.match(conn, reg, fact, chronology_only=True)
                if not reason and reading:
                    for pin in reading["material_inputs"]:
                        found(pin["artifact_ref"], row["available"])
        return records

    if pred["target"] != "experimental_observation":
        return records
    # A registered original can intrinsically disclose a schema-defined value before
    # registration, even when it never received a separate observation notification.
    intrinsic = {p["artifact_ref"] for p in reg["data"]}
    identified = {ref for ref in individual_observations if len(by_ref[ref]) == 1 and None not in by_ref[ref]}
    for ref in intrinsic | identified:
        pub = pubs.get(ref)
        if (pub is None or mats.key(ref) not in selected or pub["kind"] in ("test_fixture", "synthetic_observation")
                or pub["source_role"] in ("fixture", "test_fixture", "synthetic_fixture")
                or not (pub["media_type"] in ("application/json", "text/json") or pub["rel_path"].lower().endswith(".json"))):
            continue
        if external_measurement(conn, ref, pred)[0] is not None:
            found(ref, pub["event_id"])
    return records


def sample_identified(observation):
    acquisition = observation.get("acquisition")
    return bool(acquisition and source_identity(acquisition))


def delivered_external(conn, packet):
    """Project only registered, typed original readings present in this exact delivered packet."""
    run = packet.get("run_id")
    definitions = {}
    for claim in packet.get("claims", ()):
        registration = claim.get("maturity", {}).get("registration") if isinstance(claim, dict) else None
        if not registration or registration.get("policy") != RULE or registration.get("method_pin"):
            continue
        pred = registration["prediction"]
        definitions[registration["measurement_spec_sha256"]] = pred
    if not run or not definitions:
        return []
    delivered = {}
    for item in packet.get("observations", ()):
        ref, digest = item.get("artifact_ref"), item.get("sha256")
        projection = item.get("observation")
        if not isinstance(projection, dict) or not ref or not digest:
            continue
        official = conn.execute("""SELECT 1 FROM events e JOIN artifacts a USING(run_id)
                WHERE e.run_id=? AND e.event_id=? AND e.kind='v6.observation.added' AND a.artifact_ref=?
                AND json_extract(e.payload_json,'$.artifact_ref')=a.artifact_ref AND a.sha256=?
                AND a.origin='INGEST' AND a.published=1""", (run, item.get("event_id"), ref, digest)).fetchone()
        if not official:
            continue
        for spec, pred in definitions.items():
            projected, schema = v62_maturity._scalar_at(projection, pred["observable"])
            if schema != pred["measurement_schema"] or projected is None:
                continue
            value, _ = external_measurement(conn, ref, pred)
            if value != projected:
                continue
            metric = "external_" + ids.sha256_of_obj([ref, digest, spec])
            delivered[metric] = {"metric_ref": metric, "artifact_ref": ref, "sha256": digest, "observable": pred["observable"],
                                 "measurement_schema": schema, "measurement_spec_sha256": spec, "value": value,
                                 **pred.get("external_endpoint", {})}
    return list(delivered.values())
