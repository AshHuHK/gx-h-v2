"""Independently compare one new natural food-protein prediction and the frozen experiment."""
import argparse
import gzip
from contextlib import closing
from pathlib import Path
import hashlib
import json
import sqlite3
from tempfile import TemporaryDirectory

from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from Bio.PDB.qcprot import QCPSuperimposer
import numpy as np


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def experiment(path):
    """Read legacy PDB fixed columns independently from the assay plugin's mmCIF reader."""
    points = {}
    for line in path.read_text().splitlines():
        if line.startswith("ENDMDL"):
            break
        if line[:6] != "ATOM  " or line[21] != "A" or line[12:16].strip() != "CA" or float(line[54:60]) <= 0:
            continue
        pos = int(line[22:26])
        if line[16] != " " or line[26] != " " or pos in points:
            raise ValueError(f"Ambiguous reference C-alpha at chain A position {pos}.")
        points[pos] = (line[17:20].strip(), [float(line[i:i+8]) for i in (30,38,46)])
    return points


def recompute(model, vault):
    mapping = json.loads((vault / "mapping.json").read_bytes())["pairs"]
    ref = experiment(vault / "1F6S.pdb")
    cif = MMCIF2Dict(str(model))
    cols = ("pdbx_PDB_model_num", "label_asym_id", "label_seq_id", "label_atom_id", "label_comp_id",
            "label_alt_id", "occupancy", "Cartn_x", "Cartn_y", "Cartn_z", "B_iso_or_equiv")
    predicted = {}
    for mod, chain, seq, atom, aa, alt, occupancy, x, y, z, conf in zip(*(cif["_atom_site." + k] for k in cols), strict=True):
        if (mod, chain, atom) != ("1", "A", "CA") or float(occupancy) <= 0 or seq in ("?", "."):
            continue
        pos = int(seq)
        if pos in predicted or alt not in (".", "?"):
            raise ValueError(f"Ambiguous model C-alpha at position {pos}.")
        predicted[pos] = (aa, [float(x), float(y), float(z)], float(conf))
    pairs = [(a, b) for a, b in mapping if a in predicted and b in ref]
    if len(pairs) < 3:
        raise ValueError("Fewer than three independently comparable positions.")
    if any(predicted[a][0] != ref[b][0] for a, b in pairs):
        raise ValueError("The prediction/reference residue identities do not agree under the original pose map.")
    qcp = QCPSuperimposer()
    qcp.set(np.array([ref[b][1] for _,b in pairs]), np.array([predicted[a][1] for a,_ in pairs]))
    qcp.run()
    rmsd, coverage = float(qcp.get_rms()), len(pairs) / len(mapping)
    category = ("insufficient_coverage" if coverage < 0.90 else
                "agrees_with_deposited_backbone" if rmsd <= 2.0 else "outside_frozen_rmsd_tolerance")
    return dict(legacy_pdb_sha256=sha(vault / "1F6S.pdb"), mapping_sha256=sha(vault / "mapping.json"),
                coordinate_sha256=sha(model), comparable=len(pairs), mapped=len(mapping), coverage=coverage,
                calpha_rmsd_A=rmsd, mean_predicted_calpha_plddt=float(np.mean([predicted[a][2] for a,_ in pairs])),
                disposition=category)


def verify(state, vault):
    frozen = json.loads((Path(__file__).parent / "preregistration.json").read_bytes())
    for name in ("1F6S.pdb","1F6S.cif","mapping.json"):
        if sha(vault / name) != frozen["custody"]["vault_digests"][name]:
            raise ValueError("Evaluation material differs from the prospectively registered original.")
    with closing(sqlite3.connect((state / "gx.db").resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        studies = list(conn.execute("SELECT run_id,status FROM v6_goals"))
        if len(studies) != 1:
            raise ValueError("Select the private field workspace for exactly one prospective study.")
        run_id, status = studies[0]
        rows = list(conn.execute("""SELECT j.capability_id,a.artifact_ref,a.job_id,a.namespace,a.rel_path,a.sha256
            FROM artifacts a JOIN jobs j USING(run_id,job_id) WHERE a.run_id=? AND a.published=1
            AND j.status='SUCCEEDED' AND j.capability_id IN ('natural-food-fold','mapped-structure-agreement')""", (run_id,)))
    products = {}
    for row in rows:
        name = Path(row["rel_path"]).name
        if name not in {"model.cif", "inference.json", "agreement.json", "provider-events.jsonl", "TERMS_OF_USE.md"}:
            continue
        if name in products:
            raise ValueError(f"Duplicate published product {name}. Preserve both attempts for review.")
        path = state / "artifacts" / row["namespace"] / row["rel_path"]
        if sha(path) != row["sha256"]:
            raise ValueError(f"Published {name} contents differ from the accepted artifact.")
        products[name] = path, dict(row)
    if "model.cif" not in products:
        return dict(run_id=run_id, goal_status=status, independent_evaluation="no_published_prediction")
    model = products["model.cif"][0]
    measured = recompute(model, vault)
    inference = json.loads(products["inference.json"][0].read_bytes())
    if inference["model_sha256"] != sha(model) or inference["input_sha256"] != frozen["visible"]["materials/P00711.json"]:
        raise ValueError("The model publication has a different specimen or model hash.")
    result = dict(run_id=run_id, goal_status=status, independent_QCP=measured, measured_by_job=None,
                  artifacts={n:v[1] for n,v in products.items()}, method=inference,
                  upstream_output_terms_sha256=sha(products["TERMS_OF_USE.md"][0]),
                  agreement_check="pending_GX_scorer")
    if "agreement.json" in products:
        job = json.loads(products["agreement.json"][0].read_bytes())
        if (job["input_sha256"] != sha(model) or job["reference_sha256"] != sha(vault/"1F6S.cif")
                or job["mapping_sha256"] != measured["mapping_sha256"]):
            raise ValueError("Scorer used a different model, reference or position map.")
        delta = abs(job["calpha_rmsd_A"] - measured["calpha_rmsd_A"])
        ok = delta <= 1e-5 and (job["matched_positions"],job["mapped_positions"]) == (measured["comparable"],measured["mapped"])
        result.update(measured_by_job=job, independent_difference_A=delta, agreement_check="PASS" if ok else "DISAGREE")
    return result


def published():
    """Replay the released experiment and prediction without access to the original station."""
    directory = Path(__file__).resolve().parent
    frozen = json.loads((directory / "preregistration.json").read_bytes())
    inputs, products = directory / "released-originals", directory / "first-run"
    with TemporaryDirectory() as scratch:
        tmp = Path(scratch)
        for name in ("1F6S.pdb","1F6S.cif","P00711.json"):
            path = tmp / name
            path.write_bytes(gzip.decompress((inputs / (name + ".gz")).read_bytes()))
            expected = (frozen["uniprot"]["record_sha256"] if name == "P00711.json" else
                        frozen["custody"]["vault_digests"][name])
            if sha(path) != expected:
                raise ValueError("The released original differs from the registered content hash.")
        mapping = tmp / "mapping.json"
        mapping.write_bytes((inputs / "mapping.json").read_bytes())
        if sha(mapping) != frozen["custody"]["vault_digests"]["mapping.json"]:
            raise ValueError("Released residue map differs from the pre-registered pairs.")
        measurement = recompute(products / "model.cif", tmp)
        metadata = json.loads((products / "inference.json").read_bytes())
        if (measurement["coordinate_sha256"] != metadata["model_sha256"]
                or metadata["input_sha256"] != frozen["visible"]["materials/P00711.json"]):
            raise ValueError("Released coordinates differ from the new inference.")
        result = {"independent_QCP": measurement, "published_scorer": "not_available"}
        if (products / "agreement.json").exists():
            job = json.loads((products / "agreement.json").read_bytes())
            if (job["input_sha256"] != sha(products/"model.cif") or job["reference_sha256"] != sha(tmp/"1F6S.cif")
                    or job["mapping_sha256"] != sha(mapping)):
                raise ValueError("Incompatible published evaluation inputs.")
            delta = abs(job["calpha_rmsd_A"] - measurement["calpha_rmsd_A"])
            counts = job["matched_positions"] == measurement["comparable"] and job["mapped_positions"] == measurement["mapped"]
            result.update(published_scorer="PASS" if delta <= 1e-5 and counts else "DISAGREE", difference_A=delta)
        return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--released", action="store_true", help="Recompute from the bundled original and prediction files.")
    p.add_argument("state", type=Path, nargs="?")
    p.add_argument("vault", type=Path, nargs="?")
    args = p.parse_args()
    if not (args.released and args.state is None and args.vault is None or
            not args.released and args.state is not None and args.vault is not None):
        p.error("Use --released alone, or supply both the Run state and its original-material directory.")
    print(json.dumps(published() if args.released else verify(args.state, args.vault), indent=2))
