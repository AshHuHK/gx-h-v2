"""Replay B0 from the Goal's published artifacts and independently parsed legacy wwPDB."""
import argparse
import hashlib
import json
import sqlite3
from pathlib import Path
import sys

import numpy as np
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from Bio.PDB.qcprot import QCPSuperimposer

HERE = Path(__file__).resolve().parent
CONTROLS = HERE.parents[2] / "acceptance/science_capabilities/structure_controls"
sys.path.insert(0, str(CONTROLS))
from verify import read_pdb


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def independently_recompute(predicted):
    """Use independent parsing of the reference, quaternion fitting and a frozen positional map."""
    original = CONTROLS / "materials/1QYS.pdb"
    seq, atoms, _ = read_pdb(original)
    d = MMCIF2Dict(str(predicted))
    fields = ("pdbx_PDB_model_num label_asym_id label_seq_id label_atom_id "
              "label_comp_id Cartn_x Cartn_y Cartn_z B_iso_or_equiv").split()
    points = {}
    for model, chain, pos, atom, comp, x, y, z, conf in zip(
            *(d["_atom_site." + f] for f in fields), strict=True):
        if (model, chain, atom) != ("1", "A", "CA"):
            continue
        i = int(pos)
        if i in points or comp != {"MSE": "MET"}.get(seq[i + 1], seq[i + 1]):
            raise ValueError("Unexpected position or deposited calibration identity")
        points[i] = ([float(x), float(y), float(z)], float(conf))
    assert sorted(points) == list(range(1, 93))
    fixed = np.array([atoms[1][i + 2, "CA"] for i in sorted(points)])
    moving = np.array([points[i][0] for i in sorted(points)])
    qcp = QCPSuperimposer()
    qcp.set(fixed, moving)
    qcp.run()
    return {"reference_pdb_sha256": sha(original), "matched_pairs": len(fixed),
            "mean_calpha_plddt": float(np.mean([v[1] for v in points.values()])),
            "calpha_rmsd_A": float(qcp.get_rms())}


def verify(state, rid):
    frozen = json.loads((HERE / "preregistration.json").read_bytes())
    uri = (state / "gx.db").resolve().as_uri() + "?mode=ro"
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT artifact_ref,job_id,kind,namespace,rel_path,sha256 "
                            "FROM artifacts WHERE run_id=? AND published=1 AND job_id IS NOT NULL", (rid,))
        results = {}
        for row in rows:
            name = Path(row["rel_path"]).name
            if name not in {"model.cif", "inference.json", "agreement.json", "TERMS_OF_USE.md"}:
                continue
            if name in results:
                raise ValueError(f"Multiple published Jobs provide {name}; inspect original attempts.")
            path = state / "artifacts" / row["namespace"] / row["rel_path"]
            assert sha(path) == row["sha256"]
            results[name] = (dict(row), path)
    model = results["model.cif"][1]
    metadata = json.loads(results["inference.json"][1].read_bytes())
    agreement = json.loads(results["agreement.json"][1].read_bytes())
    original = CONTROLS / "materials/1QYS.cif"
    assert sha(original) == agreement["reference_sha256"]
    assert sha(HERE / "mapping.json") == agreement["mapping_sha256"]
    assert sha(model) == metadata["model_sha256"] == agreement["input_sha256"]
    assert metadata["input_sha256"] == sha(CONTROLS / "native_prediction/input.json")
    assert results["TERMS_OF_USE.md"][1].stat().st_size > 0
    independent = independently_recompute(model)
    rmsd = agreement["calpha_rmsd_A"]
    delta = abs(independent["calpha_rmsd_A"] - rmsd)
    assert delta <= frozen["independent_QCP_vs_job_SVD_tolerance_A"]
    assert agreement["matched_positions"] == independent["matched_pairs"] == frozen["expected_comparable"]
    assert agreement["mapped_positions"] == frozen["expected_mapped"]
    return {"run_id": rid, "artifacts": {k: v[0] for k,v in results.items()},
            "input_sha256": metadata["input_sha256"], "runner_sha256": metadata["runner_sha256"],
            "weights_sha256": metadata["weights_sha256"], "environment": metadata["environment"],
            "matched": agreement["matched_positions"], "mapped": agreement["mapped_positions"],
            "rmsd_A": rmsd, "reference_cif_sha256": agreement["reference_sha256"],
            "mapping_sha256": agreement["mapping_sha256"], "independent_QCP": independent,
            "independent_difference_A": delta, "preregistration_sha256": sha(HERE / "preregistration.json"),
            "within_historical_tolerance": abs(rmsd - frozen["historical_RMSD_A"]) <=
                                           frozen["absolute_reproducibility_tolerance_A"]}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("state", type=Path)
    p.add_argument("run_id")
    a = p.parse_args()
    print(json.dumps(verify(a.state, a.run_id), indent=2))
