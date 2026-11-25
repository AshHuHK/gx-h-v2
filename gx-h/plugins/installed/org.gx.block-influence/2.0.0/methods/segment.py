"""One preregistered mapped interval, measured on the immutable original mmCIF."""
import argparse
import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
from statistics import median


FITTER_SHA = "f6a589ae44c53725d8066988cda86b1212aad19d26f89660a2e49a3e96f13003"
ENDPOINTS = {"angle_deg": (0, "deg", 0.5, 0.02), "rmsd_A": (1, "Å", 0.15, 0.002)}
FIELDS = ("group_PDB label_entity_id pdbx_PDB_model_num label_asym_id label_seq_id label_comp_id "
          "label_atom_id type_symbol label_alt_id occupancy Cartn_x Cartn_y Cartn_z")


def released_geometry(file):
    if hashlib.sha256(file.read_bytes()).hexdigest() != FITTER_SHA:
        raise ValueError("Released Horn fitter checksum mismatch")
    spec = importlib.util.spec_from_file_location("released_horn_fit", file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def position_map(data, raw, interval):
    m = json.loads(data)
    if (m.get("schema") != "gx.coordinate-position-map/1"
            or m.get("structure_sha256") != hashlib.sha256(raw).hexdigest()
            or not all(isinstance(m.get(k), str) and m[k] not in ("", ".", "?") for k in ("model", "entity", "reference"))
            or not isinstance(m.get("chains"), list) or len(m["chains"]) != 3
            or any(not isinstance(c, str) or c in ("", ".", "?") for c in m["chains"])
            or len(set(m["chains"])) != 3):
        raise ValueError("Invalid frozen material, model, entity or three-chain binding")
    if interval not in ("21-40", "101-120"):
        raise ValueError("Unregistered reference interval")
    segment = m["segments"][interval]
    if not isinstance(segment, list) or len(segment) != 20:
        raise ValueError("Supply one target label position or null per reference position")
    nonmissing = [p for p in segment if p is not None]
    if (any(type(p) is not int or p <= 0 for p in nonmissing)
            or nonmissing != sorted(set(nonmissing))):
        raise ValueError("The reference-to-target mapping must be ordered and one-to-one")
    return m, segment


def atom_selection(raw, mapping, mask, loop_rows):
    if mask not in ("common_ca", "common_backbone"):
        raise ValueError("Unknown atom selection")
    lines = raw.decode("utf-8").splitlines()
    entries = [line.split() for line in lines if line.startswith("_entry.id ")]
    if (len(entries) != 1 or len(entries[0]) != 2
            or sum(line.lower().startswith("data_") for line in lines) != 1
            or sum(line.strip() == "_atom_site.group_PDB" for line in lines) != 1
            or sum(line.strip() == "_entity_poly_seq.num" for line in lines) != 1):
        raise ValueError("Expected one deposited entry and tabular atom_site and entity_poly_seq loops")
    entry = entries[0][1].strip("'\"")
    if entry in ("", "?", "."):
        raise ValueError("Missing deposited entry identifier")
    entity, model, chains = mapping["entity"], mapping["model"], mapping["chains"]
    elements = {"N":"N", "CA":"C", "C":"C", "O":"O"}
    required = ("CA",) if mask == "common_ca" else tuple(elements)
    coordinates = {c: {} for c in chains}
    identity = {c: {} for c in chains}
    union = set()
    fields = FIELDS.split()
    for row in loop_rows(lines, "_atom_site."):
        group, ent, mod, chain, label, res, atom, element, alt, occupancy, *xyz = (row[k].strip("'\"") for k in fields)
        if (group, ent, mod) != ("ATOM", entity, model) or chain not in coordinates or atom not in elements:
            continue
        occ = float(occupancy)
        if not 0 <= occ <= 1:
            raise ValueError("Invalid occupancy")
        if occ == 0:
            continue
        pos = int(label)
        if pos <= 0 or element != elements[atom]:
            raise ValueError("Unexpected label position or backbone element")
        union.add(pos)
        prior = identity[chain].setdefault(pos, res)
        if prior != res or res in (".", "?"):
            identity[chain][pos] = None
        if atom not in required:
            continue
        point = tuple(map(float, xyz))
        if not all(math.isfinite(x) for x in point):
            raise ValueError("Non-finite coordinate")
        key = (pos, atom)
        if alt not in (".", "?") or key in coordinates[chain]:
            coordinates[chain][key] = None
        else:
            coordinates[chain][key] = point
    matched = []
    for p in sorted(union):
        names = {identity[c].get(p) for c in chains}
        if len(names) == 1 and None not in names and all(coordinates[c].get((p,a)) is not None
                                                       for c in chains for a in required):
            matched.append(p)
    declared = {int(row["num"]) for row in loop_rows(lines, "_entity_poly_seq.")
                if row["entity_id"].strip("'\"") == entity}
    if not declared or not union <= declared:
        raise ValueError("Coordinate labels must refer to a declared polymer entity")
    return entry, coordinates, matched, required, union, declared


def change(before, after):
    return {"before": before, "after": after, "delta": abs(after-before) if before is not None and after is not None else None}


def measure(raw, mapping_raw, interval, mask, endpoint, geometry):
    mapping, target = position_map(mapping_raw, raw, interval)
    idx, unit, limit, tol = ENDPOINTS[endpoint]
    entry, coordinates, matched, atoms, union, declared = atom_selection(raw, mapping, mask, geometry._loop_rows)
    mapped = {n for n in target if n is not None}
    if not mapped <= declared:
        raise ValueError("A frozen mapped position falls outside the declared target polymer; gaps require null")
    chains = mapping["chains"]
    omitted = set(matched) & mapped
    remaining = [p for p in matched if p not in omitted]
    reasons = []
    if len(matched) < 60: reasons.append("fewer_than_60_before")
    if not union or len(matched)*5 < len(union)*3: reasons.append("insufficient_observed_coverage")
    if len(omitted) < 10: reasons.append("fewer_than_10_mapped_matched_in_interval")
    if len(remaining) < 60: reasons.append("fewer_than_60_after")
    if len(remaining)*5 < len(matched)*3: reasons.append("retained_below_60_percent")

    def fitted(a,b,positions):
        keys = [(p,atom) for p in positions for atom in atoms]
        return geometry.fit([coordinates[a][k] for k in keys], [coordinates[b][k] for k in keys])[idx]

    rows = {}
    for i,j in itertools.combinations(range(3),2):
        a,b = chains[i],chains[j]
        key = f"pair{i+1}{j+1}"
        values = {"before": None, "after": None}
        errors = {}
        if not reasons:
            for stage, positions in (("before", matched), ("after", remaining)):
                try:
                    values[stage] = fitted(a, b, positions)
                except ValueError as exc:
                    errors[stage] = str(exc)
        rows[key] = {"chains": [a, b], endpoint: change(**values)}
        if errors:
            rows[key]["reason"] = errors
    def representative(stage):
        values = [row[endpoint][stage] for row in rows.values()]
        return median(values) if None not in values else None

    med = change(representative("before"), representative("after"))
    exceptions = [k for k,r in rows.items() if r[endpoint]["delta"] is not None and r[endpoint]["delta"]>limit]
    borderline = [k for k,r in rows.items() if r[endpoint]["delta"] is not None and abs(r[endpoint]["delta"]-limit)<=tol]
    rows["median"] = {"chains":chains,endpoint:med}
    if med["delta"] is not None and abs(med["delta"]-limit)<=tol:
        borderline.append("median")
    concealed = bool(exceptions and med["delta"]<=limit) if med["delta"] is not None else None
    if med["delta"] is None:
        outcome="UNDETERMINED"
    elif borderline:
        outcome="BORDERLINE"
    elif concealed:
        outcome="CONCEALED"
    elif med["delta"]>limit:
        outcome="MEDIAN_ALSO_OVER_LIMIT"
    else:
        outcome="REPRESENTATIVE_WITHIN_LIMIT"
    return {"schema":"gx.mapped-segment/2","method":"deposited-block-influence@2","entry":entry,"input_sha256":mapping["structure_sha256"],
            "mapping_sha256":hashlib.sha256(mapping_raw).hexdigest(),"model":mapping["model"],"entity":mapping["entity"],
            "reference":mapping["reference"],"interval":interval,"target_positions":target,"chains":chains,
            "mask":mask,"endpoint":endpoint,"unit":unit,"threshold":limit,"tolerance":tol,
            "observed_union_residues":len(union),"selected_residues":len(matched),"omitted_residues":len(omitted),
            "retained_residues":len(remaining),"rows":rows,"exceptions":exceptions,"borderline":borderline,
            "concealed":concealed,"interpretation":outcome,"reasons":reasons}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ("structure","mapping","fitter","out"): p.add_argument("--"+name,type=Path,required=True)
    for name in ("interval","mask","endpoint"): p.add_argument("--"+name,required=True)
    args=p.parse_args()
    table=measure(args.structure.read_bytes(),args.mapping.read_bytes(),args.interval,args.mask,args.endpoint,released_geometry(args.fitter))
    args.out.mkdir(parents=True,exist_ok=True)
    (args.out/"segment.json").write_text(json.dumps(table,separators=(",",":"),ensure_ascii=False,allow_nan=False)+"\n")


if __name__=="__main__":
    main()
