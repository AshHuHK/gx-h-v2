"""Resolve one human PCNA label entity and three model-1 chains from the deposited asymmetric unit."""
from collections import defaultdict
from dataclasses import dataclass
from hashlib import sha256
from io import StringIO
import math

from Bio.PDB.Atom import Atom
from Bio.PDB.Chain import Chain
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from Bio.PDB.Residue import Residue
import numpy as np


ACCESSION = "P12004"
BACKBONE = {"N":"N", "CA":"C", "C":"C", "O":"O"}


@dataclass
class Selection:
    entry: str
    entity: str
    model: str
    chains: tuple
    molecules: dict
    input_sha256: str
    declared_positions: tuple
    alternate_residues: dict
    excluded_residues: dict
    reference_alignments: tuple


def rows(cif, category, columns):
    return zip(*(cif[category+"."+name] for name in columns.split()), strict=True)


def resolved(sites):
    """Keep atomwise maximum occupancy subject to one coherent residue conformer."""
    choices = {}
    permissible = None
    alternate = False
    for name, records in sites.items():
        altlocs = [r[0] for r in records]
        if len(set(altlocs)) != len(altlocs) or (len(altlocs)>1 and "" in altlocs):
            return None, "duplicate_or_overlapping_site"
        if len({r[1] for r in records}) != 1:
            return None, "conflicting_elements"
        highest = max(r[2] for r in records)
        winners = {r[0]:r for r in records if r[2]==highest}
        choices[name] = winners
        if "" not in winners:
            alternate = True
            permissible = set(winners) if permissible is None else permissible & winners.keys()
    if alternate and not permissible:
        return None, "incompatible_highest_occupancy_conformers"
    # On equal occupancy, the first deposited alt identifier in lexical order breaks the tie for every site.
    chosen = min(permissible) if alternate else ""
    return {name:options.get("", options.get(chosen)) for name,options in choices.items()}, chosen


def select(raw):
    cif = MMCIF2Dict(StringIO(raw.decode("utf-8")))
    entry, = cif["_entry.id"]
    if "_struct_ref.id" not in cif:
        raise ValueError("Missing the deposited entity to UniProt cross-reference")
    aligned_accessions = defaultdict(set)
    if "_struct_ref_seq.pdbx_db_accession" in cif:
        for ref,acc in rows(cif,"_struct_ref_seq","ref_id pdbx_db_accession"):
            if acc not in (".","?"):
                aligned_accessions[ref].add(acc)
    primary = cif.get("_struct_ref.pdbx_db_accession", ["?"]*len(cif["_struct_ref.id"]))
    refs = []
    for (ref,entity,db),acc in zip(rows(cif,"_struct_ref","id entity_id db_name"),primary,strict=True):
        if db.upper() not in ("UNP","UNIPROT","UNIPROTKB"):
            continue
        accessions = aligned_accessions[ref] | ({acc} if acc not in ("?",".") else set())
        if ACCESSION in accessions:
            if accessions != {ACCESSION}:
                raise ValueError("Conflicting UniProt accession on the same deposited reference")
            refs.append((ref,entity))
    entities = {entity for _,entity in refs}
    if len(entities) != 1:
        raise ValueError("Need exactly one deposited protein entity cross-referenced to UniProt P12004")
    entity, = entities
    polymer = [kind for ident,kind in rows(cif,"_entity_poly","entity_id type") if ident==entity]
    if polymer != ["polypeptide(L)"]:
        raise ValueError("P12004 entity must be a deposited L-polypeptide")
    chains = tuple(sorted(c for c,e in rows(cif,"_struct_asym","id entity_id") if e == entity))
    if len(chains)!=3 or len(set(chains))!=3:
        raise ValueError(f"P12004 entity {entity} has {len(chains)} asymmetric-unit label chains; exactly three required")
    declared = {int(seq) for e,seq in rows(cif,"_entity_poly_seq","entity_id num") if e==entity}
    if not declared or min(declared)<1:
        raise ValueError("Missing declared polymer positions")
    ref_ids = {ref for ref,_ in refs}
    alignment_fields = "ref_id pdbx_strand_id seq_align_beg seq_align_end db_align_beg db_align_end"
    have_alignment = all("_struct_ref_seq."+k in cif for k in alignment_fields.split())
    aligns = tuple(dict(zip(alignment_fields.split(),r)) for r in rows(cif,"_struct_ref_seq",alignment_fields)
                   if r[0] in ref_ids) if have_alignment else ()
    fields = ("group_PDB pdbx_PDB_model_num label_entity_id label_asym_id label_seq_id label_comp_id "
              "label_atom_id type_symbol label_alt_id occupancy B_iso_or_equiv Cartn_x Cartn_y Cartn_z")
    groups = {chain:defaultdict(lambda:defaultdict(lambda:defaultdict(list))) for chain in chains}
    for group,model,e,chain,pos,comp,name,el,alt,occ,b,x,y,z in rows(cif,"_atom_site",fields):
        if group!="ATOM" or model!="1" or e!=entity or chain not in groups or el.upper() in ("H","D"):
            continue
        occ = float(occ)
        if not 0<=occ<=1:
            raise ValueError("Occupancy must lie in [0,1]")
        if occ==0:
            continue
        position, element, temp = int(pos), el.upper(), float(b)
        xyz = (float(x),float(y),float(z))
        if (position not in declared or comp in ("?",".") or element in ("?",".")
                or not all(math.isfinite(v) for v in (*xyz,temp))):
            raise ValueError("Invalid deposited residue identity, coordinates or B factor")
        if name in BACKBONE and BACKBONE[name] != element:
            raise ValueError("Unexpected deposited backbone element")
        alt = "" if alt in ("?",".") else alt
        groups[chain][position][comp][name].append((alt,element,occ,temp,xyz))
    molecules, alternates, excluded = {}, {}, {}
    for chain in chains:
        molecule = Chain(chain)
        molecules[chain] = molecule
        alternates[chain], excluded[chain] = {}, {}
        for pos,variants in sorted(groups[chain].items()):
            if len(variants)!=1:
                excluded[chain][pos] = "conflicting_residue_identity"
                continue
            comp,sites = next(iter(variants.items()))
            picked,code = resolved(sites)
            if picked is None:
                excluded[chain][pos] = code
                continue
            if code:
                alternates[chain][pos] = code
            residue = Residue((" ",pos," "),comp," ")
            molecule.add(residue)
            for serial,(name,record) in enumerate(sorted(picked.items()),1):
                alt,element,occ,b,xyz = record
                residue.add(Atom(name,np.array(xyz,dtype=float),b,occ,alt or " ",name,serial,element=element))
        if not molecule.child_list:
            raise ValueError(f"No resolved heavy-atom coordinates in model 1 for selected chain {chain}")
    return Selection(entry,entity,"1",chains,molecules,sha256(raw).hexdigest(),tuple(sorted(declared)),
                     alternates,excluded,aligns)
