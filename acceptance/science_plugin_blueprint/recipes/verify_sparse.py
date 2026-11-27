"""Independently recompute the sparse-control scientific readout on its own completed Job."""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import numpy as np
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from Bio.PDB.qcprot import QCPSuperimposer

HERE = Path(__file__).resolve().parent
CONTROLS = HERE.parents[1] / "science_capabilities/structure_controls"
sys.path.insert(0, str(CONTROLS))
from verify import read_pdb


def verify(state, run_id):
    frozen=json.loads((HERE/"sparse-reference/preregistration.json").read_text())
    with closing(sqlite3.connect((state/"gx.db").resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory=sqlite3.Row
        pubs=list(conn.execute("""SELECT a.artifact_ref,a.job_id,a.namespace,a.rel_path,a.sha256
            FROM artifacts a JOIN jobs j USING(job_id) WHERE a.run_id=? AND a.published=1 AND j.status='SUCCEEDED'
            AND a.kind IN ('predicted_mmcif','mapped_structure_agreement') ORDER BY a.rowid""", (run_id,)))
    if len(pubs)!=2:
        raise ValueError("Expected a fresh completed prediction and score from this Run")
    by_name={}
    for item in pubs:
        path=state/"artifacts"/item["namespace"]/item["rel_path"]
        if hashlib.sha256(path.read_bytes()).hexdigest()!=item["sha256"]:
            raise ValueError(f"Changed scientific output {item['artifact_ref']}")
        by_name[path.name]=(item,path)
    model,item=by_name["model.cif"]
    pred=MMCIF2Dict(str(item))
    points={}
    for chain, atom, model_index, residue, x, y, z in zip(*(pred["_atom_site."+field] for field in
        ("label_asym_id","label_atom_id","pdbx_PDB_model_num","label_seq_id","Cartn_x","Cartn_y","Cartn_z")),strict=True):
        if (chain,atom,model_index)==("A","CA","1"):
            points[int(residue)]=[float(x),float(y),float(z)]
    ref_path=CONTROLS/"materials/1QYS.pdb"
    _, atoms, _=read_pdb(ref_path)
    order=frozen["selected_label_seq_id"]
    qcp=QCPSuperimposer()
    qcp.set(np.array([atoms[1][i,"CA"] for i in order]),np.array([points[i-2] for i in order]))
    qcp.run()
    score,file=by_name["agreement.json"]
    measurements=json.loads(file.read_bytes())
    if (measurements["input_sha256"]!=model["sha256"] or measurements["reference_sha256"]!=frozen["excerpt_sha256"]
            or measurements["mapping_sha256"]!=frozen["original_mapping_sha256"]):
        raise ValueError("The comparison used different materials from this control")
    difference=abs(measurements["calpha_rmsd_A"]-qcp.get_rms())
    if (difference>1e-6 or measurements["matched_positions"]!=len(order)
            or measurements["mapped_positions"]!=frozen["expected_mapped"]):
        raise ValueError("The independent coverage or RMSD is inconsistent")
    return {"run_id":run_id, "model_job":model["job_id"], "model_sha256":model["sha256"],
            "score_job":score["job_id"], "score_ref":score["artifact_ref"], "score_sha256":score["sha256"],
            "reference_pdb_sha256":hashlib.sha256(ref_path.read_bytes()).hexdigest(),
            "mapped":measurements["mapped_positions"],"matched":len(order),"job_svd_rmsd_A":measurements["calpha_rmsd_A"],
            "quaternion_rmsd_A":qcp.get_rms(),"difference_A":difference,"absolute_tolerance_A":1e-6}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state",type=Path)
    parser.add_argument("run_id")
    args=parser.parse_args()
    print(json.dumps(verify(args.state.resolve(),args.run_id),indent=2))
