"""Verify PDBx/mmCIF archive identity and declared coordinate-record transformations."""
from hashlib import sha256
from io import StringIO
import json
import re
from urllib.parse import parse_qs, urlsplit

from gx_core.errors import SchemaInvalid

from . import coordinate_equivalence

READER = "gx.pdbx.source-identity/1"
PDB_ID = r"(?:[0-9][A-Za-z0-9]{3}|pdb_[A-Za-z0-9]{8})"
CIF_MEDIA = {"chemical/x-mmcif", "chemical/x-cif"}
WITHHOLD = "gx.public-archive-coordinate-withholding/1"


def _accession(name):
    name = name.upper()
    return name[8:] if name.startswith("PDB_0000") else name


def _declarations(labels, *, coordinate_file):
    accessions, hashes = set(), set()
    for label in labels:
        match = re.fullmatch(r"(?:wwPDB:|PDB:)(" + PDB_ID + r")(?=[:/]|$).*", label, re.I)
        if match:
            accessions.add(_accession(match[1]))
        elif coordinate_file and re.fullmatch(PDB_ID, label, re.I) and any(c.isalpha() for c in label):
            accessions.add(_accession(label))
        else:
            part = urlsplit(label) if label.startswith(("http://", "https://")) else None
            if not part or part.hostname not in {"files.rcsb.org", "www.rcsb.org", "rcsb.org", "www.ebi.ac.uk",
                                                 "ftp.wwpdb.org", "files.wwpdb.org"}:
                continue
            # Accept the accession in an actual coordinate filename or a record landing page.
            match = re.search(r"/(" + PDB_ID + r")(?:\.(?:cif|mmcif)(?:\.gz)?)?/?$", part.path, re.I)
            if match:
                accessions.add(_accession(match[1]))
                hashes.update(parse_qs(part.fragment).get("sha256", []))
    return accessions, hashes


def _entry_and_dictionary(raw):
    from Bio.PDB.MMCIF2Dict import MMCIF2Dict

    try:
        doc = MMCIF2Dict(StringIO(raw.decode("utf-8")))
    except (ValueError, IndexError, UnicodeError) as exc:
        raise SchemaInvalid("Cannot read the declared coordinate archive entry ID") from exc
    if any(not tag.startswith("_") and tag != "data_" for tag in doc):
        raise SchemaInvalid("Coordinate identity requires one unambiguous archive data block")
    ids = [v for tag, values in doc.items() if tag.casefold() == "_entry.id" for v in values]
    if len(ids) != 1 or not re.fullmatch(PDB_ID, ids[0], re.I):
        raise SchemaInvalid("The coordinate source needs one readable PDB _entry.id")
    entry = _accession(ids[0])
    block = doc.get("data_", "")
    if not isinstance(block, str) or (re.fullmatch(PDB_ID, block, re.I) and _accession(block) != entry):
        raise SchemaInvalid("Coordinate data block and internal archive accession disagree")
    return entry, doc


def _coordinate_revision(doc):
    """Latest Structure model ordinal in this file, independent of loop row order."""
    prefix = "_pdbx_audit_revision_history."
    columns = {key.casefold()[len(prefix):]: values for key, values in doc.items()
               if key.casefold().startswith(prefix)}
    required = ("data_content_type", "ordinal", "major_revision", "minor_revision")
    if any(key not in columns for key in required) or len({len(columns[key]) for key in required}) != 1:
        return None
    rows = [(ordinal, major, minor) for kind, ordinal, major, minor in zip(*(columns[key] for key in required))
            if kind.casefold() == "structure model"]
    if not rows or any(not re.fullmatch(r"[1-9][0-9]*", ordinal) for ordinal, _, _ in rows):
        return None
    latest = max(int(ordinal) for ordinal, _, _ in rows)
    versions = {(major, minor) for ordinal, major, minor in rows if int(ordinal) == latest}
    if len(versions) != 1:
        return None
    major, minor = versions.pop()
    if not re.fullmatch(r"[0-9]+", major) or not re.fullmatch(r"[0-9]+", minor):
        return None
    return f"{int(major)}.{int(minor)}"


def _revision_identity(doc, labels):
    # These are the archive source IDs used by the released acquisition descriptors.
    syntax = re.compile(r"(?:wwPDB|PDB):" + PDB_ID + r"(?:/revision/|:revision-)([^/?#:]+)(?=[/?#:]|$)", re.I)
    claims = {m[1] for label in labels if (m := syntax.match(label))}
    if len(claims) > 1:
        raise SchemaInvalid("Conflicting archive revisions in the acquisition descriptor",
                            details={"claimed": sorted(claims)})
    claimed = next(iter(claims), None)
    observed = _coordinate_revision(doc)
    status = "observed" if observed is not None else "unknown"
    if claimed is not None:
        status = "operator_attested"
        if observed is not None and re.fullmatch(r"[0-9]+\.[0-9]+", claimed):
            canonical = ".".join(str(int(n)) for n in claimed.split("."))
            if canonical != observed:
                raise SchemaInvalid("Declared archive revision differs from the current coordinate file",
                                    details={"claimed": claimed, "observed": observed})
            status = "verified"
    return {"status": status, "claimed": claimed, "observed": observed}


def _withholding(parent, child, rule, parent_sha, processing, *, proof_bytes):
    # The material publisher hashes the versioned JSON file, including its trailing newline.
    rule_sha = sha256((json.dumps(rule, indent=2, sort_keys=True, ensure_ascii=False)+"\n").encode()).hexdigest()
    fields = {"schema", "parent_sha256", "operation", "group", "model", "entity", "chain", "atom",
              "label_seq_id_modulus", "residues_to_withhold", "removed_ids_sha256"}
    if (set(rule) != fields or rule["parent_sha256"] != parent_sha
            or rule["operation"] != "withhold complete archived atom_site rows"
            or processing != ["coordinate-record withholding/" + rule_sha]):
        raise SchemaInvalid("Coordinate transformation and its pinned declaration differ")
    modulus, residues = rule["label_seq_id_modulus"], rule["residues_to_withhold"]
    if (type(modulus) is not int or modulus < 1 or not isinstance(residues, list) or not residues
            or any(type(n) is not int or not 0 <= n < modulus for n in residues)
            or len(set(residues)) != len(residues)):
        raise SchemaInvalid("Invalid coordinate record selection")
    selectors = {"group_PDB": rule["group"], "pdbx_PDB_model_num": rule["model"], "label_entity_id": rule["entity"],
                 "label_asym_id": rule["chain"], "label_atom_id": rule["atom"]}
    if any(not isinstance(v, str) or not v for v in selectors.values()):
        raise SchemaInvalid("Invalid coordinate transformation selector")
    lines = parent.splitlines(keepends=True)
    columns = [line.split()[0].decode("ascii").partition(".")[2] for line in lines if line.startswith(b"_atom_site.")]
    if (len(set(columns)) != len(columns) or not {"id", "label_seq_id", *selectors}.issubset(columns)):
        raise SchemaInvalid("Coordinate transformation requires one tabular atom_site loop")
    removed, produced = [], sha256()
    affected_positions = set()
    for line in lines:
        if line.startswith((b"ATOM ", b"HETATM ")):
            words = line.decode("utf-8").split()
            if len(words) != len(columns):
                raise SchemaInvalid("Coordinate records do not fit the declared transformation")
            row = dict(zip(columns, words))
            if all(row[k] == v for k, v in selectors.items()):
                try:
                    residue = int(row["label_seq_id"])
                except ValueError as exc:
                    raise SchemaInvalid("Coordinate transformation requires integer sequence IDs") from exc
                if residue % modulus in residues:
                    removed.append(row["id"])
                    if proof_bytes is not None:
                        affected_positions.add(residue)
                    continue
        produced.update(line)
    if (not removed or sha256("\n".join(removed).encode()).hexdigest() != rule["removed_ids_sha256"]
            or produced.hexdigest() != child):
        raise SchemaInvalid("Replaying the coordinate transformation does not reproduce the child SHA-256")
    proof = {"schema": WITHHOLD, "descriptor_sha256": rule_sha, "result_sha256": child}
    if proof_bytes is not None:
        proof["coverage_masks"] = coordinate_equivalence.withholding_effects(parent, proof_bytes, affected_positions)
    return proof


def verify(media_type, labels, digest, read, parents, processing, transformation, *, verify_revision=False, prove_endpoints=False):
    """Return None for another domain. Original, parent and transformed child all use immutable file pins."""
    coordinate_file = media_type.partition(";")[0].lower() in CIF_MEDIA
    accessions, archive_digests = _declarations(labels, coordinate_file=coordinate_file)
    if not coordinate_file and not accessions:
        return None
    raw = read()
    try:
        entry, doc = _entry_and_dictionary(raw)
    except SchemaInvalid:
        if accessions:
            raise
        # A local CIF may describe a non-PDB object. Its submitter can still attest to its origin.
        return None
    if accessions and accessions != {entry}:
        raise SchemaInvalid("Declared archive accession differs from the coordinate _entry.id",
                            details={"declared": sorted(accessions), "entry_id": entry})
    if not archive_digests.issubset({parents[0][0] if parents else digest}):
        raise SchemaInvalid("Archive descriptor SHA-256 differs from the original coordinate bytes; identify the parent and transformation for derived bytes")
    result = {"reader": READER, "entry_id": entry,
              "status": "matched_accession" if accessions else "operator_attested"}
    if verify_revision:
        result.update(reader="gx.pdbx.source-identity/2", revision=_revision_identity(doc, labels))
    if prove_endpoints:
        result["reader"] = "gx.pdbx.source-identity/3"
    if not parents:
        if transformation is not None:
            raise SchemaInvalid("A coordinate transformation needs its original parent")
        return result
    if len(parents) != 1:
        raise SchemaInvalid("The registered coordinate transformations require one acquired parent")
    parent_sha, get_parent = parents[0]
    parent = get_parent()
    if _entry_and_dictionary(parent)[0] != entry:
        raise SchemaInvalid("Coordinate derivative and declared parent have different archive accessions")
    if isinstance(transformation, dict) and transformation.get("schema") == WITHHOLD:
        result["transformation"] = _withholding(parent, digest, transformation, parent_sha, processing,
                                                  proof_bytes=raw if prove_endpoints else None)
    elif transformation is None and processing == ["append-envelope-comment/v1"]:
        suffix = raw[len(parent):]
        if not (raw.startswith(parent) and suffix and all(s.lstrip().startswith(b"#") for s in suffix.splitlines())):
            raise SchemaInvalid("Coordinate comment transformation does not reproduce the supplied bytes")
        result["transformation"] = {"schema": processing[0], "result_sha256": digest}
        if prove_endpoints:
            if not parent.endswith((b"\n", b"\r")):
                raise SchemaInvalid("An appended comment must start on a separate coordinate file line")
            result["transformation"]["coverage_masks"] = dict.fromkeys(coordinate_equivalence.MASKS, "equivalent")
    elif prove_endpoints and transformation is None and processing == [coordinate_equivalence.ANNOTATION]:
        try:
            unchanged = coordinate_equivalence.same_coordinate_table(parent, raw)
        except (RuntimeError, IndexError, ValueError, KeyError, UnicodeError):
            unchanged = False
        if not unchanged:
            raise SchemaInvalid("Coordinate annotation must preserve the registered atom_site records")
        result["transformation"] = {"schema": processing[0], "result_sha256": digest,
                                    "coverage_masks": dict.fromkeys(coordinate_equivalence.MASKS, "equivalent")}
    elif (prove_endpoints and len(processing) == 1 and isinstance(processing[0], str)
          and processing[0] not in ("append-envelope-comment/v1", coordinate_equivalence.ANNOTATION)
          and not processing[0].startswith("coordinate-record withholding/")):
        return dict(result, status="unverified_derivation", parent_sha256=parent_sha,
                    reason="The archived transformation has no verified endpoint equivalence rule")
    else:
        raise SchemaInvalid("Supply the versioned descriptor for a supported coordinate transformation")
    result.update(status="verified_derivation", parent_sha256=parent_sha)
    return result
