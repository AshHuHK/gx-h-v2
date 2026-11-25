"""Verify operator-declared material identity before atomically registering a source."""
from functools import partial

from adapters.scientific import archive_coordinates

from ..artifacts import ArtifactService
from ..errors import SchemaInvalid
from . import v621_policy

READERS = (archive_coordinates.verify,)


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
    for reader in READERS:
        proof = reader(media_type or "", labels, digest, read, parents, processing, acquisition.get("transformation"))
        if proof is not None:
            return dict(schema="gx.source_identity/1", artifact_sha256=digest, **proof)
    if acquisition.get("transformation") is not None or parents:
        raise SchemaInvalid("An acquired derivative requires a reader for its declared transformation")
    return {"schema": "gx.source_identity/1", "status": "operator_attested", "artifact_sha256": digest}
