"""Bind scientific output semantics to admitted declarations and the original publications."""
from __future__ import annotations

import hashlib
import json

from .. import contracts
from ..artifacts import ArtifactService
from ..capabilities import manifest as capabilities
from ..domain import ids
from ..errors import SchemaInvalid, ScopeDenied
from ..persistence import store
from . import manifest


OUTPUT_NATURES = frozenset(contracts.label_values("output_nature"))
PREDICTION_TARGET_NATURE = {"experimental_observation": "observed", "analysis": "analysis",
                            "model_prediction": "model_prediction"}
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


def recorded_outputs(conn, run_id, job_id, *, check_publications=False, record=None):
    """Read the terminal scientific publications and logs from the sealed execution receipt."""
    if record is None:
        record = manifest.read_manifest(conn, job_id, missing_ok=True)
    if not record:
        return None
    admission, completed = record["admission"], record["completion"]
    if admission["run_id"] != run_id or record["job_id"] != job_id:
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


def acquisition_receipt(conn, run_id, artifact_ref, acquisition, *, scope="run", source_id="", prepared_identity=None, acquired_job=None):
    """The trusted observation ingress binds source-owner metadata to exact ingested bytes."""
    from . import v621_policy, v622_sources
    governed = v621_policy.current(conn, run_id)
    allowed = {*AXES, "data_refs", "applicability", "derived_from"}
    if governed:
        allowed.update(("sha256", "transformation"))
    if not isinstance(acquisition, dict) or acquisition.keys() - allowed:
        raise SchemaInvalid("Acquisition describes data, acquisition, sample, source, publication, processing, "
                            "sampling, applicability and data_refs")
    pub = _publication(conn, run_id, artifact_ref)
    if acquired_job is not None:
        if (pub["origin"] != "JOB_OUTPUT" or pub["job_id"] != acquired_job["job_id"] or
                artifact_ref != acquired_job["artifact_ref"] or pub["sha256"] != acquired_job["sha256"]):
            raise SchemaInvalid("The original must be the verified original output of its acquisition Job")
    elif pub["origin"] != "INGEST" or pub["job_id"] or pub["task_id"] or pub["kind"] in FIXTURES:
        raise SchemaInvalid("Only original ingested measurements may receive an acquisition receipt")
    elif _known_derivative_or_fixture(conn, pub["sha256"], pub["event_id"]):
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
    derived = acquisition.get("derived_from", [])
    if not isinstance(derived, list) or ("derived_from" in acquisition and not derived):
        raise SchemaInvalid("derived_from must identify immutable parent data")
    if derived and not deps["processing"]:
        raise SchemaInvalid("A derived material requires a processing or transformation identifier")
    pinned_parents = []
    for parent in derived:
        if (not isinstance(parent, dict) or set(parent) != {"artifact_ref", "sha256"}
                or not isinstance(parent["artifact_ref"], str) or not isinstance(parent["sha256"], str)):
            raise SchemaInvalid("Each derived parent requires an artifact_ref and SHA-256")
        parent_info = classify(conn, run_id, parent["artifact_ref"], scope=scope)
        if (parent_info["sha256"] != parent["sha256"] or parent["sha256"] == pub["sha256"]
                or parent_info["record_kind"] not in {"supplied_observation", "derived_input"}
                or not parent_info["original_observations"]):
            raise SchemaInvalid("A derived parent must identify a distinct acquired input in the same scope")
        pinned_parents.append(_pin(parent["artifact_ref"], parent["sha256"]))
    if not derived and conn.execute(
        "SELECT 1 FROM events WHERE kind='v6.observation.added' AND json_extract(payload_json,'$.sha256')=? "
        "AND json_type(payload_json,'$.acquisition.derived_from')='array' LIMIT 1", (pub["sha256"],)
    ).fetchone():
        raise SchemaInvalid("Previously registered derived bytes cannot become a new original observation")
    if len(_unique_pins(pinned_parents)) != len(pinned_parents):
        raise SchemaInvalid("Derived source pins must be distinct")
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
    identity = prepared_identity
    if governed:
        if identity is None:
            identity = v622_sources.prepare(conn, run_id, source_id, acquisition, pub["sha256"], pub["media_type"], ref=artifact_ref)
        elif identity["artifact_sha256"] != pub["sha256"]:
            raise SchemaInvalid("Source identity proof refers to another artifact")
    return {"schema": "gx.acquisition/1", **_pin(artifact_ref, pub["sha256"]), "dependencies": deps,
            **({"archive_job": {k: acquired_job[k] for k in ("job_id", "descriptor_ref", "completion_sha256")}}
               if acquired_job is not None else {}),
            **({"source_identity": identity} if identity is not None else {}),
            "unknown_dependencies": unknown, "applicability": applicability, "measurement_schema": measurement_schema,
            "data_refs": _unique_pins(inputs),
            **({"schema": "gx.acquisition/2", "derived_from": _unique_pins(pinned_parents)} if derived else {})}


def _base(conn, ref, pub):
    return {**_pin(ref, pub["sha256"]), "nature": "unclassified", "record_kind": "unclassified",
            "job_id": pub["job_id"], "first_event_id": first_publication(conn, pub["sha256"]),
            "dependencies": {k: [] for k in AXES}, "unknown_dependencies": list(AXES),
            "data_refs": [], "original_observations": [], "upstream_natures": [], "applicability_scopes": [],
            "independence_complete": False, "reason": "No pinned declaration or original acquisition receipt"}


def _observation(conn, run, ref, pub, scope, cursor, result, read, *, archive_job=None):
    if pub["kind"] in FIXTURES or pub["source_role"] in {"fixture", "test_fixture", "synthetic_fixture"}:
        result.update(record_kind="test_fixture", reason="Original publication identifies a test fixture")
        return
    if archive_job is None and (pub["origin"] != "INGEST" or pub["task_id"]):
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
        if (receipt.get("schema") not in {"gx.acquisition/1", "gx.acquisition/2"} or receipt.get("artifact_ref") != ref
                or event["sha256"] != pub["sha256"] or receipt.get("sha256") != pub["sha256"]):
            raise SchemaInvalid("Acquisition receipt identifies different measurement bytes")
        if archive_job is not None and (receipt.get("archive_job") or {}).get("completion_sha256") != archive_job:
            continue
        versions.setdefault(ids.sha256_of_obj(receipt), (row["event_id"], receipt))
    if len(versions) != 1:
        if versions:
            result.update(record_kind="conflicting_acquisition",
                          reason="Conflicting acquisition receipts for the same original publication")
        return
    (event_id, receipt), = versions.values()
    identity = receipt.get("source_identity") or {}
    if "revision" in identity or "external_source" in identity:
        # Preserve the distinction between matching bytes, the accession and the archive revision.
        result["source_identity"] = identity
    deps = {k: set(v) for k, v in receipt["dependencies"].items()}
    deps["data"].add("sha256:" + pub["sha256"])
    derived = receipt.get("derived_from", [])
    sources = [] if derived else [_pin(ref, pub["sha256"])]
    upstream, scopes = set(), set()
    unknown = set(receipt["unknown_dependencies"])
    complete = not unknown and bool(receipt["applicability"])
    if receipt["applicability"]:
        scopes.add(receipt["applicability"])
    parents = _unique_pins([*receipt["data_refs"], *derived])
    derived_refs = {pin["artifact_ref"] for pin in derived}
    originals_grounded = True
    data_refs = list(parents)
    for pin in parents:
        # An acquisition can reference an earlier sampled material, e.g. an
        # aliquot of the same specimen. Keep that lineage as recorded at supply.
        source = read(pin["artifact_ref"], event_id - 1)
        if pin["artifact_ref"] in derived_refs:
            originals_grounded = originals_grounded and source.get("original_inputs_grounded", False)
        if source["sha256"] != pin["sha256"]:
            raise SchemaInvalid("Applicable data differ from the acquisition receipt")
        deps["data"].add("sha256:" + pin["sha256"])
        if source["record_kind"] in {"supplied_observation", "derived_input"}:
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
    if "original_inputs_grounded" in result:
        result["original_inputs_grounded"] = originals_grounded
    result.update(nature="analysis" if derived else "observed",
                  record_kind="derived_input" if derived else "supplied_observation",
                  dependencies={k: sorted(v) for k, v in deps.items()},
                  measurement_schema=receipt.get("measurement_schema"),
                  unknown_dependencies=sorted(unknown), data_refs=_unique_pins(data_refs),
                  applicability_scopes=sorted(scopes), upstream_natures=sorted(upstream),
                  original_observations=_unique_pins(sources),
                  independence_complete=complete and len(scopes) == 1,
                  reason=("Versioned transformation of acquired input bytes" if derived else
                          "Verified archive Job and acquisition receipt on the original bytes" if archive_job else
                          "Verified operator acquisition receipt on the original ingested bytes"))


def classify(conn, run_id, artifact_ref, *, scope="run", through_event=None):
    """Replay output and original-source provenance as of the supplied monotonic cursor."""
    assets = ArtifactService(conn)
    latest = conn.execute("SELECT COALESCE(MAX(event_id),0) FROM events").fetchone()[0]
    cache = {}

    from . import v621_policy
    frozen_scope = v621_policy.enabled(conn, run_id)

    def read(ref, cursor, source_scope=scope):
        key = (ref, cursor, source_scope)
        if key in cache:
            return cache[key]
        pub = _publication(conn, run_id, ref)
        if pub["event_id"] > cursor:
            raise ScopeDenied("Artifact publication falls after the frozen context")
        _verify(assets, ref, pub["sha256"])
        result = _base(conn, ref, pub)
        if frozen_scope:
            result["original_inputs_grounded"] = False
        if not pub["job_id"]:
            _observation(conn, run_id, ref, pub, source_scope, cursor, result,
                         lambda parent, at: read(parent, at, source_scope))
        elif record := manifest.read_manifest(conn, pub["job_id"], missing_ok=True):
            # Consumption of a report in another scope cannot add a sibling's acquisition
            # to the Job. Rebuild its input provenance using its actual admission owner.
            producer_scope = source_scope
            if frozen_scope:
                producer_scope = record["admission"]["owner"].get("branch_id") or "run"
            def read_input(parent, at):
                return read(parent, at, producer_scope)
            _classify_job(conn, run_id, pub, ref, record, result, read_input)
            from . import acquired_material
            declaration = acquired_material.declared(record)
            if declaration and result.get("output_name") == declaration["name"]:
                _observation(conn, run_id, ref, pub, source_scope, cursor, result, read_input,
                             archive_job=record["manifest_hash"])
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
    if "original_inputs_grounded" in result:
        result["original_inputs_grounded"] = (
            bool(parents) and all(p["original_inputs_grounded"] for p in parents)
            and result["record_kind"] != "test_fixture"
            and nature in {"analysis", "observed"} and "model_prediction" not in upstream
        )
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


def ingest_measurement(conn, run_id, *, source, scope, path, metadata_path, actor, artifact_ref=None,
                       expected_sha256=None, media_type="application/json"):
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
    acquisition = json.loads(supplied(metadata_path))
    if source is None:
        source = acquisition.get("source") if isinstance(acquisition, dict) else None
        if not isinstance(source, str) or not source.strip():
            raise SchemaInvalid("The acquisition descriptor requires a source identifier")
    scope = scope.removeprefix("branch:")
    if artifact_ref:
        if path or not expected_sha256:
            raise SchemaInvalid("Register existing bytes using their artifact reference and expected SHA-256")
        if ArtifactService(conn).describe(artifact_ref)["sha256"] != expected_sha256:
            raise SchemaInvalid("Expected SHA-256 differs from the supplied artifact pin")
        return v6_goal.record_observation(conn, run_id, artifact_ref, source=source, scope=scope, acquisition=acquisition)
    media_suffix = {"application/json": "json", "chemical/x-mmcif": "cif", "text/plain": "txt"}
    if media_type not in media_suffix:
        raise SchemaInvalid("Unsupported observation media type")
    raw = supplied(path)
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and expected_sha256 != digest:
        raise SchemaInvalid("Supplied bytes differ from the expected SHA-256")
    from . import v622_sources
    identity = v622_sources.prepare(conn, run_id, source, acquisition, digest, media_type, raw=raw)
    original_key = ids.sha256_of_obj([scope.removeprefix("branch:"), source, digest])
    with store.transaction(conn, transaction_owner="GoalObservation", operation="ingest", run_id=run_id):
        assets = ArtifactService(conn)
        original = assets.publish_bytes(namespace=f"{run_id}/acquisitions", rel_path=f"{original_key}.{media_suffix[media_type]}",
                                        kind="instrument_measurement", run_id=run_id, data=raw,
                                        expected_sha256=digest, media_type=media_type, origin="INGEST", actor=actor)
        assets.discard_on_rollback(original)
        return v6_goal.record_observation(conn, run_id, original.artifact_ref, source=source,
                                          scope=scope.removeprefix("branch:"), acquisition=acquisition, prepared_identity=identity)
