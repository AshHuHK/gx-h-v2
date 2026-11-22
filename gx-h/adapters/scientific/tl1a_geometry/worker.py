"""Measure the observed chain geometry of the archived TL1A 2RE9 coordinate model."""
import argparse
import hashlib
import itertools
import json
import math
import platform
import shlex
from pathlib import Path
from typing import NamedTuple


SOURCE_SHA = "8c8b9a48cf32d0350327a8c74ecb18bec0ebacb4bdea6f484460fd7b1d5dd89b"
CHAINS = ("A", "B", "C")
BACKBONE = {"N": "N", "CA": "C", "C": "C", "O": "O"}
PROTOCOLS = ("common_backbone", "common_ca", "ordered_backbone")
PAIRS = tuple(itertools.combinations(CHAINS, 2))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def object_digest(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8"))


def validate_criteria(doc):
    if not isinstance(doc, dict) or doc.get("schema") != "gx.tl1a.criteria/1":
        raise ValueError("expected gx.tl1a.criteria/1")
    mapping = {"chains": list(CHAINS), "label_entity_id": "1", "model_id": "1",
               "keys": ["label_seq_id", "label_comp_id", "label_atom_id"],
               "pairs_fixed_moving": [list(pair) for pair in PAIRS]}
    if (doc.get("method") != "tl1a-geometry@1" or doc.get("source_sha256") != SOURCE_SHA
            or doc.get("mapping") != mapping or set(doc.get("protocols", {})) != set(PROTOCOLS)):
        raise ValueError("the criteria must identify the 2RE9 source and versioned atom mapping")
    coverage = doc.get("coverage")
    if (not isinstance(coverage, dict) or coverage.get("denominator") != "observed_union_residues"
            or type(coverage.get("min_residues")) is not int or coverage["min_residues"] < 1):
        raise ValueError("specify a positive residue count against observed_union_residues")
    fields = {"coverage": ("min_fraction",),
              "comparison": ("max_angle_delta_deg", "max_rmsd_delta_angstrom"),
              "reproduction": ("angle_abs_tol_deg", "rmsd_abs_tol_angstrom")}
    for section, names in fields.items():
        if not isinstance(doc.get(section), dict):
            raise ValueError(f"missing criteria section {section}")
        for name in names:
            value = doc[section].get(name)
            if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{section}.{name} must be a finite, nonnegative number")
    if not 0 < coverage["min_fraction"] <= 1:
        raise ValueError("coverage.min_fraction must be in (0, 1]")


class Atom(NamedTuple):
    xyz: tuple
    occupancy: float
    b_factor: float


def _loop_rows(lines, category):
    start = next(i for i, line in enumerate(lines) if line.startswith(category))
    names = []
    while lines[start].startswith(category):
        names.append(lines[start].strip().split(".", 1)[1])
        start += 1
    for line in lines[start:]:
        if line.startswith("#"):
            break
        yield dict(zip(names, line.split(), strict=True))


def read_coordinates(raw):
    # This version accepts exactly the archived deposition. Both relevant loops use
    # one unquoted record per line. Refuse changed input before using that representation.
    if digest(raw) != SOURCE_SHA:
        raise ValueError("coordinate SHA-256 differs from the pinned 2RE9 deposition")
    lines = raw.decode("ascii").splitlines()
    maps = {chain: {} for chain in CHAINS}
    residues = {}
    for row in _loop_rows(lines, "_atom_site."):
        chain, atom = row["label_asym_id"], row["label_atom_id"]
        if row["group_PDB"] != "ATOM" or chain not in maps or atom not in BACKBONE:
            continue
        if (row["label_entity_id"], row["pdbx_PDB_model_num"]) != ("1", "1"):
            raise ValueError("unexpected entity/model for the specified label_asym_ids")
        if row["type_symbol"] != BACKBONE[atom] or row["label_alt_id"] not in ("?", "."):
            raise ValueError("unexpected element or ambiguous backbone alternate location")
        pos, residue = int(row["label_seq_id"]), row["label_comp_id"]
        key = (pos, residue, atom)
        if residues.setdefault(pos, residue) != residue or key in maps[chain]:
            raise ValueError(f"ambiguous atom correspondence at {chain}/{key}")
        xyz = tuple(float(row["Cartn_" + axis]) for axis in "xyz")
        occupancy, b_factor = float(row["occupancy"]), float(row["B_iso_or_equiv"])
        if not all(math.isfinite(n) for n in (*xyz, occupancy, b_factor)) or not 0 <= occupancy <= 1:
            raise ValueError(f"invalid observation at {chain}/{key}")
        if occupancy > 0:
            maps[chain][key] = Atom(xyz, occupancy, b_factor)

    tags = ("_entry.id", "_exptl.method", "_refine.ls_d_res_high")
    metadata = {words[0]: words[1] for line in lines if line.startswith(tags)
                for words in [shlex.split(line)] if len(words) == 2}
    declared = {int(row["num"]) for row in _loop_rows(lines, "_entity_poly_seq.") if row["entity_id"] == "1"}
    observed = {key[0] for chain in maps.values() for key in chain}
    source = {"pdb_id": metadata["_entry.id"], "model_id": "1", "label_entity_id": "1",
              "chains": list(CHAINS), "experimental_method": metadata["_exptl.method"],
              "resolution_angstrom": float(metadata["_refine.ls_d_res_high"]),
              "declared_polymer_residues": len(declared), "unobserved_polymer_positions": sorted(declared - observed),
              "atom_site_locator": "_atom_site, model 1, label_entity_id 1, label_asym_id A/B/C"}
    return source, maps


def positions(keys):
    return {key[0] for key in keys}


def coordinate_quality(source, atoms):
    sets = [set(chain) for chain in atoms.values()]
    common = set.intersection(*sets)
    union = set.union(*sets)
    complete = {}
    for key in common:
        if all(chain[key].occupancy == 1 and chain[key].b_factor <= 40 for chain in atoms.values()):
            complete.setdefault(key[0], set()).add(key[2])
    ordered = {key for key in common if complete.get(key[0]) == BACKBONE.keys()}
    masks = {"common_backbone": common, "common_ca": {k for k in common if k[2] == "CA"},
             "ordered_backbone": ordered}
    totals = {"observed_union_atoms": len(union), "observed_union_residues": len(positions(union)),
              "common_backbone_atoms": len(common), "common_backbone_residues": len(positions(common))}
    per_chain = {chain: {"observed_backbone_atoms": len(keys), "observed_residues": len(positions(keys)),
                         "absent_from_observed_union_positions": sorted(positions(union) - positions(keys))}
                 for chain, keys in atoms.items()}
    mask_records = {name: {"selected_keys": [list(k) for k in sorted(keys)],
                           "selected_atoms": len(keys), "selected_residues": len(positions(keys))}
                    for name, keys in masks.items()}
    return {"schema": "gx.tl1a.coordinate_quality/1", "method": "tl1a-coordinate-quality@1",
            "input_sha256": SOURCE_SHA, "source": source, "per_chain": per_chain,
            "mapping": totals, "protocol_masks": mask_records,
            "runtime": {"python": platform.python_version(), "implementation": "stdlib atom-site inventory"}}


def _largest_eigenvector(matrix):
    """Diagonalize Horn's real symmetric 4x4 matrix using Jacobi rotations."""
    vectors = [[float(i == j) for j in range(4)] for i in range(4)]
    scale = max(abs(x) for row in matrix for x in row)
    if scale == 0:
        raise ValueError("zero covariance")
    for _ in range(100):
        p, q = max(itertools.combinations(range(4), 2), key=lambda pair: abs(matrix[pair[0]][pair[1]]))
        cross, a, b = matrix[p][q], matrix[p][p], matrix[q][q]
        if abs(cross) <= scale * 1e-15:
            break
        theta = 0.5 * math.atan2(2 * cross, b - a)
        c, s = math.cos(theta), math.sin(theta)
        for k in range(4):
            if k not in (p, q):
                x, y = matrix[k][p], matrix[k][q]
                matrix[k][p] = matrix[p][k] = c * x - s * y
                matrix[k][q] = matrix[q][k] = s * x + c * y
            x, y = vectors[k][p], vectors[k][q]
            vectors[k][p], vectors[k][q] = c * x - s * y, s * x + c * y
        matrix[p][p] = c*c*a - 2*c*s*cross + s*s*b
        matrix[q][q] = s*s*a + 2*c*s*cross + c*c*b
        matrix[p][q] = matrix[q][p] = 0.0
    else:
        raise ValueError("the proper rotation eigensolver failed to converge")
    best, second = sorted(range(4), key=lambda i: matrix[i][i], reverse=True)[:2]
    if matrix[best][best] - matrix[second][second] <= scale * 1e-12:
        raise ValueError("overlap does not identify a unique proper rotation")
    return tuple(row[best] for row in vectors)


def _center(xyz):
    mean = [math.fsum(axis) / len(xyz) for axis in zip(*xyz)]
    return [tuple(value - mu for value, mu in zip(row, mean)) for row in xyz]


def fit(fixed, moving):
    """Horn's unit quaternion maximizes the dot product of the centered clouds."""
    if len(fixed) < 3:
        raise ValueError("fewer than three matched atoms")
    target, mobile = _center(fixed), _center(moving)
    covariance = [[math.fsum(x[i]*y[j] for x, y in zip(mobile, target)) for j in range(3)]
                  for i in range(3)]
    (xx, xy, xz), (yx, yy, yz), (zx, zy, zz) = covariance
    matrix = [[xx+yy+zz, yz-zy, zx-xz, xy-yx],
              [yz-zy, xx-yy-zz, xy+yx, zx+xz],
              [zx-xz, xy+yx, -xx+yy-zz, yz+zy],
              [xy-yx, zx+xz, yz+zy, -xx-yy+zz]]
    w, x, y, z = _largest_eigenvector(matrix)
    rotation = [[1-2*(y*y+z*z), 2*(x*y-z*w), 2*(x*z+y*w)],
                [2*(x*y+z*w), 1-2*(x*x+z*z), 2*(y*z-x*w)],
                [2*(x*z-y*w), 2*(y*z+x*w), 1-2*(x*x+y*y)]]
    residual = math.fsum((sum(a*b for a, b in zip(axis, point)) - target_coord)**2
                         for point, other in zip(mobile, target)
                         for axis, target_coord in zip(rotation, other))
    return math.degrees(2*math.acos(min(1.0, abs(w)))), math.sqrt(residual / len(target))


def measure(raw, criteria, protocol, quality_raw=None):
    validate_criteria(criteria)
    if protocol not in PROTOCOLS:
        raise ValueError(f"unknown atom-selection protocol {protocol}")
    source, atoms = read_coordinates(raw)
    if quality_raw is None:
        quality = coordinate_quality(source, atoms)
    else:
        quality = json.loads(quality_raw)
        if (quality.get("schema") != "gx.tl1a.coordinate_quality/1"
                or quality.get("method") != "tl1a-coordinate-quality@1"
                or quality.get("input_sha256") != SOURCE_SHA or quality.get("source") != source):
            raise ValueError("the supplied coordinate inventory does not describe this deposited source")
    mask = quality["protocol_masks"][protocol]
    keys = [tuple(k) for k in mask["selected_keys"]]
    n_positions = len(positions(keys))
    key_set = set(keys)
    if (keys != sorted(key_set) or len(keys) != mask["selected_atoms"]
            or n_positions != mask["selected_residues"] or any(not key_set <= chain.keys() for chain in atoms.values())):
        raise ValueError("the coordinate inventory contains inconsistent selected atom identities")
    totals = quality["mapping"]
    fraction = n_positions / totals["observed_union_residues"]
    mapping = {**totals, "selected_atoms": len(keys), "selected_residues": n_positions,
               "selected_keys_sha256": object_digest(mask["selected_keys"]),
               "selected_fraction_of_observed_union_residues": fraction}
    issues, pairs = [], []
    if n_positions < criteria["coverage"]["min_residues"] or fraction < criteria["coverage"]["min_fraction"]:
        issues.append("selected residue count or observed-union coverage is below the frozen minimum")
    for a, b in PAIRS:
        try:
            angle, rmsd = fit([atoms[a][k].xyz for k in keys], [atoms[b][k].xyz for k in keys])
        except ValueError as exc:
            issues.append(f"{a}/{b}: {exc}")
            continue
        pairs.append({"fixed_chain": a, "moving_chain": b, "rotation_angle_deg": angle,
                      "fitted_rmsd_angstrom": rmsd, "absolute_departure_from_120_deg": abs(angle - 120)})
    return {"schema": "gx.tl1a.geometry/1", "method": "tl1a-geometry@1",
            "status": "insufficient" if issues else "measured", "issues": issues,
            "protocol": protocol, "input_sha256": SOURCE_SHA, "source": source,
            "quality_sha256": digest(quality_raw) if quality_raw is not None else None,
            "criteria_spec_sha256": object_digest(criteria), "mapping": mapping, "pairs": pairs,
            "runtime": {"python": platform.python_version(), "fit": "Horn proper quaternion, Jacobi eigensolver"},
            "units": {"rotation_angle_deg": "degrees", "fitted_rmsd_angstrom": "angstrom"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    quality = commands.add_parser("quality", help="measure coordinate coverage and atom selections")
    geometry = commands.add_parser("geometry", help="fit the selected atoms of all three chains")
    for command in (quality, geometry):
        command.add_argument("--structure", required=True, type=Path)
        command.add_argument("--out", required=True, type=Path)
    geometry.add_argument("--protocol", choices=PROTOCOLS, required=True)
    geometry.add_argument("--criteria", required=True, type=Path)
    selection = geometry.add_mutually_exclusive_group()
    selection.add_argument("--quality", type=Path, help="use a coordinate quality Job's inventory")
    selection.add_argument("--selection", type=Path, help="quality inventory or the original criteria for direct analysis")
    args = parser.parse_args()
    raw = args.structure.read_bytes()
    if args.command == "quality":
        product = coordinate_quality(*read_coordinates(raw))
    else:
        criteria_raw = args.criteria.read_bytes()
        inventory = args.quality.read_bytes() if args.quality else None
        if args.selection:
            recipe = args.selection.read_bytes()
            inventory = None if recipe == criteria_raw else recipe
        product = measure(raw, json.loads(criteria_raw), args.protocol, inventory)
        product["criteria_sha256"] = digest(criteria_raw)
    args.out.mkdir(exist_ok=True, parents=True)
    (args.out / (args.command + ".json")).write_text(json.dumps(product, sort_keys=True,
        ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
