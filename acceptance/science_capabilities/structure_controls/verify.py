"""Replay the shipped QC workers and independently recompute against wwPDB legacy PDB text."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

import numpy as np
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from Bio.PDB.qcprot import QCPSuperimposer

ROOT = Path(__file__).resolve().parent
MATERIALS = ROOT / "materials"
WORKER = ROOT.parents[2] / "gx-h/plugins/installed/org.gx.deposited-structure-qc/1.0.0/job-scripts/qc.py"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_pdb(path):
    """Independent fixed-column reader; retain polymer HETATM (e.g. MSE and DAR)."""
    models = defaultdict(dict)
    declared = []
    links = []
    model = 1
    for line in path.read_text().splitlines():
        record = line[:6].strip()
        if record == "MODEL":
            model = int(line[10:14])
        elif record == "SEQRES" and line[11] == "A":
            declared.extend(line[19:70].split())
        elif record == "LINK":
            ends = ((line[21], int(line[22:26]), line[12:16].strip()),
                    (line[51], int(line[52:56]), line[42:46].strip()))
            links.append(set(ends))
        elif record in {"ATOM", "HETATM"} and line[21] == "A":
            pos = int(line[22:26])
            if 1 <= pos <= len(declared) and line[17:20].strip() == declared[pos-1] and line[76:78].strip() not in {"H","D"}:
                models[model][pos, line[12:16].strip()] = np.array([float(line[j:j+8]) for j in (30,38,46)])
    return declared, models, links


def job(mode, name, out):
    subprocess.run([sys.executable, "-IB", str(WORKER), "--mode", mode, "--structure",
                    str(MATERIALS / (name+".cif")), "--out", str(out)], check=True)
    return json.loads((out / "qc.json").read_bytes())


def reference_controls(tmp):
    declared, models, _ = read_pdb(MATERIALS / "1QYS.pdb")
    backbone = job("backbone", "1QYS", tmp/"backbone")
    ca = np.stack([point for (_, atom), point in models[1].items() if atom == "CA"])
    # Pairwise squared distances and center-of-mass formulations have the same second moment.
    radius = np.sqrt(np.sum((ca[:, None] - ca[None, :])**2) / (2*len(ca)**2))
    assert len(declared) == backbone["declared_residues"] == 106
    assert len(ca) == backbone["observed_residues"] == backbone["complete_backbones"] == 92
    assert backbone["missing_label_positions"] == [1,2,*range(95,107)]
    assert abs(radius - backbone["ca_radius_of_gyration_A"]) < 1e-9

    residues, models, links = read_pdb(MATERIALS / "6BEN.pdb")
    assert {("A", 8, "C"), ("A", 1, "N")} in links
    native = job("macrocycle", "6BEN", tmp/"native")
    minus = job("macrocycle", "6BEN-minus-closure", tmp/"minus")
    assert native["declared_closure"] == 1 and native["closure_link_ids"] == ["covale2"]
    assert minus["declared_closure"] == 0 and not minus["closure_link_ids"]
    assert len(residues) == native["declared_residues"] == 8
    assert len(models) == native["mapped_models"] == native["model_count"] == 20
    assert native["models"] == minus["models"]
    raw = (MATERIALS/"6BEN.cif").read_bytes().splitlines(keepends=True)
    removed = [row for row in raw if row.startswith(b"covale2 ")]
    assert len(removed) == 1 and b"".join(row for row in raw if row != removed[0]) == (MATERIALS/"6BEN-minus-closure.cif").read_bytes()
    differences = {"distance_A": [], "ca_c_n_deg": [], "c_n_ca_deg": []}
    for row in native["models"]:
        atoms = models[row["model"]]
        pts = [atoms[p,a] for p,a in ((8,"CA"),(8,"C"),(1,"N"),(1,"CA"))]
        norms = np.linalg.norm
        def corner(x,y,z):
            u,v = x-y,z-y
            return np.degrees(np.arctan2(norms(np.cross(u,v)),np.dot(u,v)))
        for key, value in (("distance_A", norms(pts[2]-pts[1])),
                           ("ca_c_n_deg",corner(*pts[:3])),("c_n_ca_deg",corner(*pts[1:]))):
            differences[key].append(abs(row[key]-value))
        keys = list(atoms)
        coords = np.stack([atoms[k] for k in keys])
        p = np.array([k[0] for k in keys])
        delta = abs(p[:,None] - p[None,:])
        eligible = np.triu((delta>1) & (delta<7))
        assert np.sum(eligible) == row["nonneighbor_heavy_pair_denominator"]
        assert np.sum(norms(coords[:,None]-coords[None,:],axis=-1)[eligible]<2) == row["heavy_pairs_lt_2_A"]
    assert all(max(vals) < 1e-9 for vals in differences.values())
    return {"backbone":backbone, "reference_radius_A":float(radius),
            "radius_difference_A":abs(radius-backbone["ca_radius_of_gyration_A"]),
            "reference_source":"separately archived wwPDB PDB files; same experiment",
            "reference_maximum_difference":{k:max(v) for k,v in differences.items()},
            "topology_models_tested":len(models), "native_model_1":native["models"][0],
            "declared_closure_native":native["declared_closure"],
            "declared_closure_removed":minus["declared_closure"],
            "removed_declaration":removed[0].decode().strip(),
            "same_geometry_after_removal":native["models"] == minus["models"],
            "reference_tolerance":1e-9}


def prediction():
    folder = ROOT / "native_prediction"
    model = folder / "published_top7_1qys_model.cif"
    data = json.loads((folder / "published_top7_1qys_data.json").read_bytes())
    submitted = json.loads((folder/"input.json").read_bytes())["sequences"][0]["protein"]
    assert submitted == data["sequences"][0]["protein"]
    original = MMCIF2Dict(str(MATERIALS/"1QYS.cif"))
    canonical = "".join(original["_entity_poly.pdbx_seq_one_letter_code_can"][0].split())
    assert submitted["sequence"] == canonical[2:94]
    assert data.get("bondedAtomPairs") in (None, []) and not data.get("userCCD")
    assert submitted["templates"] == [] and submitted["pairedMsa"] == submitted["unpairedMsa"] == ""
    seq, ref, _ = read_pdb(MATERIALS/"1QYS.pdb")
    pred = MMCIF2Dict(str(model))
    names = ("label_atom_id","label_asym_id","label_seq_id","label_comp_id","Cartn_x","Cartn_y","Cartn_z","B_iso_or_equiv")
    points = {}
    for atom, chain, n, residue, x, y, z, conf in zip(*(pred["_atom_site."+f] for f in names),strict=True):
        if atom == "CA" and chain == "A":
            n = int(n)
            assert n not in points
            assert residue == ("MET" if seq[n+1] == "MSE" else seq[n+1])
            points[n] = ([float(x),float(y),float(z)],float(conf))
    assert sorted(points) == list(range(1,93))
    observed = np.stack([ref[1][n+2, "CA"] for n in sorted(points)])
    predicted = np.stack([points[n][0] for n in sorted(points)])
    a = observed - observed.mean(axis=0)
    b = predicted - predicted.mean(axis=0)
    u, _, vt = np.linalg.svd(b.T @ a)
    correction = np.diag([1.,1.,np.linalg.det(u @ vt)])
    rot = u @ correction @ vt
    rmsd = np.sqrt(((b @ rot - a)**2).sum() / len(a))
    independently = QCPSuperimposer()
    independently.set(observed, predicted)
    independently.run()
    other = independently.get_rms()
    assert abs(rmsd-other) < 1e-6
    mean_conf = np.mean([p[1] for p in points.values()])
    return {"method":"fixed published Top7 canonical sequence control; AlphaFold 3 forward inference",
            "input_sha256":digest(folder/"input.json"),"prediction_sha256":digest(model),
            "answer_source_sha256":digest(MATERIALS/"1QYS.pdb"),"declared_residues":106,
            "predicted_positions":[1,92],"reference_positions":[3,94],
            "selenomet_at_experimental_37":"represented by canonical methionine in prediction",
            "matched_calpha_atoms":len(a),"reference_coordinate_fraction":len(a)/len(seq),
            "mean_calpha_plddt":float(mean_conf),"rmsd_A_svd":float(rmsd),"rmsd_A_quaternion":float(other),
            "recalculation_difference_A":abs(rmsd-other),"recalculation_tolerance_A":1e-6,
            "historical_positive_reference_cutoffs":{"maximum_rmsd_A":2,"minimum_mean_calpha_plddt":80},
            "meets_reference_cutoffs":bool(rmsd<=2 and mean_conf>=80),
            "scope":"Single known 2003 deposition. Training familiarity is possible; no novel sequences or generalization tested."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as temp:
        result = {"status":"PASS", "coordinate_methods":reference_controls(Path(temp)), "fixed_prediction":prediction()}
    text = json.dumps(result, indent=2, allow_nan=False) + "\n"
    if args.out:
        args.out.write_text(text)
    else:
        print(text, end="")


if __name__ == "__main__":
    main()
