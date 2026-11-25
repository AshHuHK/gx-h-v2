"""Verify operator-declared material identity before atomically registering a source."""
from functools import partial

from adapters.scientific import archive_coordinates, coordinate_equivalence

from ..artifacts import ArtifactService
from ..errors import SchemaInvalid
from . import v621_policy

V624_READERS = (partial(archive_coordinates.verify, verify_revision=True, prove_endpoints=True),)
READERS = (partial(archive_coordinates.verify, verify_revision=True),)
V622_READERS = (archive_coordinates.verify,)


def prepare(conn, run_id, source, acquisition, digest, media_type, *, raw=None, ref=None):
    """Run the applicable source reader outside the ingress write transaction."""
    if not v621_policy.current(conn, run_id):
        return None
    if not isinstance(acquisition, dict):
        raise SchemaInvalid("The acquisition descriptor must be an object")
    if "sha256" in acquisition and acquisition["sha256"] != digest:
        raise SchemaInvalid("Descriptor SHA-256 differs from the supplied artifact")
    labels = [source]
    for key in ("acquisition", "data", "source"):
        values = acquisition.get(key) or []
        values = [values] if isinstance(values, str) else values
        if not isinstance(values, list) or not all(isinstance(x, str) for x in values):
            raise SchemaInvalid("Source identity fields must contain identifiers")
        labels.extend(values)
    processing = acquisition.get("processing") or []
    processing = [processing] if isinstance(processing, str) else processing
    assets = ArtifactService(conn)
    parents = []
    declared = acquisition.get("derived_from", [])
    if not isinstance(declared, list):
        raise SchemaInvalid("derived_from must identify immutable parent data")
    for pin in declared:
        if not isinstance(pin, dict) or set(pin) != {"artifact_ref", "sha256"} or not all(isinstance(v, str) for v in pin.values()):
            raise SchemaInvalid("A source parent requires an artifact reference and SHA-256")
        meta = assets.describe(pin["artifact_ref"])
        if meta["run_id"] != run_id or meta["sha256"] != pin["sha256"] or meta["sha256"] == digest:
            raise SchemaInvalid("A derived parent must identify a distinct acquired input in the same scope")
        parents.append((pin["sha256"], partial(assets.read_bytes, pin["artifact_ref"], expected_sha256=pin["sha256"])))
    # Raw ingestion callers have already hashed the buffer; references are read through the artifact owner.
    read = (lambda: raw) if raw is not None else partial(assets.read_bytes, ref, expected_sha256=digest)
    readers = READERS if v621_policy.live_science(conn, run_id) else V622_READERS
    if v621_policy.science_execution(conn, run_id):
        readers = V624_READERS
    for reader in readers:
        proof = reader(media_type or "", labels, digest, read, parents, processing, acquisition.get("transformation"))
        if proof is not None:
            return dict(schema="gx.source_identity/1", artifact_sha256=digest, **proof)
    if acquisition.get("transformation") is not None or parents:
        raise SchemaInvalid("An acquired derivative requires a reader for its declared transformation")
    return {"schema": "gx.source_identity/1", "status": "operator_attested", "artifact_sha256": digest}


def endpoint_relation(conn, method_pin, source_identity, *, parent_sha256, child_sha256):
    """Read verified acquisition and installed method pins without opening either coordinate file."""
    identity = source_identity or {}
    if (identity.get("status") != "verified_derivation" or identity.get("parent_sha256") != parent_sha256
            or identity.get("artifact_sha256") != child_sha256 or not method_pin
            or (identity.get("transformation") or {}).get("result_sha256") != child_sha256):
        return "unknown"
    spec = method_pin.get("measurement_spec") or {}
    name, _, version = spec.get("capability", "").rpartition("@")
    if (not name or not version or not method_pin.get("manifest_sha256")
            or method_pin.get("capability") != spec["capability"] or method_pin.get("output") != spec.get("output_name")
            or method_pin["manifest_sha256"] != spec.get("manifest_sha256")):
        return "unknown"
    installed = conn.execute("""SELECT json_extract(source_json,'$.installed_package.portable_sha256')
        FROM assets WHERE kind='TOOL_ADAPTER' AND asset_id=? AND version=? AND content_sha256=?""",
                             (name, version, method_pin["manifest_sha256"])).fetchone()
    return coordinate_equivalence.relation(identity, spec, installed[0]) if installed else "unknown"
