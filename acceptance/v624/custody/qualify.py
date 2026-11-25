"""Qualify archived materials by identity, actual chain count and acquisition. No coordinate arithmetic."""
import argparse
from collections import Counter
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re

from Bio import Align
from Bio.PDB.MMCIF2Dict import MMCIF2Dict

from archive import fetch, sha, write

ROOT = Path(__file__).resolve().parents[3]
HISTORICAL = {"2RE9": ROOT / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif",
              "2O0O": ROOT / "acceptance/v61/adaptive/data/2O0O.cif"}
HERE = Path(__file__).resolve().parent
WINDOWS = ((21, 40), (101, 120))


def digest_text(value):
    return hashlib.sha256(value.encode()).hexdigest()


def window_maps(reference, target):
    """Map polymer SEQRES residue numbers; no cross-deposition chain pairing."""
    aligner = Align.PairwiseAligner()
    aligner.mode = "global"
    aligner.match_score = 2
    aligner.mismatch_score = -1
    aligner.open_gap_score = -5
    aligner.extend_gap_score = -1
    results = aligner.align(reference, target)
    variants = {f"{start}-{end}": set() for start, end in WINDOWS}
    for count, alignment in enumerate(results, 1):
        if count > 1024:
            return {"alignment_status":"AMBIGUITY_NOT_RESOLVED", "windows":{}}
        mapping = {int(a)+1: int(b)+1 for a, b in alignment.indices.T if a >= 0 and b >= 0 and reference[a] == target[b]}
        for start, end in WINDOWS:
            variants[f"{start}-{end}"].add(tuple(mapping.get(i) for i in range(start, end+1)))
    windows = {key: (list(next(iter(values))) if len(values) == 1 else None) for key, values in variants.items()}
    return {"alignment_status": "CHECKED", "windows":windows}


def polymer_copies(cif, entity):
    fields = ["group_PDB", "pdbx_PDB_model_num", "label_entity_id", "label_asym_id", "label_seq_id", "occupancy"]
    columns = [cif["_atom_site."+k] for k in fields]
    return sorted({chain for group, model, target, chain, seq, occ in zip(*columns)
                   if (group, model, target)==("ATOM","1",entity) and seq not in (".","?") and float(occ)>0})


def measurements_metadata(cif):
    return {k: cif.get(k, []) for k in (
        "_diffrn_detector.pdbx_collection_date", "_diffrn_source.pdbx_synchrotron_beamline",
        "_diffrn_source.pdbx_synchrotron_site","_diffrn_detector.type","_symmetry.space_group_name_H-M",
        "_pdbx_database_related.db_id", "_pdbx_database_related.details",
        "_exptl.method","_struct.title","_entity.pdbx_mutation", "_citation.pdbx_database_id_DOI")}


def name_ref(entity):
    description = entity["rcsb_polymer_entity"]["pdbx_description"]
    refs = entity["rcsb_polymer_entity_container_identifiers"].get("reference_sequence_identifiers") or []
    tax = {str(x["ncbi_taxonomy_id"]) for x in entity.get("rcsb_entity_source_organism") or []}
    known = any(a["database_name"] == "UniProt" and a["database_accession"] in ("O95150","Q8NFE9") for a in refs)
    target_name = any(x in description.lower() for x in ("tl1a","tnfsf15","ligand superfamily member 15"))
    return description, list(sorted(tax)), known, target_name


def target_projection(work, entry, ent, refseq, reference_meta):
    url = "https://files.rcsb.org/download/"+entry+".cif"
    if entry in HISTORICAL:
        file = HISTORICAL[entry]
    else:
        try:
            file = fetch(work, entry+".rcsb.cif", url)
        except RuntimeError:
            url = "https://www.ebi.ac.uk/pdbe/entry-files/download/"+entry.lower()+".cif"
            file = fetch(work, entry+".pdbe.cif", url)
    cif = MMCIF2Dict(file)
    if cif["_entry.id"] != [entry]:
        raise ValueError("Archive identity mismatch")
    eid = ent["rcsb_polymer_entity_container_identifiers"]["entity_id"]
    index = cif["_entity_poly.entity_id"].index(eid)
    sequence = re.sub(r"\s","", cif["_entity_poly.pdbx_seq_one_letter_code_can"][index])
    named = polymer_copies(cif,eid)
    exp = measurements_metadata(cif)
    ref_dates = {date for m in reference_meta for date in m["_diffrn_detector.pdbx_collection_date"]}
    ref_beams = {beam for m in reference_meta for beam in m["_diffrn_source.pdbx_synchrotron_beamline"]}
    dates = {d for d in exp["_diffrn_detector.pdbx_collection_date"] if re.fullmatch(r"\d{4}-\d{2}-\d{2}",d)}
    beams = set(exp["_diffrn_source.pdbx_synchrotron_beamline"]) - {".","?"}
    # Describes provenance relative to the TWO references; not mutual independence of other candidates.
    distinct = bool(dates and beams and dates.isdisjoint(ref_dates) and beams.isdisjoint(ref_beams))
    last_major = cif["_pdbx_audit_revision_history.major_revision"][-1]
    last_minor = cif["_pdbx_audit_revision_history.minor_revision"][-1]
    mapped = window_maps(refseq,sequence)
    disposition = ("HISTORICAL_REFERENCE" if entry in HISTORICAL else
                   "FEWER_THAN_THREE_MODELED_TARGET_CHAINS" if len(named)<3 else
                   "REQUIRES_CUSTODIAN_ENTRANT_REVIEW")
    return {"entry":entry,"entity":eid,"deposited_label_chains":named,"actual_modelled_copy_count":len(named),
            "entity_sequence_sha256":digest_text(sequence),"bytes":file.stat().st_size,"file_sha256":sha(file),
            "https_source":url,
            "archive_record":"https://www.rcsb.org/structure/"+entry,
            "revision":f"{last_major}.{last_minor}", "revision_date":cif["_pdbx_audit_revision_history.revision_date"][-1],
            "release_date":cif["_pdbx_audit_revision_history.revision_date"][0],"metadata":exp,
            "different_collection_date_and_beamline_from_both_references":distinct,
            "position_map_2RE9_entity_1":mapped,
            "status":disposition}


def run(work):
    protocol = json.loads((HERE/"screening_protocol.json").read_bytes())
    seal = json.loads((work/"00-freeze.json").read_bytes())
    if seal["protocol_sha256"] != sha(HERE/"screening_protocol.json"):
        raise ValueError("Search criteria changed since first access")
    for name, file in HISTORICAL.items():
        if sha(file) != protocol["references"][name]:
            raise ValueError("Reference material changed")
    found = json.loads((work/"search.json").read_bytes())
    records = json.loads((work/"entries.json").read_bytes())
    if set(found["candidates"]) != set(records):
        raise ValueError("Incomplete candidate metadata")
    refseq = re.sub(r"\s","",MMCIF2Dict(HISTORICAL["2RE9"])["_entity_poly.pdbx_seq_one_letter_code_can"][0])
    reference_meta = [measurements_metadata(MMCIF2Dict(file)) for file in HISTORICAL.values()]
    reviewed = []
    targets = []
    for entry, row in sorted(records.items()):
        released = row["rcsb_accession_info"]["initial_release_date"][:10]
        reason = "OUTSIDE_TAXON_OR_TARGET"
        entities = []
        for ent in row.get("polymer_entities") or []:
            name, tax, known, target_name = name_ref(ent)
            seq = (ent.get("entity_poly") or {}).get("pdbx_seq_one_letter_code_can") or ""
            exact = SequenceMatcher(None,refseq,seq,autojunk=False).find_longest_match().size
            entities.append({"entity_id":ent["rcsb_polymer_entity_container_identifiers"]["entity_id"],
                             "name":name,"taxa":tax,"uniprot_match":known,"target_name_match":target_name,
                             "longest_contiguous_reference_match":exact})
            if released > protocol["search"]["as_of_utc_date"]:
                reason = "AFTER_CAPTURE_DATE"
            elif known or target_name and "9606" in tax:
                result = target_projection(work,entry,ent,refseq,reference_meta)
                targets.append(result)
                reason = result["status"]
            elif exact >= 60:
                # Unannotated but meaningful reference sequence is a manual candidate, never silently excluded.
                reason = "UNANNOTATED_POTENTIAL_TARGET_REQUIRES_REVIEW"
        reviewed.append({"entry":entry,"release_date":released,"title":row["struct"]["title"],
                         "sources":found["candidates"][entry],"entities":entities,"status":reason})
    pending = any("REVIEW" in x["status"] for x in reviewed)
    status = "INCOMPLETE" if pending or not targets or found["sources"]["uniprot-target"].get("status") != "OK" else "NO_ELIGIBLE"
    # No candidate automatically advances to the scientific workspace. If the union changes, fit for the
    # exact mapped mask and original acquisition must be adjudicated before an actual enrollment.
    report = {"schema":"gx.archived-deposition.screening/1", "scope":"Archived human TNFSF15; wwPDB and UniProt snapshot",
              "freeze":seal,"queries":found["sources"], "reference_sequence_sha256":digest_text(refseq),
              "observed_at":found["at"],"counts":dict(Counter(x["status"] for x in reviewed)),
              "candidate_count":len(reviewed), "target_entries":sorted(targets,key=lambda x:(x["release_date"],x["entry"])),
              "candidates":reviewed,"status":status,"new_primary_endpoints_viewed":0,"new_model_inferences":0}
    write(work/"screening.json",report)
    record = {"schema":"gx.archived-deposition.custody/1","status":status,"screening_sha256":sha(work/"screening.json"),
              "protocol_sha256":seal["protocol_sha256"],"blind_id":None,"source_path":None,"source_sha256":None,
              "descriptor_path":None,"descriptor_sha256":None,"mapping":None,"mapping_path":None,"mapping_sha256":None,
              "lead_packet":None,"reason":report["counts"],"candidate_count":len(reviewed),"new_primary_endpoints_viewed":0}
    write(work/"eligibility.json",record)
    print(json.dumps({"status":status,"counts":report["counts"],"candidate_count":len(reviewed)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    run(parser.parse_args().work)
