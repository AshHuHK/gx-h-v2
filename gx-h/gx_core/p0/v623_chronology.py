"""Rebuild typed endpoint availability from store-local immutable acquisition and Job receipts.

Chronology can consider facts outside the recipient's scope. Its public projection consists of
the registered material and ordered events, never another Run's artifact, value or source.
"""
from __future__ import annotations

import json
from collections import defaultdict

from ..artifacts import ArtifactService
from ..errors import GxError, SchemaInvalid
from ..domain import ids
from ..plugins import goal as goal_plugins
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
    """Disjoint material classes for a fixed endpoint and event cursor."""
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


def _legacy_materials(conn, pubs, through):
    mats = _Materials()
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
    identified = {ref for ref in individual_observations if len(by_ref[ref]) == 1 and None not in by_ref[ref]}
    return mats, identified


def _custody_axes(receipt):
    deps = receipt.get("dependencies", {})
    return tuple(tuple(deps.get(k, ())) for k in ("acquisition", "source", "sample"))


def _acquisitions(conn, pubs, through):
    by_ref = defaultdict(dict)
    for row in conn.execute("""SELECT json_extract(payload_json,'$.acquisition') FROM events
            WHERE kind='v6.observation.added' AND event_id<=?
            AND json_type(payload_json,'$.acquisition')='object' ORDER BY event_id""", (through,)):
        receipt = json.loads(row[0])
        ref = receipt["artifact_ref"]
        if ref not in pubs or receipt["sha256"] != pubs[ref]["sha256"]:
            continue
        # Scope and source labels on the submission do not change material identity.
        signature = ids.sha256_of_obj([_custody_axes(receipt), receipt.get("source_identity"),
                                       receipt.get("derived_from"), receipt["data_refs"]])
        by_ref[ref][signature] = receipt
    return by_ref


def _verified_materials(conn, reg, pubs, through):
    """Build equivalence for this endpoint only. A custody edge is not equivalence."""
    from . import v622_sources
    mats = _Materials()
    histories = _acquisitions(conn, pubs, through)
    receipts = {}
    uncertain = {}
    byte_copies = {}
    derived = {}
    verified_boundaries = set()
    for ref, pub in pubs.items():
        variants = histories.get(ref, {})
        if len(variants) > 1:
            uncertain[ref] = "conflicting_acquisition"
            continue
        receipt = next(iter(variants.values()), {})
        if receipt:
            receipts[ref] = receipt
        parents = receipt.get("derived_from", [])
        if parents:
            if len(parents) != 1:
                uncertain[ref] = "multiple_transformation_inputs"
                continue
            parent = parents[0]
            parent_ref = parent["artifact_ref"]
            if pubs.get(parent_ref, {}).get("sha256") != parent["sha256"]:
                uncertain[ref] = "missing_pinned_parent"
                continue
            relation = v622_sources.endpoint_relation(conn, reg.get("method_pin"), receipt.get("source_identity") or {},
                                                      parent_sha256=parent["sha256"], child_sha256=pub["sha256"])
            if relation == "equivalent":
                mats.join(ref, parent_ref)
            elif relation == "different":
                verified_boundaries.add(ref)
            else:
                uncertain[ref] = "unverified_endpoint_transformation"
            derived[ref] = parent_ref
        else:
            if receipt:
                verified_boundaries.add(ref)
            origin = _custody_axes(receipt) if receipt else None
            # Equal encodings from independent source/sample versions stay distinct.
            identity = (pub["sha256"], origin)
            mats.join(ref, byte_copies.setdefault(identity, ref))
    # A successor copying a named, hash-verified original carries its existing endpoint history,
    # including when its original receipt has run-scoped dependency references.
    transfers = conn.execute("""SELECT pin.value FROM events e,
        json_each(e.payload_json,'$.material_transfers') pin
        WHERE e.kind='v6.research.successor_opened' AND e.event_id<=?""", (through,))
    for row in transfers:
        pin = json.loads(row[0])
        parent, child = pin["parent_artifact_ref"], pin["artifact_ref"]
        if pubs.get(parent, {}).get("sha256") == pin["sha256"] == pubs.get(child, {}).get("sha256"):
            mats.join(parent, child)
    # An exact reimport of an identified derivative keeps its history even when its
    # operator labels differ. Iterate only if an alias changed a parent class.
    changed = bool(derived)
    while changed:
        changed = False
        copies = {}
        for ref, parent in derived.items():
            identity = (pubs[ref]["sha256"], mats.key(parent))
            previous = copies.setdefault(identity, ref)
            if mats.key(previous) != mats.key(ref):
                mats.join(ref, previous)
                changed = True
    # A new Goal can reimport bytes before adding a receipt. Inherit an unambiguous
    # prior version. Several independent custody versions leave the origin unresolved.
    identified_copies = defaultdict(set)
    for ref in receipts:
        identified_copies[pubs[ref]["sha256"]].add(mats.key(ref))
    for ref, pub in pubs.items():
        if ref in histories:
            continue
        candidates = identified_copies[pub["sha256"]]
        if len(candidates) == 1:
            mats.join(ref, next(iter(candidates)))
        elif candidates:
            uncertain[ref] = "ambiguous_same_bytes_acquisitions"

    certain = {mats.key(ref) for ref in verified_boundaries}
    reasons = defaultdict(set)
    for ref, reason in uncertain.items():
        key = mats.key(ref)
        if key not in certain:
            reasons[key].add(reason)
    return mats, receipts, reasons


def _external_inputs(ref, acquisition):
    # Custody dependencies of a derivative never attribute its value to a parent.
    if acquisition.get("derived_from"):
        return [ref]
    return [pin["artifact_ref"] for pin in acquisition.get("data_refs", ())] or [ref]


def material_keys(bindings, fact, observation, reading):
    """Attribute a scalar to one material class. A pooled or unbound result cannot qualify."""
    if reading is not None:
        refs = [pin["artifact_ref"] for pin in reading["material_inputs"]]
    else:
        refs = _external_inputs(fact["artifact_ref"], observation.get("acquisition") or {})
    if not refs:
        return []
    first = bindings.get(refs[0], [])
    if not first or any(bindings.get(ref) != first for ref in refs[1:]):
        return []
    return list(first)


def _original_publications(conn, through):
    return {r["subject_id"]: dict(r) for r in conn.execute("""
        SELECT e.event_id,e.subject_id,e.run_id,a.sha256,a.media_type,a.rel_path,a.kind,
               json_extract(e.payload_json,'$.source_role') source_role FROM events e
        JOIN artifacts a ON a.artifact_ref=e.subject_id WHERE e.kind='artifact.published'
        AND e.event_id<=? AND a.published=1 AND (a.origin='INGEST' OR EXISTS (
            SELECT 1 FROM events obs WHERE obs.run_id=a.run_id AND obs.kind='v6.observation.added'
            AND obs.event_id<=? AND json_extract(obs.payload_json,'$.artifact_ref')=a.artifact_ref
            AND json_extract(obs.payload_json,'$.acquisition.archive_job.job_id')=a.job_id))
        ORDER BY e.event_id""", (through, through))}


def materials_overlap(conn, candidates, originals, method_pin=None):
    if originals.intersection(candidates):
        return True
    if not candidates or not originals:
        return False
    through, = conn.execute("SELECT coalesce(max(event_id),0) FROM events").fetchone()
    publications = _original_publications(conn, through)
    materials, _, _ = _verified_materials(conn, {"method_pin": method_pin}, publications, through)
    return not {materials.key(ref) for ref in originals}.isdisjoint(materials.key(ref) for ref in candidates)


def availability(conn, reg, through, *, bindings=None):
    """Earliest provable availability of this typed endpoint, for each requested material."""
    pred = reg["prediction"]
    pubs = _original_publications(conn, through)
    verified = reg.get("material_identity") == "endpoint_verified"
    if verified:
        mats, acquisitions, uncertain = _verified_materials(conn, reg, pubs, through)
    else:
        mats, identified = _legacy_materials(conn, pubs, through)
    selected = defaultdict(list)
    records = {}
    for pin in reg["data"]:
        ref = pin["artifact_ref"]
        key = pin.get("material_key", pin["sha256"])
        selected[mats.key(ref)].append(key)
        pub = pubs.get(ref)
        records[key] = {"raw_material_received_event_id": pub["event_id"] if pub else None,
                                  "endpoint_first_available_event_id": None}
        if verified and mats.key(ref) in uncertain:
            records[key]["material_identity_uncertainty"] = sorted(uncertain[mats.key(ref)])
    if bindings is None:
        bindings = {}
    for ref in pubs:
        if keys := selected.get(mats.key(ref)):
            bindings[ref] = keys
    def found(ref, event_id, *, uncertain_launch=False, started=None):
        for digest in selected.get(mats.key(ref), ()):
            record = records[digest]
            prior = record["endpoint_first_available_event_id"]
            if prior is None or event_id < prior:
                record["endpoint_first_available_event_id"] = event_id
            if uncertain_launch:
                record["sufficient_production_order_uncertain"] = True
                prior_start = record.get("sufficient_job_launch_event_id")
                if prior_start is None or started is not None and started < prior_start:
                    record["sufficient_job_launch_event_id"] = started

    if reg.get("method_pin"):
        if verified and pred["target"] == "analysis":
            from . import v622_sources
            for original, event_id in v622_sources.screening_availability(conn, reg["method_pin"], pubs, bindings):
                found(original, event_id)
        sql = """SELECT m.job_id,json_extract(m.admission_json,'$.run_id') run_id,
            e.subject_id ref,max(e.event_id,j.event_id) available,json_extract(o.value,'$.source_inputs') source_inputs,
            json_extract(m.admission_json,'$.parameters') parameters
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
        # A complete producer table can determine the registered scalar before the
        # scalar reader runs or any report is delivered. Its bytes stay in their scope.
        spec = reg["method_pin"].get("measurement_spec", {})
        from .v6_prospective import match_parameters
        if not spec.get("sufficient_outputs"):
            return records
        owner = conn.execute("SELECT run_id FROM events WHERE event_id=?", (reg["event_id"],)).fetchone()
        provider = goal_plugins.method(conn, owner[0], spec["capability"]) if owner else None
        if not provider or not provider.sufficient_metric:
            return records
        for table in spec.get("sufficient_outputs", ()):
            args = (table["output"], through, through, table["manifest_sha256"])
            for row in conn.execute(sql, args):
                if not match_parameters(spec["parameters"], json.loads(row["parameters"]), table["match_parameters"]):
                    continue
                refs = [p["artifact_ref"] for p in json.loads(row["source_inputs"] or "[]")
                        if p["role"] == table["input_role"] and mats.key(p["artifact_ref"]) in selected]
                if not refs or not any(f["record_kind"] == "job_output" and f.get("job_id") == row["job_id"]
                                       for f in v62_maturity._historical_facts(conn, row["run_id"], row["ref"], through)):
                    continue
                try:
                    document = ArtifactService(conn).read_json(row["ref"])
                    known = provider.sufficient_metric(spec["parameters"], spec["metric_id"], table["capability"],
                                                       table["output"], document)
                except (GxError, ValueError, KeyError, IndexError, TypeError):
                    continue
                if known is True:
                    started, = conn.execute("SELECT min(event_id) FROM events WHERE subject_id=? AND kind='job.transition' "
                                            "AND new_state='SUBMITTING'", (row["job_id"],)).fetchone()
                    crossed = (row["available"] > reg["event_id"] and (started is None or started <= reg["event_id"]))
                    for ref in refs:
                        found(ref, row["available"], uncertain_launch=crossed, started=started)
        return records

    if pred["target"] != "experimental_observation":
        return records
    # A registered original can intrinsically disclose a schema-defined value before
    # registration, even when it never received a separate observation notification.
    intrinsic = {p["artifact_ref"] for p in reg["data"]}
    if verified:
        return _external_availability(conn, pred, pubs, records, acquisitions, intrinsic, bindings)
    for ref in intrinsic | identified:
        pub = pubs.get(ref)
        if (pub is None or mats.key(ref) not in selected or pub["kind"] in ("test_fixture", "synthetic_observation")
                or pub["source_role"] in ("fixture", "test_fixture", "synthetic_fixture")
                or not (pub["media_type"] in ("application/json", "text/json") or pub["rel_path"].lower().endswith(".json"))):
            continue
        if external_measurement(conn, ref, pred)[0] is not None:
            found(ref, pub["event_id"])
    return records


def _external_availability(conn, pred, pubs, records, acquisitions, intrinsic, bindings):
    # A reported original can be an endpoint in its own right or a scalar expressly
    # attributed to a sample. Its attribution never merges those two input artifacts.
    def is_original_json(ref):
        pub = pubs.get(ref)
        if not pub or pub["kind"] in ("test_fixture", "synthetic_observation") or pub["source_role"] in (
                "fixture", "test_fixture", "synthetic_fixture"):
            return False
        media = (pub["media_type"] or "").partition(";")[0].lower().strip()
        return media in ("application/json", "text/json") or media.endswith("+json") or pub["rel_path"].lower().endswith(".json")

    selected_origins = defaultdict(dict)
    for ref in intrinsic:
        receipt = acquisitions.get(ref, {})
        origin = source_identity(receipt)
        if (origin and not receipt.get("derived_from") and not receipt.get("data_refs")
                and ref in bindings and is_original_json(ref)):
            value, _ = external_measurement(conn, ref, pred)
            if value is not None:
                selected_origins[origin].setdefault(value, set()).update(bindings[ref])
    for ref in intrinsic | acquisitions.keys():
        if not is_original_json(ref):
            continue
        pub = pubs[ref]
        receipt = acquisitions.get(ref, {})
        origin = source_identity(receipt)
        keys = set(bindings.get(ref, ()))
        if receipt:
            # Source and acquisition identify a supplied reading. An unsigned or
            # unattributed reading cannot establish the first time for a different input.
            deps = receipt["dependencies"]
            if deps.get("source") and deps.get("acquisition"):
                keys.update(material_keys(bindings, {"artifact_ref": ref}, {"acquisition": receipt}, None))
        compare_origin = not receipt.get("data_refs") and not receipt.get("derived_from") and origin in selected_origins
        if not keys and not compare_origin:
            continue
        value, _ = external_measurement(conn, ref, pred)
        if value is None:
            continue
        if compare_origin:
            keys.update(selected_origins[origin].get(value, ()))
        for key in keys:
            record = records[key]
            previous = record["endpoint_first_available_event_id"]
            if previous is None or pub["event_id"] < previous:
                record["endpoint_first_available_event_id"] = pub["event_id"]
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
