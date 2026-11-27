"""Recount contacts from admitted, successful GX jobs' own immutable input bytes. Read-only."""
import argparse
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from decimal import Decimal
from fractions import Fraction
from hashlib import sha256
from io import StringIO
import json
from pathlib import Path
import sqlite3
import statistics
import subprocess

import Bio
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
import numpy as np


def rows(cif, category, fields):
    return zip(*(cif["_" + category + "." + f] for f in fields.split()), strict=True)


def selection(raw):
    cif = MMCIF2Dict(StringIO(raw.decode("utf-8")))
    crossref = defaultdict(set)
    for ref, acc in rows(cif, "struct_ref_seq", "ref_id pdbx_db_accession"):
        crossref[ref].add(acc)
    entities = {ent for ref, ent, typ in rows(cif, "struct_ref", "id entity_id db_name")
                if typ.upper() in ("UNP", "UNIPROT", "UNIPROTKB") and crossref[ref] == {"P12004"}}
    entity, = entities
    assert (entity, "polypeptide(L)") in set(rows(cif, "entity_poly", "entity_id type"))
    chains = sorted(c for c, e in rows(cif, "struct_asym", "id entity_id") if e == entity)
    assert len(chains) == 3 and len(set(chains)) == 3
    declared = {int(pos) for e, pos in rows(cif, "entity_poly_seq", "entity_id num") if e == entity}
    sites, residues = {}, {}
    cols = "group_PDB pdbx_PDB_model_num label_entity_id label_asym_id label_seq_id label_comp_id label_atom_id type_symbol label_alt_id occupancy Cartn_x Cartn_y Cartn_z"
    for group, model, ent, chain, position, comp, name, elem, alt, occ, *coords in rows(cif, "atom_site", cols):
        if ((group, model, ent) != ("ATOM", "1", entity) or chain not in chains
                or elem.upper() in ("H", "D") or Decimal(occ) <= 0):
            continue
        p = int(position)
        assert p in declared and 0 < Decimal(occ) <= 1
        if residues.setdefault((chain,p), comp) != comp:
            raise ValueError("Mixed residue names require separate conformer review")
        if (chain, p, name) in sites or alt not in (".","?"):
            raise ValueError("Alternative or duplicate heavy-atom sites require separate conformer review")
        milli = [Decimal(c) * 1000 for c in coords]
        assert all(x == int(x) and abs(x) < 10**8 for x in milli)
        sites[chain, p, name] = [int(x) for x in milli]
    vectors = {}
    for chain in chains:
        selected = [(pos, xyz) for (c, pos, atom), xyz in sites.items() if c == chain]
        assert selected
        ids, coords = zip(*selected)
        vectors[chain] = np.array(ids, dtype=np.int64), np.array(coords, dtype=np.int64)
    return cif["_entry.id"][0], entity, chains, declared, vectors


def contacts(left, right):
    """Exact integer squared distance on the mmCIF's 0.001 Å coordinate lattice."""
    pos_a, a = left
    pos_b, b = right
    hits = set()
    for start in range(0, len(a), 48):
        delta = a[start:start + 48, None, :] - b[None, :, :]
        close = np.sum(delta * delta, axis=-1, dtype=np.int64) <= 4000**2
        i, j = np.nonzero(close)
        hits.update((int(u), int(v)) for u, v in zip(pos_a[start+i], pos_b[j], strict=True))
    return hits


def artifact(db, state, run_id, ref):
    row = db.execute("SELECT namespace,rel_path,sha256,bytes FROM artifacts WHERE published=1 AND run_id=? AND artifact_ref=?",
                     (run_id, ref)).fetchone()
    assert row, (run_id,ref)
    data = (state/"artifacts"/row["namespace"]/row["rel_path"]).read_bytes()
    assert sha256(data).hexdigest() == row["sha256"] and len(data) == row["bytes"]
    return data, row["sha256"]


def recount(db, state, rid, job):
    spec = json.loads(job["spec_json"])
    assert spec["capability"] == "pcna-interface@1"
    pair, block = spec["parameters"]["pair"], spec["parameters"]["block_index"]
    original, = spec["inputs"]
    raw, digest = artifact(db, state, rid, original)
    entry, entity, chains, declared, atoms = selection(raw)
    outputs = dict(db.execute("SELECT rel_path,artifact_ref FROM artifacts WHERE run_id=? AND job_id=? AND published=1",
                              (rid,job["job_id"])))
    assert outputs["files/interface.json"] == job["result_ref"]
    src, named_hash = artifact(db, state, rid, job["result_ref"])
    matrix, matrix_hash = artifact(db, state, rid, outputs["files/interface-table.json"])
    named, table = json.loads(src), json.loads(matrix)
    assert all(d["input_sha256"] == digest and d["entry"] == entry and d["model"] == "1" and
               d["method"] == spec["capability"] and d["label_chains"] == chains and
               d["label_entity"] == entity and d["contact_cutoff_A"] == 4 and d["pair"] == pair and
               d["window_width"] == 20 for d in (table, named))
    assert named["block_index"] == block
    results = {}
    for i, key in enumerate(("12","23","31")):
        a, b = chains[i], chains[(i + 1) % 3]
        pairs = contacts(atoms[a], atoms[b])
        left, right = set(atoms[a][0]), set(atoms[b][0])
        n = len(pairs)
        source = table["pairs"][key]
        assert source["chains"] == [a,b]
        named_pairs = set(map(tuple,source["contacting_label_seq_id_pairs"]))
        assert named_pairs == pairs and len(source["contacting_label_seq_id_pairs"]) == n
        def check(field, record):
            sequence = declared if field < 0 else set(range(field * 20 + 1, field * 20 + 21))
            cover = [len(left & sequence), len(right & sequence)]
            count = len({(p,q) for p,q in pairs if p in sequence or q in sequence}) if all(cover) else None
            assert record["observed_positions"] == cover and record["n_contact_residue_pairs"] == count
            assert record["label_interval"] == (None if field < 0 else [field*20+1, field*20+20])
            return count
        check(-1, source["whole"])
        assert len(source["windows"]) == (max(declared)+19)//20
        for iwin, win in enumerate(source["windows"]):
            assert win["block_index"] == iwin
            check(iwin, win)
        if key == pair:
            assert (named["chain_left"],named["chain_right"]) == (a,b)
            check(block, named)
        results[key] = {"label_chains":[a,b],"observed_positions":[len(left),len(right)],"count":n,
                        "GX_count":source["whole"]["n_contact_residue_pairs"],"delta":n-source["whole"]["n_contact_residue_pairs"],
                        "pairs_equal":True, "pair_id_sha256":sha256(json.dumps(sorted(pairs),separators=(',',':')).encode()).hexdigest(),
                        "intervals_verified":len(source["windows"])}
    counts = [r["count"] for r in results.values()]
    ratio = Fraction(max(counts)-min(counts), statistics.median(counts)) if statistics.median(counts) else None
    return {"job_id":job["job_id"],"decision_ref":job["decision_ref"],"node_id":job["node_id"],
            "branch_id":job["branch_id"],"status":job["status"],"submitted_at":job["submitted_at"],"finished_at":job["finished_at"],
            "method":spec["capability"],"parameters":spec["parameters"],"entry":entry,"label_entity":entity,
            "chain_order":chains,"declared_positions":len(declared),"heavy_atoms":{c:len(t[0]) for c,t in atoms.items()},
            "input":{"artifact_ref":original,"sha256":digest},
            "named":{"artifact_ref":job["result_ref"],"sha256":named_hash,
                     "pair":pair,"block_index":block,"n_contact_residue_pairs":named["n_contact_residue_pairs"]},
            "table":{"artifact_ref":outputs["files/interface-table.json"],"sha256":matrix_hash},
            "pairs":results,"range_over_median":str(ratio),"ratio_decimal":float(ratio) if ratio is not None else None}


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--state", type=Path, required=True)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args=ap.parse_args()
    url=(args.state/"gx.db").resolve().as_uri()+"?mode=ro"
    with closing(sqlite3.connect(url,uri=True)) as db:
        db.row_factory=sqlite3.Row
        db.execute("BEGIN")
        jobs=db.execute("""SELECT j.job_id,j.status,j.submitted_at,j.finished_at,n.spec_json,n.decision_ref,n.node_id,
            n.branch_id,n.result_ref FROM jobs j JOIN v6_nodes n USING(job_id,run_id)
            WHERE j.run_id=? AND j.status='SUCCEEDED' AND j.capability_id='pcna-interface'
            ORDER BY j.submitted_at""", (args.run_id,)).fetchall()
        if not jobs: raise ValueError("No completed admitted PCNA interface Job in this Run")
        results=[recount(db,args.state,args.run_id,j) for j in jobs]
    payload={"run_id":args.run_id,"build":subprocess.check_output(["git","rev-parse","HEAD"],text=True).strip(),
             "at":datetime.now(timezone.utc).isoformat(),"numpy":np.__version__,"biopython":Bio.__version__,
             "status":"PASS","scope":"Native deposited asymmetric-unit contacts only; whole interfaces, all 20-position windows and visible coverage. SASA and biological/energetic equivalence were not independently calculated.",
             "algorithm":"Original immutable mmCIF bytes parsed with MMCIF2Dict. Independent P12004 entity/model/positive-occupancy heavy atom selection; exhaustive integer atom pair distances at 0.001 Å coordinate precision, inclusive <= 4.000 Å; distinct label_seq_id pairs. Candidates with ambiguous conformers require separate review. No production GX selector, Bio.PDB NeighborSearch, or production PCNA plugin invoked.",
             "tolerance":"0 for contact identities, counts, visible positions and window rows; exact rational for range/median.",
             "jobs":results}
    args.out.write_text(json.dumps(payload,indent=2,ensure_ascii=False)+"\n")
    for j in results: print(j["entry"],j["job_id"],[p["count"] for p in j["pairs"].values()],j["range_over_median"])

if __name__ == "__main__":
    main()
