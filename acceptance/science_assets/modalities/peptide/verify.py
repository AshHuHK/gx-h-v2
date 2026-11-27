"""Replay two peptide deposits and verify integer counts against independently parsed legacy PDB records."""
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[3]
ASSETS = HERE / "materials"
SCRIPT = ROOT / "gx-h/plugins/installed/org.gx.archival-molecule-qc/1.0.0/job-scripts/peptide.py"
OLD_SCRIPT = ROOT / "gx-h/plugins/installed/org.gx.deposited-structure-qc/1.0.0/job-scripts/qc.py"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reference(path):
    """Count A-chain positions in these two PDB files with ordinary fixed-column records."""
    residues, links = [], []
    models = defaultdict(lambda: defaultdict(set))
    model = None
    for line in path.read_text().splitlines():
        tag = line[:6].strip()
        if tag == "SEQRES" and line[11] == "A":
            residues.extend(line[19:70].split())
        elif tag == "MODEL":
            model = int(line[10:14])
        elif tag == "LINK" and line[21] == line[51] == "A":
            links.append({"from": [line[17:20].strip(), int(line[22:26]), line[12:16].strip()],
                          "to": [line[47:50].strip(), int(line[52:56]), line[42:46].strip()]})
        elif tag in ("ATOM", "HETATM") and line[21] == "A" and float(line[54:60]) > 0:
            pos = int(line[22:26])
            if 1 <= pos <= len(residues) and line[17:20].strip() == residues[pos-1]:
                if line[76:78].strip() not in {"H", "D"}:
                    assert model is not None and line[16] == " "
                    models[model][pos].add(line[12:16].strip())
    return {"declared_positions": len(residues), "residue_count_excluding_NH2": len(residues) - residues.count("NH2"),
            "c_terminal_component": residues[-1], "links": links,
            "models": {number: {"observed_positions": len(sites),
                       "backbone_positions": sum({"N","CA","C","O"} <= atoms for atoms in sites.values())}
                       for number, sites in models.items()}}


def run():
    evidence = {"transport": "direct local subprocess; independent PDB fixed-column recomputation",
                "integer_tolerance": 0, "input_checks": []}
    for item in json.loads((HERE / "sources.json").read_text())["records"]:
        assert digest(ASSETS / item["file"]) == item["sha256"]
    with tempfile.TemporaryDirectory() as tmp:
        for code in ("1UAO", "1LE0"):
            cif, pdb = ASSETS / (code + ".cif"), ASSETS / (code + ".pdb")
            out = Path(tmp) / code
            subprocess.run([sys.executable, "-IB", str(SCRIPT), "--structure", str(cif), "--out", str(out)], check=True)
            reading = json.loads((out / "peptide-qc.json").read_text())
            independent = reference(pdb)
            assert reading["input_sha256"] == digest(cif)
            assert reading["entry"] == code and reading["chain"] == "A"
            assert reading["declared_positions"] == independent["declared_positions"]
            assert reading["peptide_residue_positions"] == independent["residue_count_excluding_NH2"]
            assert reading["model_count"] == len(independent["models"])
            for model in reading["models"]:
                ref = independent["models"][model["model"]]
                assert (model["observed_positions"],model["backbone_positions"]) == (
                       ref["observed_positions"],ref["backbone_positions"])
            if code == "1LE0":
                assert independent["links"] == [{"from":["LYS",12,"C"], "to":["NH2",13,"N"]}]
                group, = reading["non_peptide_components"]
                assert (group["comp_id"], group["label_seq_id"]) == ("NH2",13)
                binding, = group["in_chain_covalent_links"]
                assert (binding["partner_component"],binding["partner_position"]) == ("LYS",12)
            subprocess.run([sys.executable, "-IB", str(OLD_SCRIPT), "--mode", "backbone", "--structure", str(cif),
                            "--out", str(out / "baseline")], check=True)
            old = json.loads((out / "baseline/qc.json").read_text())
            evidence["input_checks"].append({"entry":code, "original_cif_sha256":digest(cif),
                "pdb_sha256":digest(pdb), "model_count_checked":len(independent["models"]),
                "model_1_counts":independent["models"][1], "declared_positions":independent["declared_positions"],
                "peptide_residues":independent["residue_count_excluding_NH2"], "PDB_links":independent["links"],
                "reported_terminal_components":reading["non_peptide_components"],
                "modified_residue_annotation_status":reading["annotation_status"],
                "earlier_backbone_output":{"declared_residues": old["declared_residues"],
                    "chemistry_evidence": old.get("non_peptide_components"), "complete_backbones":old["complete_backbones"]}})
    return evidence


if __name__ == "__main__":
    print(json.dumps(run(), indent=2))
