"""Shared study definitions used by the three independent comparison arms."""
import hashlib
import itertools
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
STUDY = HERE.parent
ROOT = HERE.parents[3]
METHOD = "deposited-chain-geometry@1"
PACKAGE = "org.gx.protein@0.2.0"
MASKS = ("common_ca", "common_backbone", "ordered_backbone")
ENTRIES = ("2RE9", "2O0O")
PAIRS = ("A/B", "A/C", "B/C")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2, sort_keys=True, allow_nan=False, ensure_ascii=False) + "\n")


def grant():
    doc = json.loads((STUDY / "study-config.json").read_bytes())
    for item in doc["input_files"]:
        path = (STUDY / item["path"]).resolve()
        if digest(path) != item["sha256"]:
            raise ValueError("Changed comparison material: " + path.name)
        item["path"] = str(path)
    return doc


def entry_results(measurements):
    """Describe each observed chain pair using the prospectively fixed tolerances."""
    output = []
    for entry in ENTRIES:
        by_mask = {d["mask"]: d for d in measurements if d["entry"] == entry}
        eligible = [mask for mask, d in by_mask.items()
                    if d["selected_residues"] >= 60 and d["coverage_fraction"] >= .60]
        for pair in PAIRS:
            fits = []
            for mask in sorted(eligible):
                relevant = [p for p in by_mask[mask]["pairs"]
                            if p["fixed_chain"] + "/" + p["moving_chain"] == pair]
                if len(relevant) == 1 and "fitted_rmsd_angstrom" in relevant[0]:
                    fits.append(relevant[0])
            label = "insufficient"
            if len(fits) >= 2:
                stable = all(abs(a["fitted_rmsd_angstrom"] - b["fitted_rmsd_angstrom"]) <= .15
                             and abs(a["rotation_angle_deg"] - b["rotation_angle_deg"]) <= .5
                             for a, b in itertools.combinations(fits, 2))
                label = "stable" if stable else "sensitive"
            output.append({"entry": entry, "pair": pair, "label": label,
                           "assessed_masks": sorted(by_mask), "eligible_masks": sorted(eligible)})
    return output


def pair_results(measurements):
    """Include the actual research question: which conclusions hold in both deposits?"""
    individual = entry_results(measurements)
    shared = set.intersection(*[
        {m["mask"] for m in measurements if m["entry"] == entry
         and m["selected_residues"] >= 60 and m["coverage_fraction"] >= .60}
        for entry in ENTRIES
    ])
    paired = entry_results([m for m in measurements if m["mask"] in shared])
    assessed = {entry: sorted({m["mask"] for m in measurements if m["entry"] == entry})
                for entry in ENTRIES}
    for pair in PAIRS:
        labels = {r["label"] for r in paired if r["pair"] == pair}
        if len(shared) < 2 or "insufficient" in labels:
            label = "insufficient_common_coverage"
        elif len(labels) == 1:
            label = next(iter(labels)) + "_in_both"
        else:
            label = "differs_between_deposits"
        individual.append({"entry":"both","pair":pair,"label":label,
                           "assessed_masks_by_entry":assessed,"eligible_masks":sorted(shared)})
    return individual
