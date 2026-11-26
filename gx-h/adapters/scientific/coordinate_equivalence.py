"""Endpoint-specific evidence for transformations of released mmCIF coordinates.

The coverage method counts matches across three label chains. For a replayed row
deletion, inspect only the residue positions touched by the deletion; the other
positions are byte-identical. No fit, whole-file coverage, or new result is produced.
"""
from collections import defaultdict
from itertools import zip_longest
from math import isfinite

from .tl1a_geometry.worker import BACKBONE, CHAINS, _loop_rows

GEOMETRY_BUNDLE = "741e860f87822bb1b1b204cd18e6cb3035b5da35c7bded3f5cb1e97ada23ee1f"
MASKS = ("common_ca", "common_backbone", "ordered_backbone")
ANNOTATION = "gx.pdbx.coordinate-annotation/1"


def _geometry_lines(raw):
    lines = raw.decode("utf-8").splitlines()
    entries = [line.split() for line in lines if line.startswith("_entry.id ")]
    if (sum(line.lower().startswith("data_") for line in lines) != 1 or len(entries) != 1
            or len(entries[0]) != 2 or sum(line.strip() == "_atom_site.group_PDB" for line in lines) != 1):
        raise ValueError("Coordinates are outside the geometry reader's input profile")
    return lines


def same_coordinate_table(parent, child):
    """Require the released reader to see the same ordered records (spacing may differ)."""
    def rows(raw):
        return _loop_rows(_geometry_lines(raw), "_atom_site.")
    return all(a == b for a, b in zip_longest(rows(parent), rows(child)))


def _selection(sites):
    # Entries for this position are (label_comp_id, atom) -> passes the order filter.
    common = set.intersection(*(set(sites[c]) for c in CHAINS))
    complete = {comp for comp, _ in common if all((comp, atom) in common for atom in BACKBONE)}
    return (any(sites.values()), any(atom == "CA" for _, atom in common), bool(complete),
            any(all(sites[c][comp, atom] for atom in BACKBONE for c in CHAINS) for comp in complete))


def _affected_sites(raw, affected_positions):
    sites = defaultdict(lambda: {chain: {} for chain in CHAINS})
    for row in _loop_rows(_geometry_lines(raw), "_atom_site."):
        chain, atom = row["label_asym_id"], row["label_atom_id"]
        if (row["group_PDB"], row["pdbx_PDB_model_num"], row["label_entity_id"]) != ("ATOM", "1", "1"):
            continue
        if chain not in CHAINS or atom not in BACKBONE:
            continue
        pos = int(row["label_seq_id"])
        if pos not in affected_positions:
            continue
        key = (row["label_comp_id"], atom)
        occ = float(row["occupancy"])
        if not 0 <= occ <= 1:
            raise ValueError("invalid occupancy")
        if occ == 0:
            continue
        b = float(row["B_iso_or_equiv"])
        if (not isfinite(b) or not all(isfinite(float(row["Cartn_" + axis])) for axis in "xyz")
                or row["label_alt_id"] not in (".", "?") or row["type_symbol"] != BACKBONE[atom]
                or key in sites[pos][chain]):
            raise ValueError("ambiguous backbone")
        sites[pos][chain][key] = occ == 1 and b <= 40
    return sites


def withholding_effects(parent, child, affected_positions):
    """Check only the endpoint's numerator/denominator membership where rows were removed."""
    try:
        before = _affected_sites(parent, affected_positions)
        after = _affected_sites(child, affected_positions)
    except (KeyError, ValueError, RuntimeError, IndexError, UnicodeError):
        return dict.fromkeys(MASKS, "unknown")
    result = {mask: "equivalent" for mask in MASKS}
    for pos, sites in before.items():
        old, new = _selection(sites), _selection(after[pos])
        for i, mask in enumerate(MASKS, 1):
            if old[0] != new[0] or old[i] != new[i]:
                result[mask] = "different"
    return result


def relation(identity, spec, bundle_sha):
    proof = identity.get("transformation") or {}
    if (identity.get("reader") != "gx.pdbx.source-identity/3" or bundle_sha != GEOMETRY_BUNDLE
            or spec.get("capability") != "deposited-chain-geometry@2"
            or spec.get("output_name") != "geometry.json" or spec.get("metric_id") != "coverage"
            or spec.get("input_role") != "structure" or spec.get("source_roles") != ["structure"]
            or spec.get("observable") != "/metrics/coverage" or spec.get("result_pointer") != "/coverage_fraction"
            or spec.get("measurement_schema") not in (None, "gx.deposited_chain_geometry/2")
            or spec.get("unit") != "1" or spec.get("missing_parameters") != []):
        return "unknown"
    params = spec.get("parameters") or {}
    if set(params) != {"mask"} or params["mask"] not in MASKS:
        return "unknown"
    effects = proof.get("coverage_masks") or {}
    return effects.get(params["mask"], "unknown")


# The released count screen and these readers use the same model, entity, chains
# and atom selection. The signed eligibility denominator is independent of e, z,
# k and fitted endpoint. Evaluable intervals and misses require actual fits.
_INFLUENCE = "0e88a493cfa80d50b6b6fe1a9ceaeb6b8eafc4ba95b7aa2a32d802903bb436d7"
_SIGNED = "0ad355138c40104db552a5344e80a6ae5cdb5c1b0d1fc412d43d12a4e34341f7"
_COUNT = "eligible_20_position_blocks_by_count"
_COVERAGE = "common_fraction_of_observed_union"
SCREEN_ENDPOINTS = {
    (GEOMETRY_BUNDLE, "geometry.json", "coverage"): ("gx.deposited_chain_geometry/2", _COVERAGE),
    (_INFLUENCE, "block-influence.json", "coverage"): ("gx.block-influence/1", _COVERAGE),
    (_INFLUENCE, "block-influence.json", "eligible_blocks"): ("gx.block-influence/1", _COUNT),
    (_SIGNED, "signed-summary.json", "blocks_eligible"): ("gx.signed-summary/1", _COUNT),
}


def screen_column(spec, bundle_sha):
    entry = SCREEN_ENDPOINTS.get((bundle_sha, spec.get("output_name"), spec.get("metric_id")))
    if (entry and spec.get("measurement_schema") in (None, entry[0])
            and spec.get("input_role") == "structure" and not spec.get("missing_parameters")
            and spec.get("parameters", {}).get("mask") in ("common_ca", "common_backbone")):
        return entry[1]
    return None


def screened_originals(document, mask, column):
    """Yield coordinate digests with an actual published count or ratio for this selection."""
    if not isinstance(document, dict) or not isinstance(document.get("materials"), list):
        return
    if document.get("kind") == "count-only material screening, known before the study":
        digest_key, entity_key = "coordinate_sha256", "label_entity"
    elif document.get("title") == "Human PCNA coordinate material qualification":
        digest_key, entity_key = "sha256", "entity"
    else:
        return
    for item in document["materials"]:
        if not isinstance(item, dict) or item.get("model") != "1" or item.get(entity_key) != "1":
            continue
        chains = item.get("observed_positions_per_chain")
        if not isinstance(chains, dict) or set(chains) != {"A", "B", "C"}:
            continue
        masks = item.get("masks")
        row = masks.get(mask) if isinstance(masks, dict) else None
        if not isinstance(row, dict) or not isinstance(item.get(digest_key), str):
            continue
        if column == _COUNT:
            count = row.get(column)
            known = type(count) is int and count >= 0
        else:
            n, d = row.get("common_positions"), row.get("observed_union_positions")
            known = type(n) is int and type(d) is int and d > 0 and 0 <= n <= d
        if known:
            yield item[digest_key]
