"""Search public human PCNA cross-references and acquire an unmodified deposited mmCIF."""
from hashlib import sha256
import http.client
import json
import os
import re
from urllib.parse import urljoin, urlsplit

ACCESSION = "P12004"
REQUEST = "gx.pcna-archive-request/1"
CANDIDATES = "gx.pcna-archive-candidates/1"
UNIPROT = "https://rest.uniprot.org/uniprotkb/P12004.json"
ENTRY_ID = re.compile(r"(?:[0-9][A-Z0-9]{3}|PDB_[A-Z0-9]{8})", re.I | re.ASCII)


def accession(value):
    if not isinstance(value, str) or ENTRY_ID.fullmatch(value) is None:
        raise ValueError("Expected a PDB entry_id")
    return value.lower() if value.upper().startswith("PDB_") else value.upper()


def request_ids(document):
    if (not isinstance(document, dict) or document.get("schema") != REQUEST
            or document.get("accession") != ACCESSION or document.keys() - {"schema", "accession", "entry_ids"}):
        raise ValueError("Expected a human PCNA P12004 archive request")
    if "entry_ids" not in document:
        return None
    if not isinstance(document["entry_ids"], list):
        raise ValueError("entry_ids must be an array of PDB entry IDs")
    return {accession(v) for v in document["entry_ids"]}


def retrieve(url):
    """The only TCP destination is the Job owner's HTTP CONNECT proxy, including redirects."""
    proxy = os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
    if proxy != "http://127.0.0.1:7890":
        raise ValueError("Archive downloads require the configured GX Clash HTTPS proxy")
    host = urlsplit(url).hostname
    for _ in range(4):
        target = urlsplit(url)
        if (target.scheme != "https" or host not in {"rest.uniprot.org", "files.rcsb.org"}
                or target.hostname != host or target.port not in (None, 443) or target.username or target.password):
            raise ValueError("Archive redirect left its original HTTPS provider")
        conn = http.client.HTTPSConnection("127.0.0.1", 7890, timeout=25)
        try:
            conn.set_tunnel(host, 443)
            remote_path = target.path + ("?" + target.query if target.query else "")
            conn.request("GET", remote_path, headers={"User-Agent": "GX-H-PCNA-archive/1", "Accept-Encoding": "identity"})
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308) and response.getheader("Location"):
                url = urljoin(url, response.getheader("Location"))
                continue
            if response.status != 200:
                raise ValueError(f"{host} returned HTTP {response.status}")
            return response.read()
        finally:
            conn.close()
    raise ValueError("Too many redirects from the archive provider")


def cross_references(document, selected=None):
    if document.get("primaryAccession") != ACCESSION or document.get("organism", {}).get("taxonId") != 9606:
        raise ValueError("Expected the UniProt P12004 record for Homo sapiens")
    entries = {}
    for ref in document["uniProtKBCrossReferences"]:
        if ref["database"] != "PDB":
            continue
        entry = accession(ref["id"])
        if selected is not None and entry not in selected:
            continue
        fields = {p["key"]: p["value"] for p in ref.get("properties", [])}
        row = {"entry_id": entry, "method": fields.get("Method"), "resolution": fields.get("Resolution"),
               "chains": fields.get("Chains")}
        if entry in entries and entries[entry] != row:
            raise ValueError(f"UniProt cross-references disagree for {entry}")
        entries[entry] = row
    return [entries[k] for k in sorted(entries)]


def search(raw, download=retrieve):
    document = json.loads(raw)
    selected = None
    source_mode = "provided_uniprot_record"
    if document.get("schema") == REQUEST:
        selected = request_ids(document)
        raw = download(UNIPROT)
        document = json.loads(raw)
        source_mode = "live_uniprot_record"
    candidates = cross_references(document, selected)
    return {"schema": CANDIDATES, "accession": ACCESSION, "source": UNIPROT, "source_mode": source_mode,
            "source_sha256": sha256(raw).hexdigest(), "record_version": document.get("entryAudit", {}).get("entryVersion"),
            "candidates": candidates}


def authorize_entry(archive, entry):
    if archive.get("schema") == CANDIDATES and archive.get("accession") == ACCESSION:
        ids = {accession(r["entry_id"]) for r in archive["candidates"]}
    else:
        ids = request_ids(archive)
    if ids is not None and entry not in ids:
        raise ValueError(f"{entry} is outside this PCNA archive selection")


def rows(doc, *keys):
    return zip(*(doc.get(k, ()) for k in keys), strict=True)


def pcna_entity(doc):
    references = doc.get("_struct_ref.id", ())
    aligned = {}
    if "_struct_ref_seq.pdbx_db_accession" in doc:
        for ref, acc in rows(doc, "_struct_ref_seq.ref_id", "_struct_ref_seq.pdbx_db_accession"):
            if acc not in ("?", "."):
                aligned.setdefault(ref, set()).add(acc)
    mapped = set()
    primary = doc.get("_struct_ref.pdbx_db_accession", ["?"] * len(references))
    for ref, db, entity, acc in zip(references, doc.get("_struct_ref.db_name", ()),
                                    doc.get("_struct_ref.entity_id", ()), primary, strict=True):
        if db.upper() not in ("UNP", "UNIPROT", "UNIPROTKB"):
            continue
        accessions = aligned.get(ref, set()) | ({acc} if acc not in ("?", ".") else set())
        if ACCESSION in accessions:
            if accessions != {ACCESSION}:
                raise ValueError("Conflicting UniProt accession on the same deposited reference")
            mapped.add(entity)
    if len(mapped) != 1:
        raise ValueError("Need exactly one deposited protein entity cross-referenced to UniProt P12004")
    return mapped.pop()


def pcna_chains(doc):
    entity = pcna_entity(doc)
    human = set()
    for category, taxid in (("_entity_src_gen", "pdbx_gene_src_ncbi_taxonomy_id"),
                            ("_entity_src_nat", "pdbx_ncbi_taxonomy_id"),
                            ("_pdbx_entity_src_syn", "ncbi_taxonomy_id")):
        if category + "." + taxid in doc:
            human.update(e for e, tax in rows(doc, category + ".entity_id", category + "." + taxid) if tax == "9606")
    polymers = {e for e, kind in rows(doc, "_entity_poly.entity_id", "_entity_poly.type") if kind.lower() == "polypeptide(l)"}
    if entity not in human or entity not in polymers:
        raise ValueError("The P12004 entity must be a deposited human L-polypeptide")
    declared = [ch for ch, ent in rows(doc, "_struct_asym.id", "_struct_asym.entity_id") if ent == entity]
    if len(declared) != 3 or len(set(declared)) != 3:
        raise ValueError("The P12004 entity must have exactly three distinct asymmetric-unit label chains")
    observed = {ch for group, model, ch, ent in rows(doc, "_atom_site.group_PDB", "_atom_site.pdbx_PDB_model_num",
                "_atom_site.label_asym_id", "_atom_site.label_entity_id")
                if group == "ATOM" and model == "1" and ent == entity and ch in declared}
    if len(observed) != 3:
        raise ValueError(f"Expected three deposited model-1 human P12004 polypeptide chains, observed {len(observed)}")
    assemblies = {}
    for assembly, operation, asym in rows(doc, "_pdbx_struct_assembly_gen.assembly_id",
            "_pdbx_struct_assembly_gen.oper_expression", "_pdbx_struct_assembly_gen.asym_id_list"):
        assemblies.setdefault((assembly, operation), set()).update(ch.strip() for ch in asym.split(","))
    if not any(observed <= together for together in assemblies.values()):
        raise ValueError("The three observed PCNA chains do not share a deposited assembly operation")
    return sorted(observed), [entity]


def structure_context(entry, digest, doc, pcna_entities):
    """Describe deposited model-1 co-occurrence; association and apo state require independent evidence."""
    observed = {}
    for model, ent, chain in rows(doc, "_atom_site.pdbx_PDB_model_num", "_atom_site.label_entity_id",
                                 "_atom_site.label_asym_id"):
        if model == "1":
            observed.setdefault(ent, set()).add(chain)
    labels = dict(rows(doc, "_entity.id", "_entity.pdbx_description")) if "_entity.pdbx_description" in doc else {}
    polymers = dict(rows(doc, "_entity_poly.entity_id", "_entity_poly.type"))
    nonpoly = dict(rows(doc, "_pdbx_entity_nonpoly.entity_id", "_pdbx_entity_nonpoly.comp_id"))
    kinds = dict(rows(doc, "_entity.id", "_entity.type"))

    def entities(ids):
        records = []
        selected = sorted(ids & observed.keys())
        for ident in selected[:24]:
            chains = sorted(observed[ident])
            records.append({"entity_id": ident, "polymer_type": polymers.get(ident), "component_id": nonpoly.get(ident),
                            "description": (labels.get(ident) or "")[:240] or None,
                            "label_chains": chains[:32], "chain_count": len(chains)})
        return {"entity_count": len(selected), "omitted_entities": len(selected) - len(records), "entities": records}

    resolutions = []
    for tag in ("_refine.ls_d_res_high", "_em_3d_reconstruction.resolution", "_reflns.d_resolution_high"):
        for value in dict.fromkeys(doc.get(tag, ())):
            if value not in (".", "?"):
                resolutions.append({"source": tag, "value_A": value})
    return {"schema": "gx.pcna-archive-context/1", "entry_id": entry, "structure_sha256": digest, "model": 1,
            "experimental_methods": [v for v in doc.get("_exptl.method", ()) if v not in (".", "?")],
            "resolution": resolutions, "pcna": entities(set(pcna_entities)),
            "other_deposited_polymers": entities(polymers.keys() - set(pcna_entities)),
            "deposited_non_water_nonpolymers": entities({e for e, kind in kinds.items() if kind.casefold() == "non-polymer"}),
            "ligand_roles": None, "direct_binding": None, "apo_status": None,
            "scope": "Counts describe deposited species with coordinates in model 1. Co-deposition alone does not demonstrate direct binding or establish apo state."}


def fetch(archive, entry, reader, download=retrieve):
    entry = accession(entry)
    authorize_entry(archive, entry)
    url = f"https://files.rcsb.org/download/{entry}.cif"
    raw = download(url)
    observed, doc = reader._entry_and_dictionary(raw)
    if observed != reader._accession(entry):
        raise ValueError("The downloaded mmCIF entry ID differs from the requested accession")
    revision = reader._coordinate_revision(doc)
    if revision is None:
        raise ValueError("The downloaded mmCIF lacks a verified archive revision")
    chains, entities = pcna_chains(doc)
    doi = next((val for k, val in rows(doc, "_citation.id", "_citation.pdbx_database_id_DOI")
                if k.casefold() == "primary" and val not in ("?", ".")), None) if "_citation.pdbx_database_id_DOI" in doc else None
    digest = sha256(raw).hexdigest()
    entity_label = ("entity " if len(entities) == 1 else "entities ") + ", ".join(entities)
    descriptor = {"source": f"wwPDB:{entry}/revision/{revision}", "data": f"{url}#sha256={digest}", "sha256": digest,
                  "acquisition": None, "sample": None, "publication": f"https://doi.org/{doi}" if doi else None,
                  "processing": None, "sampling": None,
                  "applicability": f"Deposited asymmetric unit, model 1, human PCNA {entity_label}, label chains {'/'.join(chains)}."}
    return raw, descriptor, structure_context(entry, digest, doc, entities)
