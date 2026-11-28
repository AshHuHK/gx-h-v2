"""Prepare two CC0 archive derivatives and independently count the frozen coordinate endpoint.

Only coordinate observation rows are withheld. The deposited sequence, coordinates of
retained atoms, and parent revision remain unchanged. The assignment map stays with
the rehearsal operator; none of this module is imported by the scripted researcher.
"""
from hashlib import sha256
from pathlib import Path
import json

from Bio.PDB.MMCIF2Dict import MMCIF2Dict

ROOT = Path(__file__).resolve().parents[2]
PARENT = ROOT / "gx-h/adapters/scientific/tl1a_geometry/data/2RE9.cif"
PROVENANCE = ROOT / "acceptance/v61/adaptive/data/provenance.json"
PARENT_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"
SCOPE = "Deposited model 1, entity 1, label chains A/B/C; observational completeness only."
ATOMS = {"N", "CA", "C", "O"}


def digest(raw):
    return sha256(raw).hexdigest()


def save(path, value):
    path.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


def coverage(path):
    """An independent Biopython parser, separate from the GX method and its fitted structures."""
    cif = MMCIF2Dict(str(path))
    fields = ["group_PDB", "pdbx_PDB_model_num", "label_entity_id", "label_asym_id", "label_seq_id",
              "label_comp_id", "label_atom_id", "occupancy", "label_alt_id"]
    sites = {ch: set() for ch in "ABC"}
    for group, model, entity, ch, pos, comp, atom, occupancy, alt in zip(
            *(cif["_atom_site." + field] for field in fields), strict=True):
        if (group, model, entity) == ("ATOM", "1", "1") and ch in sites and atom in ATOMS and float(occupancy) > 0:
            if alt not in (".", "?"):
                raise ValueError("The frozen endpoint does not admit ambiguous alternate conformations")
            sites[ch].add((int(pos), comp, atom))
    universe = {pos for chain in sites.values() for pos, _, _ in chain}
    common = set.intersection(*sites.values())
    complete = {pos for pos, comp, _ in common if all((pos, comp, a) in common for a in ATOMS)}
    if not universe:
        raise ValueError("The assigned data contain no observed position in the frozen scope")
    return {"selected": len(complete), "observed": len(universe), "mask": "common_backbone"}


def parent_acquisition():
    provenance = json.loads(PROVENANCE.read_text())
    entry = next(row for row in provenance["data"] if row["entry"] == "2RE9")
    assert entry["sha256"] == PARENT_SHA
    return {"source": [entry["coordinate_url"] + "#" + entry["revision"]],
            "acquisition": ["wwPDB:2RE9:" + entry["revision"]], "publication": [provenance["original_article"]],
            "processing": [], "sample": None, "sampling": None, "applicability": SCOPE}


def prepare(directory):
    directory.mkdir(mode=0o700, parents=True)
    raw = PARENT.read_bytes()
    if digest(raw) != PARENT_SHA:
        raise ValueError("The public parent is not the registered coordinate revision")
    parts = raw.splitlines(keepends=True)
    headers = [line.split()[0].decode().split(".", 1)[1] for line in parts if line.startswith(b"_atom_site.")]
    if len(headers) != len(set(headers)):
        raise ValueError("The frozen public revision should have one atom site loop")
    answer = {}
    # Neutral identifiers are assigned by the operator and are not material names in a Goal.
    for code, (modulus, residues) in {"K7": (3, [0, 1]), "P4": (11, [0])}.items():
        excluded, kept = [], []
        for line in parts:
            if line.startswith(b"ATOM "):
                row = dict(zip(headers, line.decode("ascii").split(), strict=True))
                if (row["pdbx_PDB_model_num"], row["label_entity_id"], row["label_asym_id"],
                    row["label_atom_id"]) == ("1", "1", "A", "O") and int(row["label_seq_id"]) % modulus in residues:
                    excluded.append(row["id"])
                    continue
            kept.append(line)
        slot = directory / code
        slot.mkdir(mode=0o700)
        file = slot / "specimen.cif"
        file.write_bytes(b"".join(kept))
        truth = coverage(file)
        transform = {"schema": "gx.public-archive-coordinate-withholding/1", "parent_sha256": PARENT_SHA,
                     "operation": "withhold complete archived atom_site rows", "group": "ATOM", "model": "1",
                     "entity": "1", "chain": "A", "atom": "O", "label_seq_id_modulus": modulus,
                     "residues_to_withhold": residues, "removed_ids_sha256": digest("\n".join(excluded).encode())}
        save(slot / "transform.json", transform)
        answer[code] = {"sha256": digest(file.read_bytes()), "transform_sha256": digest((slot/"transform.json").read_bytes()),
                        **truth, "expected": "CHANGE" if truth["selected"]*5 < truth["observed"]*3 else "MAINTAIN"}
    assert {x['expected'] for x in answer.values()} == {'CHANGE','MAINTAIN'}
    assert len({x['observed'] for x in answer.values()}) == 1
    save(directory / "oracle.json", answer)
    return answer
