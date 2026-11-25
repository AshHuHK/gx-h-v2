"""Bind scientific output semantics to admitted declarations and the original publications."""
from __future__ import annotations

import hashlib
import json

from ..artifacts import ArtifactService
from ..capabilities import manifest as capabilities
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied
from ..persistence import store
from . import manifest


AXES = ("data", "acquisition", "sample", "source", "publication", "processing", "sampling")
FIXTURES = {"synthetic_observation", "test_fixture"}
UNKNOWN = {"unknown", "unspecified", "n/a", "not available", "not recorded"}


def freeze_declarations(declarations, inputs):
    """Freeze the exact OutputDecl values, checking dynamic roles against this Job."""
    roles = {item["role"] for item in inputs}
    for item in declarations:
        if not set(item.get("source_roles", ())) <= roles:
            raise SchemaInvalid(f"{item['name']} cites an input role absent from this Job")
    return [dict(item) for item in declarations]


def _binding(admission, output):
    """Identify one declaration, including the children of a declared directory."""
    name = output["output_name"]
    for item in admission["output_declarations"]:
        directory = capabilities.is_dir_output(item)
        if not (name.startswith(item["name"] + "/") if directory else name == item["name"]):
            continue
        kind = item["kind"]
        if directory or capabilities.is_raw_output(item):
            kind = kind.partition(":")[2] or "job_output_file"
        if kind != output["kind"]:
            break
        roles = item.get("source_roles")
        return {"declaration_name": item["name"], "nature": item.get("nature", "unclassified"),
                "source_inputs": [dict(i) for i in admission["inputs"] if roles is None or i["role"] in roles]}
    raise SchemaInvalid("Published output does not match the name and kind of an admitted OutputDecl")


def bind_published(admission, outputs):
    """Called by JobService after publishing the real files/documents, before sealing completion."""
    if admission and "output_declarations" in admission:
        for item in outputs:
            item.update(_binding(admission, item))


def _frozen_nature(admission, output):
    if "output_declarations" not in admission:
        return "unclassified"
    binding = _binding(admission, output)
    if any(output.get(k) != value for k, value in binding.items()):
        raise SchemaInvalid("Completed output binding differs from its admitted declaration and input pins")
    return binding["nature"]


def _publication(conn, run_id, ref):
    row = conn.execute(
        "SELECT e.event_id,e.payload_json,json_extract(e.payload_json,'$.source_role') AS source_role,"
        "a.sha256,a.origin,a.kind,a.task_id,a.job_id,a.bytes,a.media_type,a.rel_path FROM events e "
        "JOIN artifacts a ON a.artifact_ref=e.subject_id AND a.run_id=e.run_id "
        "WHERE e.subject_kind='artifact' AND e.subject_id=? AND e.kind='artifact.published' "
        "AND e.run_id=? AND a.published=1 ORDER BY e.event_id LIMIT 1", (ref, run_id)).fetchone()
    if not row:
        raise ScopeDenied("Cited artifact has no publication in the selected run")
    published = json.loads(row["payload_json"])
    fields = ("sha256", "kind", "origin", "bytes", "task_id", "media_type", "rel_path")
    if published.get("artifact_ref") != ref or any(published.get(k) != row[k] for k in fields):
        raise SchemaInvalid("Artifact record differs from the original publication")
    return row


def recorded_outputs(conn, run_id, job_id, *, check_publications=False):
    """Read the terminal scientific publications and logs from the sealed execution receipt."""
    record = manifest.read_manifest(conn, job_id, missing_ok=True)
    if not record:
        return None
    admission, completed = record["admission"], record["completion"]
    if admission["run_id"] != run_id:
        raise ScopeDenied("Output inventory is outside the selected run")
    if not completed or completed["status"] != "SUCCEEDED":
        return None
    scientific = []
    for output in completed["outputs"]:
        if check_publications:
            pub = _publication(conn, run_id, output["artifact_ref"])
            if (pub["origin"] != "JOB_OUTPUT" or pub["job_id"] != job_id or pub["kind"] != output["kind"]
                    or pub["sha256"] != output["sha256"] or pub["bytes"] != output["bytes"]):
                raise SchemaInvalid("Published artifact differs from the completed Job output")
        entry = dict(output, nature=_frozen_nature(admission, output))
        if output.get("runner_sha256", output["sha256"]) != output["sha256"]:
            entry.update(nature="unclassified", reason="Output lacks a matching detached runner file receipt")
        scientific.append(entry)
    return scientific, completed["logs"]


def published_outputs(conn, run_id, job_id):
    """Short cards for an authorized settled Job. Byte verification occurs on scientific use."""
    receipt = recorded_outputs(conn, run_id, job_id, check_publications=True)
    return [{k: item[k] for k in ("artifact_ref", "sha256", "output_name", "kind", "nature")}
            for item in receipt[0]] if receipt else []


def first_publication(conn, sha256):
    """Earliest availability of any copy of these bytes, even before disclosure to the Lead."""
    return conn.execute("SELECT MIN(event_id) FROM events WHERE kind='artifact.published' "
                        "AND json_extract(payload_json,'$.sha256')=?", (sha256,)).fetchone()[0]


def _verify(assets, ref, digest):
    manifest.verify_artifact_pin(assets, _pin(ref, digest))


def _pin(ref, digest):
    return {"artifact_ref": ref, "sha256": digest}


def _unique_pins(pins):
    return [_pin(ref, sha) for ref, sha in sorted({(p["artifact_ref"], p["sha256"]) for p in pins})]


def _known_derivative_or_fixture(conn, digest, cursor):
    return conn.execute(
        "SELECT 1 FROM events WHERE kind='artifact.published' AND event_id<=? "
        "AND json_extract(payload_json,'$.sha256')=? AND (json_extract(payload_json,'$.origin') "
        "IN ('JOB_OUTPUT','HOST_PUT') OR json_extract(payload_json,'$.kind') IN ('synthetic_observation','test_fixture') "
        "OR json_extract(payload_json,'$.source_role') IN ('fixture','test_fixture','synthetic_fixture',"
        "'model_prediction','prediction','engineering_failure','execution_failure','module_suggestion')) "
        "LIMIT 1", (cursor, digest)).fetchone() is not None


def acquisition_receipt(conn, run_id, artifact_ref, acquisition, *, scope="run"):
    """The trusted observation ingress binds source-owner metadata to exact ingested bytes."""
    if not isinstance(acquisition, dict) or acquisition.keys() - {*AXES, "data_refs", "applicability"}:
        raise SchemaInvalid("Acquisition describes data, acquisition, sample, source, publication, processing, "
                            "sampling, applicability and data_refs")
    pub = _publication(conn, run_id, artifact_ref)
    if pub["origin"] != "INGEST" or pub["job_id"] or pub["task_id"] or pub["kind"] in FIXTURES:
        raise SchemaInvalid("Only original ingested measurements may receive an acquisition receipt")
    if _known_derivative_or_fixture(conn, pub["sha256"], pub["event_id"]):
        raise SchemaInvalid("The same bytes were already published as a Job, host output or fixture; cite that original")
    assets = ArtifactService(conn)
    from .v6_goal import _is_json_artifact
    if _is_json_artifact(pub):
        document = assets.read_json(artifact_ref)
    else:
        _verify(assets, artifact_ref, pub["sha256"])
        document = None
    measurement_schema = document.get("schema") if isinstance(document, dict) else None
    if not isinstance(measurement_schema, str) or not measurement_schema.strip():
        measurement_schema = None
    deps, unknown = {}, []
    for axis in AXES:
        item = acquisition.get(axis)
        if item is None:
            deps[axis] = []
            unknown.append(axis)
        elif item == [] and axis in {"publication", "processing"}:
            deps[axis] = []  # The source owner expressly records none for this dependency.
        else:
            values = [item] if isinstance(item, str) else item
            if (not isinstance(values, list) or not values
                    or any(not isinstance(s, str) or not s.strip() or s != s.strip() for s in values)
                    or len(values) != len(set(values))):
                raise SchemaInvalid(f"Acquisition {axis} requires distinct stable identifiers or null for unknown")
            deps[axis] = sorted(s for s in values if s.casefold() not in UNKNOWN)
            if len(deps[axis]) < len(values):
                unknown.append(axis)
    if not (deps["acquisition"] or deps["source"]):
        raise SchemaInvalid("An acquisition requires an identified acquisition or versioned source")
    applicability = acquisition.get("applicability")
    if applicability is not None and (not isinstance(applicability, str) or not applicability.strip()):
        raise SchemaInvalid("Acquisition applicability must identify the scientific scope")
    refs = acquisition.get("data_refs", [])
    if (not isinstance(refs, list) or not all(isinstance(r, str) and r for r in refs)
            or len(refs) != len(set(refs)) or artifact_ref in refs):
        raise SchemaInvalid("Applicable data_refs must be distinct original input artifact references")
    inputs = []
    for ref in refs:
        source = _publication(conn, run_id, ref)
        if source["origin"] != "INGEST" or source["task_id"] or source["job_id"] or source["kind"] in FIXTURES:
            raise ScopeDenied("Applicable input must be an ingested source in this run")
        if _known_derivative_or_fixture(conn, source["sha256"], source["event_id"]):
            raise SchemaInvalid("An output or fixture alias cannot identify an original experimental sample")
        prior_scopes = {r[0] for r in conn.execute(
            "SELECT DISTINCT json_extract(payload_json,'$.scope') FROM events WHERE run_id=? "
            "AND kind='v6.observation.added' AND json_extract(payload_json,'$.artifact_ref')=?", (run_id, ref))}
        if prior_scopes and not prior_scopes.intersection({"run", scope.removeprefix("branch:")}):
            raise ScopeDenied("Applicable input was supplied outside the observation's scope")
        _verify(assets, ref, source["sha256"])
        inputs.append(_pin(ref, source["sha256"]))
    return {"schema": "gx.acquisition/1", **_pin(artifact_ref, pub["sha256"]), "dependencies": deps,
            "unknown_dependencies": unknown, "applicability": applicability, "measurement_schema": measurement_schema,
            "data_refs": _unique_pins(inputs)}


def _base(conn, ref, pub):
    return {**_pin(ref, pub["sha256"]), "nature": "unclassified", "record_kind": "unclassified",
            "job_id": pub["job_id"], "first_event_id": first_publication(conn, pub["sha256"]),
            "dependencies": {k: [] for k in AXES}, "unknown_dependencies": list(AXES),
            "data_refs": [], "original_observations": [], "upstream_natures": [], "applicability_scopes": [],
            "independence_complete": False, "reason": "No pinned declaration or original acquisition receipt"}


def _observation(conn, run, ref, pub, scope, cursor, result, read):
    if pub["kind"] in FIXTURES or pub["source_role"] in {"fixture", "test_fixture", "synthetic_fixture"}:
        result.update(record_kind="test_fixture", reason="Original publication identifies a test fixture")
        return
    if pub["origin"] != "INGEST" or pub["task_id"]:
        return
    records = conn.execute(
        "SELECT event_id,payload_json FROM events WHERE run_id=? AND kind='v6.observation.added' AND event_id<=? "
        "AND json_extract(payload_json,'$.artifact_ref')=? AND json_extract(payload_json,'$.scope') IN ('run',?) "
        "AND json_type(payload_json,'$.acquisition')='object' ORDER BY event_id",
        (run, cursor, ref, scope.removeprefix("branch:")))
    versions = {}
    for row in records:
        event = json.loads(row["payload_json"])
        receipt = event["acquisition"]
        if (receipt.get("schema") != "gx.acquisition/1" or receipt.get("artifact_ref") != ref
                or event["sha256"] != pub["sha256"] or receipt.get("sha256") != pub["sha256"]):
            raise SchemaInvalid("Acquisition receipt identifies different measurement bytes")
        versions.setdefault(ids.sha256_of_obj(receipt), (row["event_id"], receipt))
    if len(versions) != 1:
        if versions:
            result.update(record_kind="conflicting_acquisition",
                          reason="Conflicting acquisition receipts for the same original publication")
        return
    (event_id, receipt), = versions.values()
    deps = {k: set(v) for k, v in receipt["dependencies"].items()}
    deps["data"].add("sha256:" + pub["sha256"])
    sources = [_pin(ref, pub["sha256"])]
    upstream, scopes = set(), set()
    unknown = set(receipt["unknown_dependencies"])
    complete = not unknown and bool(receipt["applicability"])
    if receipt["applicability"]:
        scopes.add(receipt["applicability"])
    data_refs = list(receipt["data_refs"])
    for pin in receipt["data_refs"]:
        # An acquisition can reference an earlier sampled material, e.g. an
        # aliquot of the same specimen. Keep that lineage as recorded at supply.
        source = read(pin["artifact_ref"], event_id - 1)
        if source["sha256"] != pin["sha256"]:
            raise SchemaInvalid("Applicable data differ from the acquisition receipt")
        deps["data"].add("sha256:" + pin["sha256"])
        if source["record_kind"] == "supplied_observation":
            for axis in AXES:
                deps[axis].update(source["dependencies"][axis])
            sources.extend(source["original_observations"])
            data_refs.extend(source["data_refs"])
            scopes.update(source["applicability_scopes"])
            upstream.update([source["nature"], *source["upstream_natures"]])
            unknown.update(source["unknown_dependencies"])
            complete = complete and source["independence_complete"]
        elif source["record_kind"] in {"test_fixture", "conflicting_acquisition"}:
            complete = False
            unknown.update(AXES)
    result.update(nature="observed", record_kind="supplied_observation",
                  dependencies={k: sorted(v) for k, v in deps.items()},
                  measurement_schema=receipt.get("measurement_schema"),
                  unknown_dependencies=sorted(unknown), data_refs=_unique_pins(data_refs),
                  applicability_scopes=sorted(scopes), upstream_natures=sorted(upstream),
                  original_observations=_unique_pins(sources),
                  independence_complete=complete and len(scopes) == 1,
                  reason="Verified operator acquisition receipt on the original ingested bytes")


def classify(conn, run_id, artifact_ref, *, scope="run", through_event=None):
    """Replay output and original-source provenance as of the supplied monotonic cursor."""
    assets = ArtifactService(conn)
    latest = conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0]
    cache = {}

    def read(ref, cursor):
        key = (ref, cursor)
        if key in cache:
            return cache[key]
        pub = _publication(conn, run_id, ref)
        if pub["event_id"] > cursor:
            raise ScopeDenied("Artifact publication falls after the frozen context")
        _verify(assets, ref, pub["sha256"])
        result = _base(conn, ref, pub)
        if not pub["job_id"]:
            _observation(conn, run_id, ref, pub, scope, cursor, result, read)
        elif record := manifest.read_manifest(conn, pub["job_id"], missing_ok=True):
            _classify_job(conn, run_id, pub, ref, record, result, read)
        cache[key] = result
        return result

    return read(artifact_ref, latest if through_event is None else min(through_event, latest))


def _classify_job(conn, run_id, pub, ref, record, result, read):
    admission, completion = record["admission"], record["completion"]
    if admission["run_id"] != run_id:
        raise ScopeDenied("Scientific output belongs to another Job run")
    if not completion or completion["status"] != "SUCCEEDED":
        if completion and completion["status"] in {"FAILED", "CANCELLED"}:
            result["record_kind"] = "execution_failure"
        result["reason"] = "Job has no successful scientific completion"
        return
    item = next((o for o in completion["outputs"] if o["artifact_ref"] == ref), None)
    if not item:
        result["reason"] = "Artifact is absent from the completed scientific outputs"
        return
    if (pub["origin"] != "JOB_OUTPUT" or item["sha256"] != pub["sha256"]
            or item["kind"] != pub["kind"] or item["bytes"] != pub["bytes"]
            or item.get("runner_sha256", item["sha256"]) != item["sha256"]):
        raise SchemaInvalid("Actual output does not match its completed Job and runner receipts")
    nature = _frozen_nature(admission, item)
    fixture = admission["reproducibility"]["level"] == "deterministic_synthetic_cpu"
    result.update(record_kind="test_fixture" if fixture else "job_output", nature=nature,
                  admission_sha256=record["admission_hash"], completion_sha256=record["manifest_hash"],
                  capability=admission["capability"], output_name=item["output_name"])
    if nature == "unclassified":
        result["reason"] = "This Job has no verified output nature declaration"
        result["data_refs"] = _unique_pins(admission["inputs"])
        result["dependencies"]["data"] = sorted({"sha256:" + p["sha256"] for p in admission["inputs"]})
        return
    # Every parent predates this admission. New source labels cannot change an old Job's lineage.
    original_inputs = item["source_inputs"]
    parents = []
    for entry in original_inputs:
        p = read(entry["artifact_ref"], admission["provenance_cursor"])
        if p["sha256"] != entry["sha256"]:
            raise SchemaInvalid("Scientific source differs from its original Job admission pin")
        parents.append(p)
    deps = {axis: {v for p in parents for v in p["dependencies"][axis]} for axis in AXES}
    deps["data"].update("sha256:" + row["sha256"] for row in original_inputs)
    deps["processing"].add("capability:" + admission["capability"]["sha256"])
    files = admission["source"]["files"]
    if source := (files.get("method_source") or files.get("entry_script")):
        deps["processing"].add("method_source:" + source)
    if files.get("parser"):
        deps["processing"].add("parser:" + files["parser"])
    deps["processing"].update("weights:" + weight["sha256"] for weight in admission["model"]["weights"])
    roles = {v["role"] for v in original_inputs}
    assets = ArtifactService(conn)
    manifest.verify_artifact_pin(assets, admission["input_manifest"])
    for processing_input in admission["inputs"]:
        if processing_input["role"] not in roles:
            manifest.verify_artifact_pin(assets, processing_input)
            deps["processing"].add("input_sha256:" + processing_input["sha256"])
    sources = [s for p in parents for s in p["original_observations"]]
    if nature == "observed":
        sources.append(_pin(ref, pub["sha256"]))
        deps["data"].add("sha256:" + pub["sha256"])
    upstream = {v for p in parents for v in [p["nature"], *p["upstream_natures"]]}
    unknown = {v for p in parents for v in p["unknown_dependencies"]} if parents else set(AXES)
    if admission.get("external_data_paths"):
        unknown.add("source")
    if fixture or any(p["record_kind"] == "test_fixture" for p in parents):
        result["record_kind"] = "test_fixture"
    scopes = {s for p in parents for s in p["applicability_scopes"]}
    result.update(dependencies={k: sorted(v) for k, v in deps.items()}, unknown_dependencies=sorted(unknown),
                  original_observations=_unique_pins(sources), upstream_natures=sorted(upstream),
                  data_refs=_unique_pins([*original_inputs, *(r for p in parents for r in p["data_refs"])]),
                  applicability_scopes=sorted(scopes), declaration_name=item["declaration_name"],
                  independence_complete=bool(sources) and not unknown and len(scopes) == 1
                      and all(p["independence_complete"] for p in parents)
                      and nature != "model_prediction" and "model_prediction" not in upstream
                      and result["record_kind"] != "test_fixture",
                  reason="Verified output bytes, prospective OutputDecl and admitted source inputs")


def ingest_measurement(conn, run_id, *, source, scope, path, metadata_path, actor):
    """Local operator ingress used by gx-p0 observe, including after prospective registration."""
    from ..orchestration import service
    from . import v6_goal

    def supplied(path):
        path = path.expanduser().resolve()
        if service._forbidden(path) or not path.is_file():
            raise SchemaInvalid("Observation input must be a readable authorized local file")
        with path.open("rb") as stream:
            raw = stream.read(service.MAX_COPIED_INPUT_BYTES + 1)
        if len(raw) > service.MAX_COPIED_INPUT_BYTES:
            raise SchemaInvalid("Observation exceeds the Goal immutable input size bound")
        return raw

    if not conn.execute("SELECT 1 FROM v6_goals WHERE run_id=? AND status='OPEN'", (run_id,)).fetchone():
        raise SchemaInvalid("Observations require an open research Goal")
    raw = supplied(path)
    acquisition = json.loads(supplied(metadata_path))
    digest = hashlib.sha256(raw).hexdigest()
    original_key = ids.sha256_of_obj([scope.removeprefix("branch:"), source, digest])
    with store.transaction(conn, transaction_owner="GoalObservation", operation="ingest", run_id=run_id):
        assets = ArtifactService(conn)
        original = assets.publish_bytes(namespace=f"{run_id}/acquisitions", rel_path=f"{original_key}.json",
                                        kind="instrument_measurement", run_id=run_id, data=raw,
                                        expected_sha256=digest, media_type="application/json", origin="INGEST", actor=actor)
        assets.discard_on_rollback(original)
        return v6_goal.record_observation(conn, run_id, original.artifact_ref, source=source,
                                          scope=scope.removeprefix("branch:"), acquisition=acquisition)
