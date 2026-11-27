"""Prepare site-relative Goal material and provenance from pinned wwPDB originals."""
from hashlib import sha256
import json
from pathlib import Path
from Bio.PDB.MMCIF2Dict import MMCIF2Dict

HERE = Path(__file__).resolve().parent


def write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def prepare(case, entry):
    root = HERE / case
    coords = HERE / "materials" / (entry + ".cif")
    structured = MMCIF2Dict(str(coords))
    rev = ".".join(structured["_pdbx_audit_revision_history." + key][-1]
                   for key in ("major_revision", "minor_revision"))
    doi = structured["_citation.pdbx_database_id_DOI"][0]
    write(root / "acquisition.json", {
        "source": "https://files.rcsb.org/download/" + coords.name + "#" + rev,
        "acquisition": [f"wwPDB:{entry}:{rev}"], "publication": (["https://doi.org/" + doi] if doi not in (".", "?") else None),
        "processing": [], "sample": None, "sampling": None,
        "applicability": f"Original experimental archive record {entry} revision {rev}. Limited to this deposition."
    })
    pdb = HERE / "materials" / (entry + ".pdb")
    lines = [line for line in pdb.read_text().splitlines() if line.startswith(
        ("HEADER", "TITLE ", "CAVEAT", "EXPDTA", "REMARK   2", "REMARK 200", "REMARK 465", "REMARK 470"))]
    note = root / "experimental-record.txt"
    note.write_text(f"Extract from wwPDB {pdb.name}, SHA-256 {sha256(pdb.read_bytes()).hexdigest()}. "
                    "Experimental and coordinate-omission records, when present, from the same deposition.\n\n"
                    + "\n".join(lines) + "\n")
    write(root / "goal.json", {
        "project": "two-deposited-chain-qc", "budget_profile": "oauth_research", "research_effort": "deep",
        "capability_collections": ["archived_structure"], "max_parallel_roots": 4,
        "input_files": [{"role": "structure", "path": "../materials/" + coords.name,
                         "sha256": sha256(coords.read_bytes()).hexdigest(),
                         "acquisition": "acquisition.json"},
                        {"role": "literature", "path": note.name, "sha256": sha256(note.read_bytes()).hexdigest()}]
    })


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case", choices=("case-a", "case-b"))
    parser.add_argument("entry")
    args = parser.parse_args()
    prepare(args.case, args.entry)
